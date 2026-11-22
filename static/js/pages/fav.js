/* 我的收藏：两级视图——顶层收藏夹卡片 / 进入后夹内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad,
         getLibSearchQuery, replaceItems, collectionCard, collectionRow,
         collectionCoverMedia } from '../lib.js';
import { getJSON, isAbortError } from '../net.js';
import { collectionAutoControl, loadCollectionSubs,
         collSubKey, collectionSub } from './seasons.js';
import { emptyStateHtml, loadingStateHtml } from '../lib.js';
import { refreshFollowChips } from '../follow.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, nonNegativeInt, safeHttpUrl, formatDay } from '../util.js';

let _favFolders = [];
let _favCurrent = null;

/* 收藏夹「最近收藏时间」:收藏夹列表接口不带该字段,进详情时从条目的
   fav_at 学习缓存(学到之前卡片不显示,不硬编、不猜)。 */
const FAV_LATEST_KEY = 'bd_fav_latest';
let _favLatest = new Map();
try {
  const saved = JSON.parse(localStorage.getItem(FAV_LATEST_KEY) || '{}');
  for (const [k, v] of Object.entries(saved)) _favLatest.set(String(k), Number(v) || 0);
} catch (error) { /* 缓存损坏就当没有 */ }

function noteFavLatest(fid, items) {
  const maxTs = (Array.isArray(items) ? items : [])
    .reduce((m, it) => Math.max(m, Number(it && it.fav_at) || 0), 0);
  if (!maxTs) return;
  const key = String(fid);
  if (maxTs <= (_favLatest.get(key) || 0)) return;
  _favLatest.set(key, maxTs);
  try {
    localStorage.setItem(FAV_LATEST_KEY,
      JSON.stringify(Object.fromEntries(_favLatest)));
  } catch (error) { /* 存不下就算了,内存里还有 */ }
}

const favLatestLabel = (fid) => {
  const ts = _favLatest.get(String(fid)) || 0;
  return ts ? `最近收藏 ${formatDay(ts)}` : '';
};

let _favGeneration = 0;
let _favController = null;
const _favLearnedCovers = new Map();

/* 收藏夹拼图封面(2026-11-18 用户要求):收藏夹列表接口 cover 常为空,进页后
   异步拉一次 /api/fav-folder-covers(后端有 10 分钟缓存),用夹内最新收藏
   视频的封面就地补拼图;模块级缓存,切页往返不重拉。 */
const _favFolderCovers = new Map();
let _favCoversLoaded = false;

function cancelFavContent() {
  _favGeneration++;
  if (_favController) _favController.abort();
  _favController = null;
}

function favRequestIsCurrent(generation, controller, fid) {
  return generation === _favGeneration
    && controller === _favController
    && !controller.signal.aborted
    && state.mode === 'fav'
    && String(_favCurrent) === String(fid);
}

function showFolders() {
  document.getElementById('fav-folder-list').style.display = '';
  document.getElementById('list-fav').style.display = 'none';
  document.getElementById('fav-back-btn').style.display = 'none';
  document.getElementById('fav-page-title').textContent = '收藏';
  document.getElementById('fav-page-title').classList.add('sr-only');
  const search = document.getElementById('tb-search');
  if (search) search.placeholder = '搜索收藏夹名称';
  document.getElementById('meta-fav').textContent =
    _favFolders.length ? `共 ${_favFolders.length} 个收藏夹` : '';
}

export async function favLoadFolders() {
  cancelFavContent();
  _favCurrent = null;
  replaceItems([]);
  showFolders();
  await cachedLoad({
    page: 'fav',
    api: '/api/fav-folders',
    cacheKey: 'fav_folders',
    extract: (b) => b.folders || [],
    stillValid: () => state.mode === 'fav' && !_favCurrent,
    showLoading: () => {
      document.getElementById('fav-folder-list').innerHTML =
        loadingStateHtml('加载收藏夹列表…');
    },
    showError: (msg) => {
      document.getElementById('fav-folder-list').innerHTML = emptyStateHtml(escapeHtml(msg || '加载失败'), { retry: 'favLoadFolders()' });
    },
    apply: (b) => {
      _favFolders = b.folders || [];
      _orderDirty = true;
      showFolders();
      renderFolders();
      loadFavFolderCovers();
      // 订阅状态是另一条异步请求,返回时用户可能已点进某个收藏夹:
      // renderFolders() 会写 meta-fav,迟到执行会把「共 N 条」刷回
      // 「共 M 个收藏夹」。只在仍停留在收藏夹列表时才重画。
      loadCollectionSubs(() => {
        // 订阅状态迟到会改变排序键(已订阅在前):重算后只在收藏夹列表视图重画
        _orderDirty = true;
        if (state.mode === 'fav' && !_favCurrent) renderFolders();
      });
    },
  });
}

/* 排序规则与「关注 UP」「追更合集」一致:已开自动下载的整批在前。
   组内用订阅时间倒序——收藏夹接口本身不带任何时间字段,只有本地订阅记录
   有 added_at,所以未订阅的那一组没有时间键,保持 B 站返回的原顺序。
   重算只在数据加载时发生(_orderDirty):分段切换即时生效但列表位置不动,
   重排只出现在页面加载/刷新(2026-11-17 用户要求,与关注 UP 一致)。 */
let _orderDirty = true;
let _orderedFolders = [];
function favFolderList() {
  if (_orderDirty) {
    _orderDirty = false;
    const subOf = (f) => collectionSub('fav', f.id);
    _orderedFolders = [..._favFolders].sort((a, b) => {
      const subA = !!subOf(a);
      const subB = !!subOf(b);
      if (subA !== subB) return subA ? -1 : 1;
      if (!subA) return 0;
      return nonNegativeInt(subOf(b).added_at) - nonNegativeInt(subOf(a).added_at);
    });
  }
  return _orderedFolders;
}

function renderFolders() {
  const box = document.getElementById('fav-folder-list');
  if (!box) return;
  const q = getLibSearchQuery();
  const all = favFolderList();
  let list = all;
  if (q) list = all.filter(f => (f.title || '').toLowerCase().includes(q));
  const metaEl = document.getElementById('meta-fav');
  if (metaEl) {
    metaEl.textContent = q
      ? `共 ${all.length} 个收藏夹 · 匹配 ${list.length}`
      : (all.length ? `共 ${all.length} 个收藏夹` : '');
  }
  if (list.length === 0) {
    box.innerHTML = emptyStateHtml(q ? '没有匹配的收藏夹（试试其他关键词）' : '暂无收藏夹');
    return;
  }
  box.innerHTML = state.view === 'grid'
    ? `<div class="lib-grid">${list.map(folderCard).join('')}</div>`
    : `<div class="row-list">${list.map(folderRow).join('')}</div>`;
  // 与追更页一致:渲染完批量落关注状态(当前收藏卡不带 UP 名,空跑无害)
  refreshFollowChips();
}

/* 拼图封面到货后只补封面区,不整页重渲染(标题/沙发等元素不动)。 */
function patchFolderCovers() {
  const box = document.getElementById('fav-folder-list');
  if (!box) return;
  box.querySelectorAll('.fav-card[data-id]').forEach((card) => {
    const covers = _favFolderCovers.get(String(card.dataset.id)) || [];
    if (!covers.length || card.dataset.coversApplied) return;
    const media = collectionCoverMedia('', covers);
    if (!media) return;
    const coverEl = card.querySelector('.fc-cover');
    if (!coverEl) return;
    card.dataset.coversApplied = '1';
    // 占位符是 .fc-cover 的第一个子节点,插到它后面才能盖住它
    const anchor = coverEl.querySelector('.fc-placeholder');
    if (anchor) anchor.insertAdjacentHTML('afterend', media);
    else coverEl.insertAdjacentHTML('afterbegin', media);
  });
}

async function loadFavFolderCovers() {
  if (_favCoversLoaded || !_favFolders.length) return;
  _favCoversLoaded = true;
  try {
    const r = await getJSON('/api/fav-folder-covers');
    if (!r || !r.ok || !Array.isArray(r.covers)) {
      _favCoversLoaded = false;
      return;
    }
    for (const entry of r.covers) {
      if (entry && entry.id != null) {
        _favFolderCovers.set(String(entry.id),
          Array.isArray(entry.covers) ? entry.covers : []);
      }
    }
    // 只在仍停留在收藏夹列表时就地补;进了详情/切页后由下次渲染带上
    if (state.mode === 'fav' && !_favCurrent) patchFolderCovers();
  } catch (error) {
    _favCoversLoaded = false;
    /* 拼图封面失败不影响列表展示,下次进页重试 */
  }
}

const normalizedCover = (value) => safeHttpUrl(value, { httpsOnly: true });

const folderCard = (f) => {
  const learnedCover = _favLearnedCovers.get(String(f.id)) || '';
  // 「追更中」角标撤下(2026-11-18 用户):档位沙发本身已是订阅状态,角标重复
  return collectionCard({
    id: f.id, title: f.title || '', count: f.media_count,
    cover: normalizedCover(f.cover) || learnedCover,
    covers: _favFolderCovers.get(String(f.id)) || [],
    onEnter: 'favEnter',
    autoHtml: collectionAutoControl('fav', f.id, f.title || '该收藏夹', f.title || ''),
    latestLabel: favLatestLabel(f.id),
  });
};

function folderRow(f) {
  // 「追更中」小标签同步撤下(2026-11-18 用户),理由同网格卡
  return collectionRow({
    id: f.id, title: f.title, count: f.media_count,
    onEnter: 'favEnter',
    autoHtml: collectionAutoControl('fav', f.id, f.title || '该收藏夹', f.title || ''),
    iconName: 'folder',
    latestLabel: favLatestLabel(f.id),
  });
}

function showList(title) {
  document.getElementById('fav-folder-list').style.display = 'none';
  document.getElementById('list-fav').style.display = '';
  document.getElementById('fav-back-btn').style.display = '';
  document.getElementById('fav-page-title').textContent = title;
  document.getElementById('fav-page-title').classList.remove('sr-only');
  const search = document.getElementById('tb-search');
  if (search) search.placeholder = '搜索标题 / UP 主';
}

export async function favEnter(fid) {
  const folder = _favFolders.find(f => String(f.id) === String(fid));
  if (!folder) return;
  cancelFavContent();
  const generation = _favGeneration;
  const controller = new AbortController();
  _favController = controller;
  _favCurrent = String(fid);
  replaceItems([]);
  state.checked.clear();
  state.shown = 0;
  showList(folder.title);
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = loadingStateHtml('加载收藏夹内容…');
  try {
    const r = await getJSON(`/api/fav-content?media_id=${encodeURIComponent(String(fid))}`, {
      signal: controller.signal,
    });
    if (!favRequestIsCurrent(generation, controller, fid)) return;
    if (!r.ok) throw new Error(r.error || '加载失败');
    const items = Array.isArray(r.items) ? r.items : [];
    noteFavLatest(fid, items);
    if (!normalizedCover(folder.cover)) {
      const latestCover = items.map(item => normalizedCover(item.thumbnail)).find(Boolean);
      if (latestCover) _favLearnedCovers.set(String(fid), latestCover);
    }
    replaceItems(items);
    renderDefaultList(d);
    d.meta.textContent = `共 ${state.items.length} 条`;
  } catch (error) {
    if (isAbortError(error) || !favRequestIsCurrent(generation, controller, fid)) return;
    d.list.innerHTML = emptyStateHtml(escapeHtml(error.message || '加载失败'), { retry: 'favEnter(this.dataset.id)', retryId: String(fid) });
  }
}

export function favBack() { favLoadFolders(); }
export function favRefresh() {
  if (_favCurrent) favEnter(_favCurrent);
  else favLoadFolders();
}

registerPage('fav', {
  load: favLoadFolders,
  retry: favRefresh,
  render: (d) => { if (!_favCurrent) renderFolders(); else renderDefaultList(d); },
  infinite: () => _favCurrent ? 'items' : 'none',
  actionBarVisible: () => !!_favCurrent,
  reset: () => {
    cancelFavContent();
    _favCurrent = null;
    _favLearnedCovers.clear();
    showFolders();
  },
  deactivate: cancelFavContent,
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '这里还没有收藏任何视频',
});
