/* 追更合集:两级视图——顶层「我追的合集」卡片 / 进入后合集内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad, getLibSearchQuery } from '../lib.js';
import { getJSON } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, nonNegativeInt, safeHttpUrl } from '../util.js';

let _seasons = [];
let _seasonsCurrent = null;  // 已进入的合集 season_id

function showOverview() {
  document.getElementById('season-overview').style.display = '';
  document.getElementById('list-seasons').style.display = 'none';
  document.getElementById('season-back-btn').style.display = 'none';
  document.getElementById('season-page-title').textContent = '追更合集';
  document.getElementById('meta-seasons').textContent =
    _seasons.length ? `共 ${_seasons.length} 个追更中的合集` : '';
}

export async function loadSeasons() {
  _seasonsCurrent = null;
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
        `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost sm" onclick="loadSeasons()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _seasons = b.seasons || [];
      showOverview();
      renderOverview();
    },
  });
}

/* 顶层:合集卡片/行,可按标题搜索 */
function renderOverview() {
  const box = document.getElementById('season-overview');
  if (!box) return;
  const q = getLibSearchQuery();
  let list = _seasons;
  if (q) list = _seasons.filter(s => (s.title || '').toLowerCase().includes(q));
  const metaEl = document.getElementById('meta-seasons');
  if (metaEl) {
    metaEl.textContent = q
      ? `共 ${_seasons.length} 个合集 · 匹配 ${list.length}`
      : (_seasons.length ? `共 ${_seasons.length} 个追更中的合集` : '');
  }
  if (list.length === 0) {
    box.innerHTML = `<div class="empty">${q ? '没有匹配结果(试试其他关键词)' : '还没有追的合集。在 B 站视频页点「追合集」后,会出现在这里。'}</div>`;
    return;
  }
  box.innerHTML = state.view === 'grid'
    ? `<div class="lib-grid">${list.map(seasonCard).join('')}</div>`
    : `<div class="panel"><div class="panel-body tight">${list.map(seasonRow).join('')}</div></div>`;
}

const seasonCard = (s) => {
  const cover = safeHttpUrl(s.cover, { httpsOnly: true });
  const itemCount = nonNegativeInt(s.media_count);
  return `
  <div class="fav-card" onclick="seasonEnter(this.dataset.id)" data-id="${escapeAttr(String(s.id ?? ''))}">
    <div class="fc-cover">
      <img src="${escapeAttr(cover)}" onerror="this.style.visibility='hidden'" loading="lazy">
      <span class="fc-count">${itemCount} 个视频</span>
    </div>
    <div class="fc-title">${escapeHtml(s.title || '(未命名)')}</div>
    <div class="fc-sub">${icon('layers')}追更中</div>
  </div>`;
};

function seasonRow(s) {
  const itemCount = nonNegativeInt(s.media_count);
  return `
    <div class="fav-folder" onclick="seasonEnter(this.dataset.id)" data-id="${escapeAttr(String(s.id ?? ''))}">
      <div class="fav-icon">${icon('layers')}</div>
      <div>
        <div class="fav-name">${escapeHtml(s.title)}<span class="fav-tag">追更中</span></div>
        <div class="fav-count">${itemCount} 个视频</div>
      </div>
      <span class="fav-arrow">${icon('chevron')}</span>
    </div>`;
}

export async function seasonEnter(sid) {
  const season = _seasons.find(s => String(s.id) === String(sid));
  if (!season) return;
  _seasonsCurrent = sid;
  document.getElementById('season-overview').style.display = 'none';
  document.getElementById('list-seasons').style.display = '';
  document.getElementById('season-back-btn').style.display = '';
  document.getElementById('season-page-title').textContent = season.title;
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载合集内容...</div>';
  try {
    const r = await getJSON(`/api/season-content?season_id=${encodeURIComponent(String(sid))}`);
    if (!r.ok) throw new Error(r.error || '加载失败');
    state.items = r.items || [];
    state.checked.clear();
    state.shown = 0;
    renderDefaultList(d);
    d.meta.textContent = `共 ${state.items.length} 条`;
  } catch (e) {
    d.list.innerHTML = `<div class="empty">${escapeHtml(e.message || '加载失败')}<br><button class="ghost sm" onclick="seasonEnter(this.dataset.id)" data-id="${escapeHtml(String(sid))}">${icon('refresh')} 重试</button></div>`;
  }
}

export function seasonBack() {
  loadSeasons();
}

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
  reset: () => { _seasonsCurrent = null; showOverview(); },
  emptyHint: (q) => q ? '没有匹配结果(试试其他关键词)' : '这个合集还没有视频',
});
