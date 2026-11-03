/* 列表页公共框架：页控制器注册表 + 稳定业务 ID 状态 + 标准列表渲染 +
   无限滚 + 勾选/批量操作条 + 视图切换 + 搜索 +「缓存优先→后台拉新」。

   页控制器契约（各页面模块通过 registerPage 注册）：
   - load()             进入页面/刷新时的数据加载入口（必需）
   - render(d)          渲染当前视图；缺省用 renderDefaultList
   - reset()            goPage 进入该页时的复位
   - chunk()            无限滚每页条数
   - infinite           'items' | 'own' | 'none'
   - advanceChunk()     无限滚推进一块，返回 false 表示本地没有更多
   - actionBar          'download' | 'delete'
   - actionBarVisible() 操作条是否适用当前视图
   - emptyHint(q)       空态文案
   - retry()            空态/错误态的重新加载
   - showCount          是否显示总数
   - metaText(q, pairs)  自定义统计行文案(缺省「共 N 条」;用于总数不可得的页面)
   - onViewToggle()     列表/网格切换时的额外复位 */
import { icon } from './icons.js';
import { escapeHtml, escapeAttr, sig, formatDuration, formatTime,
         safeHttpUrl, safeDecimalId, bvFromUrl, pageFromUrl } from './util.js';
import { getJSON, isAbortError } from './net.js';

/* ---------- 共享状态（所有标准列表页共用） ---------- */
export const state = {
  mode: 'history',
  view: localStorage.getItem('bd_lib_view') || 'grid',
  items: [],                 // 仅负责展示顺序
  itemById: new Map(),       // 稳定业务 ID -> 当前条目
  shown: 0,
  checked: new Set(),        // Set<string>，永不保存数组下标
};

function stableHash(value) {
  let hash = 0x811c9dc5;
  for (let i = 0; i < value.length; i++) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

/* 业务 ID 的唯一来源。标准视频使用 (BV, P)，任务使用不可复用的 task_id；
   其余类型供同一状态模型扩展。fallback 仍由内容确定，不依赖数组位置。 */
export function itemBusinessId(item) {
  const it = item && typeof item === 'object' ? item : {};
  const taskId = String(it.task_id ?? '').trim();
  if (taskId) return `task:${taskId}`;

  const bvid = String(it.bvid || bvFromUrl(it.url) || '').trim();
  if (bvid) {
    const rawPage = Number.parseInt(it.page, 10);
    const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : pageFromUrl(it.url || '');
    return `video:${bvid}#p${page}`;
  }

  const mid = safeDecimalId(it.mid);
  if (mid) return `up:${mid}`;
  if (it.kind === 'folder' && it.id != null) return `folder:${String(it.id)}`;
  if (it.kind === 'season' && it.id != null) return `season:${String(it.id)}`;

  const explicit = String(it.id ?? '').trim();
  if (explicit) return `item:${explicit}`;
  const fingerprint = JSON.stringify([
    String(it.url ?? ''), String(it.title ?? ''), String(it.uploader ?? ''),
    Number(it.duration) || 0,
  ]);
  return `item:${stableHash(fingerprint)}`;
}

/* 替换是所有列表写入的统一入口：重建 Map，并让选择集与新快照求交。
   同一业务 ID 若重复出现，保留第一次的位置、使用最后一次的数据。 */
export function replaceItems(items) {
  const nextItems = [];
  const nextMap = new Map();
  const positions = new Map();
  for (const item of Array.isArray(items) ? items : []) {
    const id = itemBusinessId(item);
    if (positions.has(id)) {
      nextItems[positions.get(id)] = item;
    } else {
      positions.set(id, nextItems.length);
      nextItems.push(item);
    }
    nextMap.set(id, item);
  }
  state.items = nextItems;
  state.itemById = nextMap;
  for (const id of [...state.checked]) {
    if (!nextMap.has(id)) state.checked.delete(id);
  }
  return nextItems;
}

export function appendItems(items) {
  return replaceItems([...state.items, ...(Array.isArray(items) ? items : [])]);
}

export function selectedVisibleItems() {
  return [...document.querySelectorAll('.page.active input[data-libid]:checked')]
    .map((input) => state.itemById.get(String(input.dataset.libid || '')))
    .filter(Boolean);
}

export function libDom() {
  const m = state.mode;
  return {
    list: document.getElementById('list-' + m),
    meta: document.getElementById('meta-' + m),
  };
}

/* ---------- 页控制器注册表 ---------- */
export const LIB_PAGES = [];
const PAGES = {};

export function registerPage(name, ctrl) {
  PAGES[name] = ctrl;
  LIB_PAGES.push(name);
}

export function page(name) { return PAGES[name || state.mode]; }

/* ---------- 页面路由 ---------- */
export function goPage(name, skipLoad) {
  document.querySelectorAll('.nav-item').forEach((item) => {
    const active = item.dataset.page === name;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.dataset.page === name));
  document.querySelector('.content').scrollTop = 0;
  if (LIB_PAGES.includes(name)) {
    const previous = page(state.mode);
    if (previous && previous !== page(name) && previous.deactivate) previous.deactivate();
    cancelCachedLoads();
    state.mode = name;
    replaceItems([]);
    state.shown = 0;
    state.checked.clear();
    // 换页时选择集已清空,操作条必须同步收起:收藏/合集的首屏是卡片而不是
    // 可勾选列表,不主动收起就会把上一个页面(如下载记录)的删除条留在顶部,
    // 盖住新页面的页头。
    updateActionBar();
    const ctrl = page(name);
    if (ctrl.reset) ctrl.reset();
    const pg = document.querySelector(`.page[data-page="${name}"]`);
    const s = pg && pg.querySelector('.lib-search');
    if (s) s.value = '';
    if (!skipLoad) refreshLib();
  }
}

export function refreshLib() {
  const ctrl = page();
  if (ctrl && ctrl.load) ctrl.load();
}

/* ---------- 每页条数自适应 ---------- */
export function computePageSize(minCol = 200, gap = 14, rowH = 232) {
  if (state.view === 'list') {
    return Math.max(10, parseInt(localStorage.getItem('bd_list_size') || '30', 10));
  }
  const d = libDom();
  const w = (d && d.list) ? d.list.clientWidth : 1064;
  const cols = Math.max(2, Math.floor((w + gap) / (minCol + gap)));
  const rows = Math.max(2, Math.floor((window.innerHeight - 150) / rowH) + 1);
  return cols * rows;
}

export function defaultChunk() {
  return state.view === 'list' ? computePageSize() : computePageSize(200, 14, 232);
}

export function upCardChunk() {
  return state.view === 'list' ? computePageSize() : computePageSize(240, 12, 76);
}

export function curChunk() {
  const ctrl = page();
  return ctrl.chunk ? ctrl.chunk() : defaultChunk();
}

/* ---------- 渲染分发 ---------- */
export function renderPage() {
  const d = libDom();
  if (!d.list) return;
  const ctrl = page();
  (ctrl.render || renderDefaultList)(d);
}

function matchesQuery(it, q) {
  const t = (it.title || '').toLowerCase();
  const u = (it.uploader || '-').toLowerCase();
  return t.includes(q) || u.includes(q);
}

/* 当前应渲染的 (条目, 稳定业务 ID) 对。遍历 state.items 只负责维持顺序，
   选择与反查均不再依赖位置；搜索或刷新重排不会把操作转移到另一条记录。 */
export function visiblePairs(q) {
  const pairs = [];
  state.items.forEach((it, index) => {
    if (q ? matchesQuery(it, q) : index < state.shown) {
      pairs.push({ it, id: itemBusinessId(it) });
    }
  });
  return pairs;
}

export function renderDefaultList(d = libDom()) {
  if (!d || !d.list) return;
  const ctrl = page();
  const q = getLibSearchQuery();
  if (!state.shown) state.shown = curChunk();
  const pairs = visiblePairs(q);
  renderLibList(pairs, d.list);
  if (d.meta) {
    // metaText 给"总数拿不到"的页面自己定义统计口径(如观看历史是服务端分页,
    // 上游只回当页列表)。缺省仍是「共 N 条」;showCount:false 会留出一整行空白,
    // 只在确实没话可说时才用。
    if (typeof ctrl.metaText === 'function') d.meta.textContent = ctrl.metaText(q, pairs);
    else d.meta.textContent = q
      ? `共 ${state.items.length} 条 · 匹配 ${pairs.length}`
      : (ctrl.showCount === false ? '' : `共 ${state.items.length} 条`);
  }
  maybeFillScreen();
}

/* ---------- 无限滚 ---------- */
let _filling = false;

function infiniteMode() {
  const ctrl = page();
  const v = ctrl ? (typeof ctrl.infinite === 'function' ? ctrl.infinite() : ctrl.infinite) : 'none';
  return v || 'items';
}

export function maybeFillScreen() {
  if (_filling || getLibSearchQuery()) return;
  if (infiniteMode() === 'none') return;
  const sc = document.querySelector('.content');
  if (!sc || sc.scrollHeight > sc.clientHeight + 2) return;
  _filling = true;
  try {
    for (let i = 0; i < 10 && sc.scrollHeight <= sc.clientHeight + 2; i++) {
      if (!advanceDefaultChunk()) break;
    }
  } finally { _filling = false; }
}

export function maybeLoadMore() {
  if (getLibSearchQuery()) return;
  if (infiniteMode() === 'none') return;
  advanceDefaultChunk();
}

export function advanceDefaultChunk() {
  const ctrl = page();
  if (typeof ctrl.advanceChunk === 'function') {
    const r = ctrl.advanceChunk();
    if (r !== undefined) return r;
  }
  if (state.shown >= state.items.length) {
    if (typeof ctrl.fetchMore === 'function') ctrl.fetchMore();
    return false;
  }
  state.shown += curChunk();
  renderLibList(visiblePairs(getLibSearchQuery()), libDom().list);
  return true;
}

export function scrollToTop() {
  document.querySelector('.content').scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---------- 「缓存优先 → 后台拉新 → 有新数据提示」 ---------- */
const _freshApply = {};

export function showFreshHint(pageName, applyFn) {
  const pg = document.querySelector(`.page[data-page="${pageName}"]`);
  if (!pg) return;
  _freshApply[pageName] = applyFn;
  let bar = pg.querySelector('.fresh-hint');
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'fresh-hint';
    (pg.querySelector('.sub-info') || pg.querySelector('.page-head')).after(bar);
  }
  bar.setAttribute('role', 'status');
  bar.setAttribute('aria-live', 'polite');
  bar.innerHTML = `<span class="fh-text">获取到新内容</span>
    <button class="primary" onclick="applyFresh('${pageName}')">查看最新</button>
    <button class="quiet" onclick="dismissFresh('${pageName}')">暂不</button>`;
  bar.style.display = '';
}

export function applyFresh(pageName) {
  const fn = _freshApply[pageName];
  dismissFresh(pageName);
  if (fn) fn();
}

export function dismissFresh(pageName) {
  const pg = document.querySelector(`.page[data-page="${pageName}"]`);
  const bar = pg && pg.querySelector('.fresh-hint');
  if (bar) bar.style.display = 'none';
  delete _freshApply[pageName];
}

/* 每个 cacheKey 只有一个请求代际。新调用、换页或切换同页数据源都会取消旧请求；
   abort 与 generation 双保险，旧 response/catch/finally 均无权提交 UI。 */
const _bgInFlight = new Map();
const _bgGeneration = new Map();

export function cancelCachedLoads(pageName = null) {
  for (const [cacheKey, slot] of _bgInFlight) {
    if (pageName === null || slot.page === pageName) {
      slot.controller.abort();
      _bgInFlight.delete(cacheKey);
    }
  }
}

export async function cachedLoad(opts) {
  const { page: pageName, api: apiUrl, cacheKey, extract, apply, stillValid } = opts;
  const previous = _bgInFlight.get(cacheKey);
  if (previous) previous.controller.abort();
  const generation = (_bgGeneration.get(cacheKey) || 0) + 1;
  const controller = new AbortController();
  const slot = { page: pageName, generation, controller };
  _bgGeneration.set(cacheKey, generation);
  _bgInFlight.set(cacheKey, slot);
  dismissFresh(pageName);

  const current = () => _bgGeneration.get(cacheKey) === generation
    && !controller.signal.aborted && stillValid();
  let shown = null;
  try {
    try {
      const cached = await getJSON('/api/cache/' + cacheKey, { signal: controller.signal });
      if (current() && cached.ok && cached.data && cached.data.ok) {
        shown = extract(cached.data);
        apply(cached.data);
      }
    } catch (error) {
      if (isAbortError(error)) return;
    }

    if (shown === null && opts.showLoading && current()) opts.showLoading();
    const freshResponse = await getJSON(apiUrl, { signal: controller.signal });
    if (!current()) return;
    if (!freshResponse.ok) {
      if (shown === null && opts.showError) opts.showError(freshResponse.error || '加载失败');
      return;
    }

    const fresh = extract(freshResponse);
    if (shown === null) {
      if (current()) apply(freshResponse);
      return;
    }
    if (sig(fresh) !== sig(shown) && current()) {
      if (opts.fast && state.checked.size === 0) {
        apply(freshResponse);
      } else {
        showFreshHint(pageName, () => {
          if (_bgGeneration.get(cacheKey) === generation && stillValid()) apply(freshResponse);
        });
      }
    }
  } catch (error) {
    if (!isAbortError(error) && shown === null && opts.showError && current()) {
      opts.showError(error.message || '加载失败');
    }
  } finally {
    const active = _bgInFlight.get(cacheKey);
    if (active && active.generation === generation) _bgInFlight.delete(cacheKey);
  }
}

/* ---------- 列表项渲染（标准视频卡） ---------- */
const _itemExtras = {
  isProgressCard: () => false,
  progressCard: null,
  warnHtml: () => '',
  isDownloaded: () => false,
  onChecksEmpty: null,
};

export function setItemExtras(x) { Object.assign(_itemExtras, x); }
export function isUrlDownloaded(url) { return _itemExtras.isDownloaded(url); }

export function renderLibList(pairs, container) {
  if (pairs.length === 0) {
    const ctrl = page();
    const q = getLibSearchQuery();
    const hint = ctrl.emptyHint ? ctrl.emptyHint(q)
      : (q ? '没有匹配结果（试试其他关键词）' : '暂无内容（可能 B 站限流或账号无数据）');
    if (ctrl.emptyRetry === false) {
      container.innerHTML = `<div class="empty">${hint}</div>`;
    } else {
      container.innerHTML = `<div class="empty">${hint}<br><button class="ghost" data-lib-retry>${icon('refresh')} 重新加载</button></div>`;
    }
    return;
  }
  const isGrid = state.view === 'grid';
  container.className = 'lib-list' + (isGrid ? ' lib-grid' : '');
  container.innerHTML = pairs.map(({ it, id }) => {
    const libId = String(id);
    const checked = state.checked.has(libId);
    const checkedAttr = checked ? 'checked' : '';
    const checkedCls = checked ? ' checked' : '';
    const itemLabel = escapeAttr(it.title || '未命名视频');
    const checkHtml = `<label class="vi-check-hit"><input type="checkbox" data-libid="${escapeAttr(libId)}" ${checkedAttr} aria-label="选择：${itemLabel}" onchange="toggleLibCheck(this.dataset.libid, this.checked)" class="vi-check"></label>`;
    if (_itemExtras.isProgressCard(it)) {
      return _itemExtras.progressCard(it, isGrid, checkHtml, checkedCls);
    }
    const badgeHtml = (state.mode !== 'downloads' && _itemExtras.isDownloaded(it.url))
      ? `<span class="dl-badge">${icon('check')}已下载</span>` : '';
    const pct = it.watched_pct || 0;
    const showProgress = it.duration && pct > 0 && pct < 100;
    const watchbar = showProgress
      ? `<div class="vi-watchbar" role="progressbar" aria-label="观看进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(pct, 100)}"><i style="width:${Math.min(pct, 100)}%"></i></div>`
      : '';
    const upName = escapeHtml(it.uploader || '-');
    const uploaderMid = safeDecimalId(it.uploader_mid);
    const identParts = [uploaderMid
      ? `<a href="https://space.bilibili.com/${uploaderMid}" target="_blank" rel="noopener">${upName}</a>`
      : upName];
    if (it.view_at) identParts.push(escapeHtml(formatTime(it.view_at)));
    // 播放/点赞单独成组:它们和上面的身份信息不在同一个换行单元里。
    // 原来四段平铺 + flex-wrap,UP 名字一长就把「点赞」挤到第二行,
    // 同一行卡片因此一高一矮;现在身份区可截断、统计区固定不换行。
    const statParts = [];
    if (it.stat_view) statParts.push(`<span class="vi-stat">${icon('play')} ${escapeHtml(it.stat_view)}</span>`);
    if (it.stat_like) statParts.push(`<span class="vi-stat">${icon('like')} ${escapeHtml(it.stat_like)}</span>`);
    const metaHtml = `<span class="vi-ident">${identParts.join(' · ')}</span>`
      + (statParts.length ? `<span class="vi-stats">${statParts.join('')}</span>` : '');
    const rcmdBadge = (state.mode === 'home' && it.rcmd_reason)
      ? `<span class="rcmd-badge">${escapeHtml(it.rcmd_reason)}</span>` : '';
    const targetUrl = safeHttpUrl(it.url);
    const titleHtml = targetUrl
      ? `<a href="${escapeAttr(targetUrl)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>`
      : escapeHtml(it.title);
    const durHtml = it.duration ? `<span class="thumb-dur">${escapeHtml(formatDuration(it.duration))}</span>` : '';
    const thumbnail = safeHttpUrl(it.thumbnail, { httpsOnly: true });
    const taskArg = escapeAttr(JSON.stringify(String(it.task_id ?? '')));
    const playHtml = (state.mode === 'downloads' && it.task_id)
      ? `<button class="vi-play" title="在 Finder 中显示该视频" aria-label="在 Finder 中显示：${itemLabel}" onclick="event.stopPropagation();revealTask(${taskArg})">${icon('play')}</button>` : '';
    const thumbCls = playHtml ? 'thumb-wrap has-play' : 'thumb-wrap';
    const warnHtml = _itemExtras.warnHtml(it);
    if (!isGrid) {
      return `
      <div class="video-item lv${checkedCls}">
        ${checkHtml}
        <div class="${thumbCls}">
          <img src="${escapeAttr(thumbnail)}" alt="" onerror="this.style.visibility='hidden'" loading="lazy">
          ${durHtml}${playHtml}
        </div>
        <div class="lv-body">
          <div class="vi-title" title="${itemLabel}">${titleHtml} ${rcmdBadge}${badgeHtml}</div>
          <div class="vi-meta">${metaHtml}</div>
          ${watchbar}
          ${warnHtml}
        </div>
      </div>`;
    }
    return `
    <div class="video-item${checkedCls}">
      ${checkHtml}
      <div class="${thumbCls}">
        <img src="${escapeAttr(thumbnail)}" alt="" onerror="this.style.visibility='hidden'" loading="lazy">
        ${durHtml}${playHtml}
      </div>
      <div class="vi-title" title="${itemLabel}">${titleHtml} ${rcmdBadge}${badgeHtml}</div>
      <div class="vi-meta">${metaHtml}</div>
      ${watchbar}
      ${warnHtml}
    </div>`;
  }).join('');
  updateLibCount();
}

/* ---------- 列表/网格视图切换 ---------- */
export function updateLibViewBtn() {
  document.querySelectorAll('.view-btn').forEach(btn => {
    const label = state.view === 'grid' ? '切换为列表视图' : '切换为网格视图';
    btn.innerHTML = state.view === 'grid' ? icon('list') : icon('grid');
    btn.title = label;
    btn.setAttribute('aria-label', label);
  });
}

export function toggleLibView() {
  state.view = state.view === 'list' ? 'grid' : 'list';
  localStorage.setItem('bd_lib_view', state.view);
  updateLibViewBtn();
  state.shown = 0;
  const ctrl = page();
  if (ctrl.onViewToggle) ctrl.onViewToggle();
  applyLibFilter();
}

/* ---------- 搜索过滤 ---------- */
export function getLibSearchQuery() {
  const pg = document.querySelector('.page.active');
  const el = pg && pg.querySelector('.lib-search');
  return el ? el.value.trim().toLowerCase() : '';
}

export function applyLibFilter() { renderPage(); }

/* ---------- 勾选与批量操作 ---------- */
function findVisibleCheckbox(libId) {
  return [...document.querySelectorAll('.page.active input[data-libid]')]
    .find((input) => String(input.dataset.libid || '') === libId) || null;
}

export function toggleLibCheck(libId, checked) {
  const id = String(libId || '');
  const next = !!checked && state.itemById.has(id);
  if (next) state.checked.add(id); else state.checked.delete(id);
  const cb = findVisibleCheckbox(id);
  if (cb) cb.checked = next;
  const item = cb ? cb.closest('.video-item') : null;
  if (item) item.classList.toggle('checked', next);
  updateLibCount();
}

document.addEventListener('click', (e) => {
  const thumb = e.target.closest('.video-item .thumb-wrap');
  if (!thumb) return;
  const cb = thumb.closest('.video-item').querySelector('input.vi-check');
  if (!cb) return;
  cb.checked = !cb.checked;
  toggleLibCheck(cb.dataset.libid, cb.checked);
});

document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-lib-retry]');
  if (!b) return;
  const ctrl = page();
  if (ctrl && ctrl.retry) ctrl.retry();
});

export function libToggleAll(checked) {
  document.querySelectorAll('.page.active .video-item').forEach(item => {
    const cb = item.querySelector('input.vi-check');
    if (!cb) return;
    const id = String(cb.dataset.libid || '');
    cb.checked = checked;
    item.classList.toggle('checked', checked);
    if (checked) state.checked.add(id); else state.checked.delete(id);
  });
  updateLibCount();
}

export function updateLibCount() {
  const n = document.querySelectorAll('.page.active input[data-libid]:checked').length;
  document.querySelectorAll('.lib-count').forEach(el => { el.textContent = n; });
  if (n === 0 && _itemExtras.onChecksEmpty) _itemExtras.onChecksEmpty();
  updateActionBar();
}

export function updateActionBar() {
  const bar = document.getElementById('lib-action-bar');
  const ctrl = page();
  const onLibPage = !!ctrl
    && (ctrl.actionBarVisible ? ctrl.actionBarVisible() : true)
    && !!document.querySelector(`.page.active[data-page="${state.mode}"]`);
  const n = document.querySelectorAll('.page.active input[data-libid]:checked').length;
  const visible = !!onLibPage && n > 0;
  bar.classList.toggle('show', visible);
  document.body.classList.toggle('action-bar-open', visible);
  const isDelete = ctrl && ctrl.actionBar === 'delete';
  const show = (id, on) => {
    const el = document.getElementById(id);
    if (el) el.style.display = on ? '' : 'none';
  };
  show('ab-quality', !isDelete);
  show('ab-download-btn', !isDelete);
  show('ab-del-rec', isDelete);
  show('ab-del-file', isDelete);
}
