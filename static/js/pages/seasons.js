/* 追更合集：两级视图——顶层「我追的合集」卡片 / 进入后合集内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad,
         getLibSearchQuery, replaceItems, refreshLib, renderPage,
         collectionCard, collectionRow } from '../lib.js';
import { getJSON, postJSON, isAbortError } from '../net.js';
import { emptyStateHtml, loadingStateHtml } from '../lib.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, nonNegativeInt, safeHttpUrl, formatDay } from '../util.js';
import { AUTO_QUALITY_SEGMENTS, normAutoQuality } from '../quality.js';
import { announce, toast } from '../confirm.js';
import { refreshFollowChips } from '../follow.js';

let _seasons = [];
/* 合集自动下载订阅状态:kind+id -> {quality} | null(未订阅) */
let _collSubs = new Map();

/* 订阅状态的内存键。收藏页也要用,导出以免两处手写同一格式后漂移。 */
export function collSubKey(kind, id) { return `${kind}:${String(id)}`; }

/* 只读访问某个合集/收藏夹的订阅记录(没有则 undefined)。
   收藏页排序要用订阅时间,又不该直接碰 seasons.js 的模块内 Map。 */
export function collectionSub(kind, id) {
  return _collSubs.get(collSubKey(kind, id));
}

export function setCollectionSubs(collections) {
  _collSubs = new Map();
  for (const item of Array.isArray(collections) ? collections : []) {
    _collSubs.set(collSubKey(item.kind, item.id), item);
  }
}

/* 合集/收藏夹的自动下载档位:与关注 UP 卡同一套平铺分段(quality.js 共享字典,
   2026-11-17 用户要求,下拉退役)。未订阅只有「不自动下载」亮,点任一画质
   胶囊即订阅;已订阅当前档亮,点「不自动下载」停止。

   `label` 只用于无障碍文案(允许是「该合集」这类兜底串),`title` 是订阅时
   要落库的真实名称。两者分开是因为收藏页没有 _seasons 数据,订阅请求里的
   标题只能从卡片上带过来;混用会让兜底文案变成真实标题存进 up_list.json。 */
export function collectionAutoControl(kind, id, label, title = '') {
  const sub = _collSubs.get(collSubKey(kind, id));
  const mid = escapeAttr(String(id));
  const segments = AUTO_QUALITY_SEGMENTS.map(([value, text]) => {
    const on = sub ? (value !== '__off' && normAutoQuality(sub.quality) === value)
                   : value === '__off';
    const tip = value === '__off'
      ? (sub ? '停止自动下载' : '当前不自动下载')
      : (sub ? `新入库视频按${text}下载` : `订阅后新入库按${text}下载`);
    return `<button type="button" class="uc-auto-pill${on ? ' on' : ''}${value === '__off' ? ' off' : ''}"`
      + ` data-value="${escapeAttr(value)}" title="${escapeAttr(tip)}"`
      + ` aria-pressed="${on ? 'true' : 'false'}" onclick="collectionAutoPick(this)">${escapeHtml(text)}</button>`;
  }).join('');
  // kind/id/title 挂行上,胶囊只带自己的档位值;点击不得冒泡到卡片(role=button)
  // 触发进入详情。
  return `<div class="uc-auto-row" role="group" data-kind="${escapeAttr(kind)}" data-id="${mid}"`
    + ` data-title="${escapeAttr(String(title || ''))}"`
    + ` aria-label="${escapeAttr(label)}的自动下载档位"`
    + ` onclick="event.stopPropagation()">${segments}</div>`;
}

/* 就地同步一行的分段高亮(切档收尾用,不整页重渲染)。 */
function syncRowSegs(row, kind, cid) {
  if (!row) return;
  const sub = _collSubs.get(collSubKey(kind, cid));
  row.querySelectorAll('.uc-auto-pill').forEach((el) => {
    const value = String(el.dataset.value || '');
    const on = sub ? (value !== '__off' && normAutoQuality(sub.quality) === value)
                   : value === '__off';
    el.classList.toggle('on', on);
    el.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

export async function collectionAutoPick(btn) {
  const row = btn.closest('.uc-auto-row');
  const kind = String((row || btn).dataset.kind || '');
  const cid = parseInt(String((row || btn).dataset.id || ''), 10);
  const title = String((row || btn).dataset.title || '');
  const value = String(btn.dataset.value || '');
  if (!cid) return;
  const key = collSubKey(kind, cid);
  const existing = _collSubs.get(key);
  const turnOff = value === '__off';
  // 点的就是当前状态/当前档:不重复请求。
  // 注意不能写成 turnOff === !existing:订阅状态下 turnOff=false 且 !existing=false,
  // 恒等成立会把**换档点击**也吞掉(实测 4K 点击零请求零高亮)。守卫只拦真正的重复:
  // 未订阅点「不下载」、已订阅点当前档。
  if (!existing && turnOff) return;
  if (existing && normAutoQuality(existing.quality) === value) return;
  if (row && row.dataset.busy) return;   // 上一发还在路上,不叠加
  if (row) row.dataset.busy = '1';
  const prevSub = existing;
  // 乐观先亮(同 UP 卡):换档 3ms,卡的是订阅分支要拉全量内容建基线
  if (turnOff) _collSubs.delete(key);
  else _collSubs.set(key, { ...(existing || {}), quality: value });
  syncRowSegs(row, kind, cid);
  btn.disabled = true;
  // 订阅要拉全量内容建基线,请求可达数秒;期间用户可能切页或点进合集详情。
  // 收尾只就地同步这一行的分段高亮,不整页重渲染:renderPage 会重建全部卡片、
  // 封面图重挂,每次切档都闪一下(2026-11-17 用户反馈「卡一下」);徽标与排序
  // 本就不随切档变化,结果反馈由 announce 承担。
  try {
    if (turnOff) {
      const r = await postJSON('/api/collection/remove', { kind, id: cid });
      if (!r.ok) throw new Error(r.error || '停止失败');
      _collSubs.delete(key);
      announce('已停止该合集的自动下载');
    } else if (!existing) {
      const own = _seasons.find(s => String(s.id) === String(cid)) || {};
      const r = await postJSON('/api/collection/add', {
        kind, id: cid,
        // 标题优先取卡片带上来的真实名称:收藏页没有 _seasons 数据。
        title: title || own.title || '',
        cover: own.cover || '',
        quality: value,
      });
      if (!r.ok) throw new Error(r.error || '订阅失败');
      // 以后端返回为准:若后端把非法档位归一,本地不能继续显示用户选的档
      _collSubs.set(key, {
        ...(r.collection || {}), quality: (r.collection || {}).quality ?? value,
      });
      announce(r.already
        ? '该合集已在自动下载清单中'
        : `已订阅新视频自动下载（基线 ${r.baseline_count} 条）`);
    } else {
      const r = await postJSON('/api/collection/set-quality',
                               { kind, id: cid, quality: value });
      if (!r.ok) throw new Error(r.error || '设置失败');
      _collSubs.set(key, { ...existing, quality: value });
      announce('已更新自动下载清晰度');
    }
  } catch (error) {
    // 回滚到操作前的订阅状态,高亮跟手
    if (prevSub) _collSubs.set(key, prevSub); else _collSubs.delete(key);
    announce(error.message || '操作失败', true);
    toast(error.message || '操作失败');
  } finally {
    btn.disabled = false;
    if (row) delete row.dataset.busy;
    syncRowSegs(row, kind, cid);
  }
}
let _seasonsCurrent = null;
let _seasonGeneration = 0;
let _seasonController = null;

function cancelSeasonContent() {
  _seasonGeneration++;
  if (_seasonController) _seasonController.abort();
  _seasonController = null;
}

function seasonRequestIsCurrent(generation, controller, sid) {
  return generation === _seasonGeneration
    && controller === _seasonController
    && !controller.signal.aborted
    && state.mode === 'seasons'
    && String(_seasonsCurrent) === String(sid);
}

function showOverview() {
  document.getElementById('season-overview').style.display = '';
  document.getElementById('list-seasons').style.display = 'none';
  document.getElementById('season-back-btn').style.display = 'none';
  document.getElementById('season-page-title').textContent = '追更合集';
  document.getElementById('season-page-title').classList.add('sr-only');
  const search = document.querySelector('.page[data-page="seasons"] .lib-search');
  if (search) search.placeholder = '搜索合集名称';
  document.getElementById('meta-seasons').textContent =
    _seasons.length ? `共 ${_seasons.length} 个追更中的合集` : '';
}

export async function loadCollectionSubs(rerender) {
  try {
    const r = await getJSON('/api/up-list');
    if (r && r.ok) {
      setCollectionSubs(r.collections);
      // 调用方传入自己的重渲染函数(收藏页用它刷新收藏夹列表);
      // 缺省回落到合集页,避免跨页写别人的 DOM。
      // 订阅状态迟到会改变排序键(已订阅在前):重算后重画,但只在这条
      // 加载路径上重算——用户点分段不应触发重排。
      if (typeof rerender === 'function') rerender();
      else if (state.mode === 'seasons' && !_seasonsCurrent) {
        _orderDirty = true;
        renderOverview();
      }
    }
  } catch (error) { /* 订阅状态失败不影响列表展示 */ }
}

export async function loadSeasons() {
  cancelSeasonContent();
  _seasonsCurrent = null;
  replaceItems([]);
  showOverview();
  await cachedLoad({
    page: 'seasons',
    api: '/api/fav-seasons',
    cacheKey: 'fav_seasons',
    fast: true,
    extract: (b) => b.seasons || [],
    stillValid: () => state.mode === 'seasons' && !_seasonsCurrent,
    showLoading: () => {
      document.getElementById('season-overview').innerHTML =
        loadingStateHtml('加载追更合集…');
    },
    showError: (msg) => {
      document.getElementById('season-overview').innerHTML = emptyStateHtml(escapeHtml(msg || '加载失败'), { retry: 'loadSeasons()' });
    },
    apply: (b) => {
      _seasons = b.seasons || [];
      _orderDirty = true;
      showOverview();
      renderOverview();
      loadCollectionSubs();
    },
  });
}

/* 排序规则与「关注 UP」一致:已开自动下载的整批在前,其余在后;
   同一组内按合集更新时间倒序(接口没给 mtime 时保持原有顺序)。
   重算只在数据加载时发生(_orderDirty):分段切换即时生效但列表位置不动,
   重排只出现在页面加载/刷新(2026-11-17 用户要求,与关注 UP 一致)。 */
let _orderDirty = true;
let _orderedSeasons = [];
function overviewList() {
  if (_orderDirty) {
    _orderDirty = false;
    const key = (s) => collSubKey('season', s.id);
    _orderedSeasons = [..._seasons].sort((a, b) =>
      (Number(!_collSubs.has(key(a))) - Number(!_collSubs.has(key(b))))
      || (nonNegativeInt(b.mtime) - nonNegativeInt(a.mtime)));
  }
  return _orderedSeasons;
}

function renderOverview() {
  const box = document.getElementById('season-overview');
  if (!box) return;
  const q = getLibSearchQuery();
  const all = overviewList();
  let list = all;
  if (q) list = all.filter(s => (s.title || '').toLowerCase().includes(q));
  const metaEl = document.getElementById('meta-seasons');
  if (metaEl) {
    metaEl.textContent = q
      ? `共 ${all.length} 个合集 · 匹配 ${list.length}`
      : (all.length ? `共 ${all.length} 个追更中的合集` : '');
  }
  if (list.length === 0) {
    box.innerHTML = emptyStateHtml(q ? '没有匹配结果（试试其他关键词）' : '还没有追的合集。在 B 站视频页点「追合集」后，会出现在这里。');
    return;
  }
  box.innerHTML = state.view === 'grid'
    ? `<div class="lib-grid">${list.map(seasonCard).join('')}</div>`
    : `<div class="row-list">${list.map(seasonRow).join('')}</div>`;
  // 卡片上的关注胶囊:渲染完批量落「关注/已关注」(视频卡列表渲染器同一做法)
  refreshFollowChips();
}

const isBilibiliDefaultCover = (cover) =>
  /\/(?:viedeo|video)_material_default\.png(?:[?#]|$)/i.test(cover);

const seasonCard = (s) => {
  const normalizedCover = safeHttpUrl(s.cover, { httpsOnly: true });
  const cover = normalizedCover && !isBilibiliDefaultCover(normalizedCover)
    ? normalizedCover
    : '';
  // 「追更中」角标撤下(2026-11-18 用户):本页来自「我追的合集」接口,
  // 每张卡必然在追更,角标零信息量;搜索也只在这批里过滤。
  return collectionCard({
    id: s.id, title: s.title || '', count: s.media_count, cover,
    onEnter: 'seasonEnter',
    autoHtml: collectionAutoControl('season', s.id, s.title || '该合集', s.title || ''),
    upName: s.uploader || '',
    upMid: s.uploader_mid || 0,
    latestLabel: s.mtime ? `更新于 ${formatDay(s.mtime)}` : '',
  });
};

function seasonRow(s) {
  return collectionRow({
    id: s.id, title: s.title, count: s.media_count,
    onEnter: 'seasonEnter',
    autoHtml: collectionAutoControl('season', s.id, s.title || '该合集', s.title || ''),
    iconName: 'layers',
    upName: s.uploader || '',
    latestLabel: s.mtime ? `更新于 ${formatDay(s.mtime)}` : '',
  });
}

export async function seasonEnter(sid) {
  const season = _seasons.find(s => String(s.id) === String(sid));
  if (!season) return;
  cancelSeasonContent();
  const generation = _seasonGeneration;
  const controller = new AbortController();
  _seasonController = controller;
  _seasonsCurrent = String(sid);
  replaceItems([]);
  state.checked.clear();
  state.shown = 0;
  const search = document.querySelector('.page[data-page="seasons"] .lib-search');
  if (search) search.placeholder = '搜索标题 / UP 主';
  document.getElementById('season-overview').style.display = 'none';
  document.getElementById('list-seasons').style.display = '';
  document.getElementById('season-back-btn').style.display = '';
  document.getElementById('season-page-title').textContent = season.title;
  document.getElementById('season-page-title').classList.remove('sr-only');
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = loadingStateHtml('加载合集内容…');
  try {
    const r = await getJSON(`/api/season-content?season_id=${encodeURIComponent(String(sid))}`, {
      signal: controller.signal,
    });
    if (!seasonRequestIsCurrent(generation, controller, sid)) return;
    if (!r.ok) throw new Error(r.error || '加载失败');
    replaceItems(r.items || []);
    renderDefaultList(d);
    d.meta.textContent = `共 ${state.items.length} 条`;
  } catch (error) {
    if (isAbortError(error) || !seasonRequestIsCurrent(generation, controller, sid)) return;
    d.list.innerHTML = emptyStateHtml(escapeHtml(error.message || '加载失败'), { retry: 'seasonEnter(this.dataset.id)', retryId: String(sid) });
  }
}

export function seasonBack() { loadSeasons(); }
export function seasonRefresh() {
  if (_seasonsCurrent) seasonEnter(_seasonsCurrent);
  else loadSeasons();
}

registerPage('seasons', {
  load: loadSeasons,
  retry: seasonRefresh,
  render: (d) => { if (!_seasonsCurrent) renderOverview(); else renderDefaultList(d); },
  infinite: () => _seasonsCurrent ? 'items' : 'none',
  actionBarVisible: () => !!_seasonsCurrent,
  reset: () => { cancelSeasonContent(); _seasonsCurrent = null; showOverview(); },
  deactivate: cancelSeasonContent,
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '这个合集还没有视频',
});
