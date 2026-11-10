/* 追更合集：两级视图——顶层「我追的合集」卡片 / 进入后合集内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad,
         getLibSearchQuery, replaceItems, refreshLib, renderPage } from '../lib.js';
import { getJSON, postJSON, isAbortError } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, nonNegativeInt, safeHttpUrl } from '../util.js';
import { QUALITY_OPTIONS, QUALITY_CARD_LABELS } from '../quality.js';
import { announce } from '../confirm.js';

let _seasons = [];
/* 合集自动下载订阅状态:kind+id -> {quality} | null(未订阅) */
let _collSubs = new Map();

function _qualityOptionsHtml(cur) {
  // cur 为 null 表示「未订阅」:此时不能给任何画质项打 selected。
  // 否则它会和「不下载」同时 selected,浏览器只认最后一个,控件显示成
  // 「最高画质」而实际没有订阅;用户再选一次「最高画质」不产生 change
  // 事件,于是永远订阅不上。
  return QUALITY_OPTIONS.map(([value]) => {
    const label = QUALITY_CARD_LABELS.get(value) ?? String(value);
    const selected = cur != null && value === cur ? ' selected' : '';
    return `<option value="${escapeAttr(value)}"${selected}>${escapeHtml(label)}</option>`;
  }).join('');
}

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

/* 合集/收藏夹的自动下载开关:未订阅 → 选清晰度即订阅;已订阅 → 可换档或停止。

   `label` 只用于无障碍文案(允许是「该合集」这类兜底串),`title` 是订阅时
   要落库的真实名称。两者分开是因为收藏页没有 _seasons 数据,订阅请求里的
   标题只能从卡片上带过来;混用会让兜底文案变成真实标题存进 up_list.json。 */
export function collectionAutoControl(kind, id, label, title = '') {
  const sub = _collSubs.get(collSubKey(kind, id));
  const mid = escapeAttr(String(id));
  const titleAttr = ` data-title="${escapeAttr(String(title || ''))}"`;
  if (sub) {
    return `<select class="uc-auto on" data-kind="${kind}" data-id="${mid}"${titleAttr}
      aria-label="${escapeAttr(label)}的自动下载清晰度"
      title="新入库视频自动下载中;改选换清晰度,选「不下载」即停止"
      onclick="event.stopPropagation()"
      onchange="collectionAutoChanged(this)">
      <option value="__off">不下载</option>
      ${_qualityOptionsHtml(sub.quality || '')}</select>`;
  }
  return `<select class="uc-auto" data-kind="${kind}" data-id="${mid}"${titleAttr}
    aria-label="${escapeAttr(label)}的自动下载清晰度"
    title="当前不自动下载;选任一清晰度即订阅新入库视频"
    onclick="event.stopPropagation()"
    onchange="collectionAutoChanged(this)">
    <option value="__off" selected>不下载</option>
    ${_qualityOptionsHtml(null)}</select>`;
}

export async function collectionAutoChanged(sel) {
  const kind = String(sel.dataset.kind || '');
  const id = String(sel.dataset.id || '');
  const value = String(sel.value || '');
  if (!id || value === '__on') return;
  const existing = _collSubs.get(collSubKey(kind, id));
  if (value === '__off' && !existing) { refreshLib(); return; }  // 本来就不下载
  sel.disabled = true;
  // 订阅要拉全量内容建基线,请求可达数秒;期间用户可能切页或点进合集详情。
  // 收尾只允许重渲染发起页,且用 renderPage(纯内存重渲染)而不是 refreshLib:
  // 后者会重新拉数据,把详情视图重置回列表层、或把切过去的页面整页重载。
  const fromPage = state.mode;
  try {
    if (value === '__off') {
      const r = await postJSON('/api/collection/remove', { kind, id: parseInt(id, 10) });
      if (!r.ok) throw new Error(r.error || '停止失败');
      _collSubs.delete(collSubKey(kind, id));
      announce('已停止该合集的自动下载');
    } else if (!existing) {
      const own = _seasons.find(s => String(s.id) === id) || {};
      const r = await postJSON('/api/collection/add', {
        kind, id: parseInt(id, 10),
        // 标题优先取卡片带上来的真实名称:收藏页没有 _seasons 数据。
        title: String(sel.dataset.title || '') || own.title || '',
        cover: own.cover || '',
        quality: value,
      });
      if (!r.ok) throw new Error(r.error || '订阅失败');
      // 以后端返回为准:若后端把非法档位归一成空串,本地不能继续显示用户选的档
      _collSubs.set(collSubKey(kind, id), {
        ...(r.collection || {}), quality: (r.collection || {}).quality ?? value,
      });
      announce(r.already
        ? '该合集已在自动下载清单中'
        : `已订阅新视频自动下载（基线 ${r.baseline_count} 条）`);
    } else {
      const r = await postJSON('/api/collection/set-quality',
                               { kind, id: parseInt(id, 10), quality: value });
      if (!r.ok) throw new Error(r.error || '设置失败');
      _collSubs.set(collSubKey(kind, id), { ...existing, quality: value });
      announce('已更新自动下载清晰度');
    }
  } catch (error) {
    announce(error.message || '操作失败', true);
    alert(error.message || '操作失败');
  } finally {
    sel.disabled = false;
    if (state.mode === fromPage) renderPage();
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
      if (typeof rerender === 'function') rerender();
      else if (state.mode === 'seasons' && !_seasonsCurrent) renderOverview();
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
        '<div class="empty"><span class="spinner"></span>加载追更合集...</div>';
    },
    showError: (msg) => {
      document.getElementById('season-overview').innerHTML =
        `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost" onclick="loadSeasons()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _seasons = b.seasons || [];
      showOverview();
      renderOverview();
      loadCollectionSubs();
    },
  });
}

/* 排序规则与「关注 UP」一致:已开自动下载的整批在前,其余在后;
   同一组内按合集更新时间倒序(接口没给 mtime 时保持原有顺序)。 */
function overviewList() {
  const key = (s) => collSubKey('season', s.id);
  return [..._seasons].sort((a, b) =>
    (Number(!_collSubs.has(key(a))) - Number(!_collSubs.has(key(b))))
    || (nonNegativeInt(b.mtime) - nonNegativeInt(a.mtime)));
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
    box.innerHTML = `<div class="empty">${q ? '没有匹配结果（试试其他关键词）' : '还没有追的合集。在 B 站视频页点「追合集」后，会出现在这里。'}</div>`;
    return;
  }
  box.innerHTML = state.view === 'grid'
    ? `<div class="lib-grid">${list.map(seasonCard).join('')}</div>`
    : `<div class="row-list">${list.map(seasonRow).join('')}</div>`;
}

const isBilibiliDefaultCover = (cover) =>
  /\/(?:viedeo|video)_material_default\.png(?:[?#]|$)/i.test(cover);

const seasonCard = (s) => {
  const normalizedCover = safeHttpUrl(s.cover, { httpsOnly: true });
  const cover = normalizedCover && !isBilibiliDefaultCover(normalizedCover)
    ? normalizedCover
    : '';
  const image = cover
    ? `<img src="${escapeAttr(cover)}" alt="" onerror="this.hidden=true" loading="lazy">`
    : '';
  const itemCount = nonNegativeInt(s.media_count);
  return `
  <div class="fav-card" role="button" tabindex="0" onclick="seasonEnter(this.dataset.id)" onkeydown="if(event.target!==this)return;if(event.key==='Enter'||event.key===' '){event.preventDefault();seasonEnter(this.dataset.id)}" data-id="${escapeAttr(String(s.id ?? ''))}">
    <div class="fc-cover">
      <span class="fc-placeholder" aria-hidden="true">${icon('bilibili')}<span>暂无封面</span></span>
      ${image}
      <span class="uc-badge on">追更中</span>
      <span class="fc-count">${itemCount} 个视频</span>
    </div>
    <div class="fc-head">
      <div class="fc-title" title="${escapeAttr(s.title || '')}">${escapeHtml(s.title || '(未命名)')}</div>
      ${collectionAutoControl('season', s.id, s.title || '该合集', s.title || '')}
    </div>
  </div>`;
};

function seasonRow(s) {
  const itemCount = nonNegativeInt(s.media_count);
  return `
    <div class="fav-folder" role="button" tabindex="0" onclick="seasonEnter(this.dataset.id)" onkeydown="if(event.target!==this)return;if(event.key==='Enter'||event.key===' '){event.preventDefault();seasonEnter(this.dataset.id)}" data-id="${escapeAttr(String(s.id ?? ''))}">
      <div class="fav-icon">${icon('layers')}</div>
      <div class="fav-main">
        <div class="fav-name">${escapeHtml(s.title)}<span class="fav-tag">追更中</span></div>
        <div class="fav-count">${itemCount} 个视频</div>
      </div>
      ${collectionAutoControl('season', s.id, s.title || '该合集', s.title || '')}
      <span class="fav-arrow">${icon('chevron')}</span>
    </div>`;
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
  document.getElementById('season-overview').style.display = 'none';
  document.getElementById('list-seasons').style.display = '';
  document.getElementById('season-back-btn').style.display = '';
  document.getElementById('season-page-title').textContent = season.title;
  document.getElementById('season-page-title').classList.remove('sr-only');
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载合集内容...</div>';
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
    d.list.innerHTML = `<div class="empty">${escapeHtml(error.message || '加载失败')}<br><button class="ghost" onclick="seasonEnter(this.dataset.id)" data-id="${escapeAttr(String(sid))}">${icon('refresh')} 重试</button></div>`;
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
