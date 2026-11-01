/* 我的收藏：两级视图——顶层收藏夹卡片 / 进入后夹内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad,
         getLibSearchQuery, replaceItems } from '../lib.js';
import { getJSON, isAbortError } from '../net.js';
import { collectionAutoControl, loadCollectionSubs,
         collSubKey, collectionSub } from './seasons.js?v=20261031e';
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
        '<div class="empty"><span class="spinner"></span>加载收藏夹列表...</div>';
    },
    showError: (msg) => {
      document.getElementById('fav-folder-list').innerHTML =
        `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost sm" onclick="favLoadFolders()">${icon('refresh')} 重试</button></div>`;
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
    box.innerHTML = `<div class="empty">${q ? '没有匹配的收藏夹（试试其他关键词）' : '暂无收藏夹'}</div>`;
    return;
  }
  box.innerHTML = state.view === 'grid'
    ? `<div class="lib-grid">${list.map(folderCard).join('')}</div>`
    : `<div class="panel"><div class="panel-body tight">${list.map(folderRow).join('')}</div></div>`;
}

const normalizedCover = (value) => safeHttpUrl(value, { httpsOnly: true });

const coverMarkup = (cover) => {
  const coverUrl = normalizedCover(cover);
  const image = coverUrl
    ? `<img src="${escapeAttr(coverUrl)}" alt="" onerror="this.hidden=true" loading="lazy">`
    : '';
  return `
      <span class="fc-placeholder" aria-hidden="true">${icon('bilibili')}<span>暂无封面</span></span>
      ${image}`;
};

const _card = (cover, count, title, subHtml, id) => {
  const itemCount = nonNegativeInt(count);
  return `
  <div class="fav-card" role="button" tabindex="0" onclick="favEnter(this.dataset.id)" onkeydown="if(event.target!==this)return;if(event.key==='Enter'||event.key===' '){event.preventDefault();favEnter(this.dataset.id)}" data-id="${escapeAttr(String(id ?? ''))}">
    <div class="fc-cover">
      ${coverMarkup(cover)}
      <span class="fc-count">${itemCount} 个视频</span>
    </div>
    <div class="fc-title">${escapeHtml(title)}</div>
    <div class="fc-sub">${subHtml}${collectionAutoControl('fav', id, title || '该收藏夹', title || '')}</div>
  </div>`;
};

const folderCard = (f) => {
  const learnedCover = _favLearnedCovers.get(String(f.id)) || '';
  return _card(normalizedCover(f.cover) || learnedCover,
               f.media_count, f.title || '(未命名)',
               icon('folder') + '收藏夹', f.id);
};

function folderRow(f) {
  const itemCount = nonNegativeInt(f.media_count);
  return `
    <div class="fav-folder" role="button" tabindex="0" onclick="favEnter(this.dataset.id)" onkeydown="if(event.target!==this)return;if(event.key==='Enter'||event.key===' '){event.preventDefault();favEnter(this.dataset.id)}" data-id="${escapeAttr(String(f.id ?? ''))}">
      <div class="fav-icon">${escapeHtml((f.title || '?').slice(0, 1))}</div>
      <div>
        <div class="fav-name">${escapeHtml(f.title)}</div>
        <div class="fav-count">${itemCount} 个视频</div>
      </div>
      ${collectionAutoControl('fav', f.id, f.title || '该收藏夹', f.title || '')}
      <span class="fav-arrow">${icon('chevron')}</span>
    </div>`;
}

function showList(title) {
  document.getElementById('fav-folder-list').style.display = 'none';
  document.getElementById('list-fav').style.display = '';
  document.getElementById('fav-back-btn').style.display = '';
  document.getElementById('fav-search').style.display = '';
  document.getElementById('fav-page-title').textContent = title;
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
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载收藏夹内容...</div>';
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
    d.list.innerHTML = `<div class="empty">${escapeHtml(error.message || '加载失败')}<br><button class="ghost sm" onclick="favEnter(this.dataset.id)" data-id="${escapeAttr(String(fid))}">${icon('refresh')} 重试</button></div>`;
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
