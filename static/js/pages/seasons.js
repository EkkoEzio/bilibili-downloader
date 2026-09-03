/* 追更合集：两级视图——顶层「我追的合集」卡片 / 进入后合集内视频列表。 */
import { registerPage, state, libDom, renderDefaultList, cachedLoad,
         getLibSearchQuery, replaceItems } from '../lib.js';
import { getJSON, isAbortError } from '../net.js';
import { icon } from '../icons.js?v=20260903a';
import { escapeHtml, escapeAttr, nonNegativeInt, safeHttpUrl } from '../util.js';

let _seasons = [];
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
  document.getElementById('meta-seasons').textContent =
    _seasons.length ? `共 ${_seasons.length} 个追更中的合集` : '';
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
        `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost sm" onclick="loadSeasons()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _seasons = b.seasons || [];
      showOverview();
      renderOverview();
    },
  });
}

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
    box.innerHTML = `<div class="empty">${q ? '没有匹配结果（试试其他关键词）' : '还没有追的合集。在 B 站视频页点「追合集」后，会出现在这里。'}</div>`;
    return;
  }
  box.innerHTML = state.view === 'grid'
    ? `<div class="lib-grid">${list.map(seasonCard).join('')}</div>`
    : `<div class="panel"><div class="panel-body tight">${list.map(seasonRow).join('')}</div></div>`;
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
  <button type="button" class="fav-card" onclick="seasonEnter(this.dataset.id)" data-id="${escapeAttr(String(s.id ?? ''))}">
    <div class="fc-cover">
      <span class="fc-placeholder" aria-hidden="true">${icon('bilibili')}<span>暂无封面</span></span>
      ${image}
      <span class="fc-count">${itemCount} 个视频</span>
    </div>
    <div class="fc-title">${escapeHtml(s.title || '(未命名)')}</div>
    <div class="fc-sub">${icon('layers')}追更中</div>
  </button>`;
};

function seasonRow(s) {
  const itemCount = nonNegativeInt(s.media_count);
  return `
    <button type="button" class="fav-folder" onclick="seasonEnter(this.dataset.id)" data-id="${escapeAttr(String(s.id ?? ''))}">
      <div class="fav-icon">${icon('layers')}</div>
      <div>
        <div class="fav-name">${escapeHtml(s.title)}<span class="fav-tag">追更中</span></div>
        <div class="fav-count">${itemCount} 个视频</div>
      </div>
      <span class="fav-arrow">${icon('chevron')}</span>
    </button>`;
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
    d.list.innerHTML = `<div class="empty">${escapeHtml(error.message || '加载失败')}<br><button class="ghost sm" onclick="seasonEnter(this.dataset.id)" data-id="${escapeAttr(String(sid))}">${icon('refresh')} 重试</button></div>`;
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
