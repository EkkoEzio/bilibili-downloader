/* 我的收藏：两级视图——顶层收藏夹卡片 / 进入后夹内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad,
         getLibSearchQuery, replaceItems, collectionCard, collectionRow } from '../lib.js';
import { getJSON, isAbortError } from '../net.js';
import { collectionAutoControl, loadCollectionSubs,
         collSubKey, collectionSub } from './seasons.js?v=20261111b';
import { emptyStateHtml, loadingStateHtml } from '../lib.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, nonNegativeInt, safeHttpUrl } from '../util.js';

let _favFolders = [];
let _favCurrent = null;
let _favGeneration = 0;
let _favController = null;
const _favLearnedCovers = new Map();

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
  document.getElementById('fav-search').style.display = '';
  document.getElementById('fav-page-title').textContent = '我的收藏';
  document.getElementById('fav-page-title').classList.add('sr-only');
  const search = document.getElementById('fav-search');
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
    fast: true,
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
      showFolders();
      renderFolders();
      // 订阅状态是另一条异步请求,返回时用户可能已点进某个收藏夹:
      // renderFolders() 会写 meta-fav,迟到执行会把「共 N 条」刷回
      // 「共 M 个收藏夹」。只在仍停留在收藏夹列表时才重画。
      loadCollectionSubs(() => {
        if (state.mode === 'fav' && !_favCurrent) renderFolders();
      });
    },
  });
}

/* 排序规则与「关注 UP」「追更合集」一致:已开自动下载的整批在前。
   组内用订阅时间倒序——收藏夹接口本身不带任何时间字段,只有本地订阅记录
   有 added_at,所以未订阅的那一组没有时间键,保持 B 站返回的原顺序。 */
function favFolderList() {
  const subOf = (f) => collectionSub('fav', f.id);
  return [..._favFolders].sort((a, b) => {
    const subA = !!subOf(a);
    const subB = !!subOf(b);
    if (subA !== subB) return subA ? -1 : 1;
    if (!subA) return 0;
    return nonNegativeInt(subOf(b).added_at) - nonNegativeInt(subOf(a).added_at);
  });
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
}

const normalizedCover = (value) => safeHttpUrl(value, { httpsOnly: true });

const folderCard = (f) => {
  const learnedCover = _favLearnedCovers.get(String(f.id)) || '';
  /* 已订阅自动下载的收藏夹与追更合集卡共用同一枚「追更中」角标:
     合集卡恒挂、收藏夹卡只在订阅后挂,两页状态语言一致。 */
  const badge = collectionSub('fav', f.id) ? '<span class="uc-badge on">追更中</span>' : '';
  return collectionCard({
    id: f.id, title: f.title || '', count: f.media_count,
    cover: normalizedCover(f.cover) || learnedCover, badge,
    onEnter: 'favEnter',
    autoHtml: collectionAutoControl('fav', f.id, f.title || '该收藏夹', f.title || ''),
  });
};

function folderRow(f) {
  /* 与网格卡的 .uc-badge.on、合集行式行的 .fav-tag 同一枚状态标记:
     行式没有封面可挂角标,标签形式在两个视图之间保持订阅状态可见。 */
  const badge = collectionSub('fav', f.id) ? '<span class="fav-tag">追更中</span>' : '';
  return collectionRow({
    id: f.id, title: f.title, count: f.media_count, badge,
    onEnter: 'favEnter',
    autoHtml: collectionAutoControl('fav', f.id, f.title || '该收藏夹', f.title || ''),
    iconName: 'folder',
  });
}

function showList(title) {
  document.getElementById('fav-folder-list').style.display = 'none';
  document.getElementById('list-fav').style.display = '';
  document.getElementById('fav-back-btn').style.display = '';
  document.getElementById('fav-search').style.display = '';
  document.getElementById('fav-page-title').textContent = title;
  document.getElementById('fav-page-title').classList.remove('sr-only');
  const search = document.getElementById('fav-search');
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
