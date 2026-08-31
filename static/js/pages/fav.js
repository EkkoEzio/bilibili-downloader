/* 我的收藏:两级视图——顶层收藏夹卡片 / 进入后夹内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad, getLibSearchQuery } from '../lib.js';
import { getJSON } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, nonNegativeInt, safeHttpUrl } from '../util.js';

let _favFolders = [];
let _favCurrent = null;  // 已进入的收藏夹 id

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
  _favCurrent = null;
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
    },
  });
}

/* 顶层:收藏夹卡片/行,支持列表/网格两种视图与标题搜索 */
function renderFolders() {
  const box = document.getElementById('fav-folder-list');
  if (!box) return;
  const q = getLibSearchQuery();
  let list = _favFolders;
  if (q) list = _favFolders.filter(f => (f.title || '').toLowerCase().includes(q));
  const metaEl = document.getElementById('meta-fav');
  if (metaEl) {
    metaEl.textContent = q
      ? `共 ${_favFolders.length} 个收藏夹 · 匹配 ${list.length}`
      : (_favFolders.length ? `共 ${_favFolders.length} 个收藏夹` : '');
  }
  if (list.length === 0) {
    box.innerHTML = `<div class="empty">${q ? '没有匹配的收藏夹(试试其他关键词)' : '暂无收藏夹'}</div>`;
    return;
  }
  box.innerHTML = state.view === 'grid'
    ? `<div class="lib-grid">${list.map(folderCard).join('')}</div>`
    : `<div class="panel"><div class="panel-body tight">${list.map(folderRow).join('')}</div></div>`;
}

const _card = (cover, count, title, subHtml, id) => {
  const coverUrl = safeHttpUrl(cover, { httpsOnly: true });
  const itemCount = nonNegativeInt(count);
  return `
  <div class="fav-card" onclick="favEnter(this.dataset.id)" data-id="${escapeAttr(String(id ?? ''))}">
    <div class="fc-cover">
      <img src="${escapeAttr(coverUrl)}" onerror="this.style.visibility='hidden'" loading="lazy">
      <span class="fc-count">${itemCount} 个视频</span>
    </div>
    <div class="fc-title">${escapeHtml(title)}</div>
    <div class="fc-sub">${subHtml}</div>
  </div>`;
};

const folderCard = (f) =>
  _card(f.cover || '', f.media_count, f.title || '(未命名)',
        icon('folder') + '收藏夹', f.id);

function folderRow(f) {
  const itemCount = nonNegativeInt(f.media_count);
  return `
    <div class="fav-folder" onclick="favEnter(this.dataset.id)" data-id="${escapeAttr(String(f.id ?? ''))}">
      <div class="fav-icon">${escapeHtml((f.title || '?').slice(0, 1))}</div>
      <div>
        <div class="fav-name">${escapeHtml(f.title)}</div>
        <div class="fav-count">${itemCount} 个视频</div>
      </div>
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
  // fid 参数实际传的是文件夹的媒体列表 id(长 id),接口按 media_id 查
  const folder = _favFolders.find(f => String(f.id) === String(fid));
  if (!folder) return;
  _favCurrent = fid;
  showList(folder.title);
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载收藏夹内容...</div>';
  try {
    const r = await getJSON(`/api/fav-content?media_id=${encodeURIComponent(String(fid))}`);
    if (!r.ok) throw new Error(r.error || '加载失败');
    state.items = r.items || [];
    state.checked.clear();
    state.shown = 0;
    renderDefaultList(d);
    d.meta.textContent = `共 ${state.items.length} 条`;
  } catch (e) {
    d.list.innerHTML = `<div class="empty">${escapeHtml(e.message || '加载失败')}<br><button class="ghost sm" onclick="favEnter(this.dataset.id)" data-id="${escapeHtml(String(fid))}">${icon('refresh')} 重试</button></div>`;
  }
}

export function favBack() {
  favLoadFolders();
}

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
  reset: () => { _favCurrent = null; showFolders(); },
  emptyHint: (q) => q ? '没有匹配结果(试试其他关键词)' : '这里还没有收藏任何视频',
});
