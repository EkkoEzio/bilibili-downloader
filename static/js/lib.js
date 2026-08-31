/* 列表页公共框架:页控制器注册表 + 共享状态 + 标准列表渲染 + 无限滚 +
   勾选/批量操作条 + 视图切换 + 搜索 + 「缓存优先→后台拉新」。

   页控制器契约(各页面模块通过 registerPage 注册):
   - load()             进入页面/刷新时的数据加载入口(必需)
   - render(d)          渲染当前视图;缺省用 renderDefaultList(标准视频卡列表)
   - reset()            goPage 进入该页时的复位(回顶层视图、清子状态)
   - chunk()            无限滚每页条数;缺省用视频卡规格
   - infinite           'items'(缺省,标准 _shown 分块) | 'own'(页内自管) | 'none'(无无限滚)
   - advanceChunk()     无限滚推进一块并渲染;返回 false 表示本地没有更多。
                        缺省实现推进 _shown;本地耗尽时若定义了 fetchMore() 则触发它
                        (观看历史向服务器要更早一页)。
   - actionBar          'download'(缺省,批量下载动作组) | 'delete'(下载记录页)
   - actionBarVisible() 操作条是否适用当前视图(缺省 true;收藏夹/合集顶层视图返回 false)
   - emptyHint(q)       空态文案;emptyRetry=false 时空态不带「重新加载」按钮
   - retry()            空态/错误态的重新加载
   - showCount          非搜索时 meta 是否显示「共 N 条」(观看历史为 false)
   - onViewToggle()     列表/网格切换时的额外复位(页内自管的显示条数) */
import { icon } from './icons.js';
import { escapeHtml, escapeAttr, sig, formatDuration, formatTime,
         safeHttpUrl, safeDecimalId } from './util.js';
import { getJSON } from './net.js';

/* ---------- 共享状态(所有标准列表页共用) ---------- */
export const state = {
  mode: 'history',                                            // 当前页
  view: localStorage.getItem('bd_lib_view') || 'grid',        // list | grid
  items: [],          // 当前页数据(标准列表形态)
  shown: 0,           // 无限滚已渲染条数
  checked: new Set(), // 勾选的原始索引
};

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
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.page === name));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.dataset.page === name));
  document.querySelector('.content').scrollTop = 0;  // 换页回顶
  if (LIB_PAGES.includes(name)) {
    state.mode = name;
    state.items = [];
    state.shown = 0;
    state.checked.clear();
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
/* 列数按 CSS auto-fill 规则由容器宽度推,行数按视口高度推。
   网格首屏刻意多渲染一行——正好铺满一屏时滚动条不出现,scroll 事件永远
   不触发,无限滚会卡死(列表视图不受影响,由用户自选条数)。
   rowH 为单行卡片高度:视频卡约 232,UP 行卡约 76。 */
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

/* 搜索匹配谓词(标题或 UP 名命中);q 已由 getLibSearchQuery() 做过 trim + toLowerCase */
function matchesQuery(it, q) {
  const t = (it.title || '').toLowerCase();
  const u = (it.uploader || '-').toLowerCase();
  return t.includes(q) || u.includes(q);
}

/* 当前应渲染的 (条目, 原始下标) 对 —— pairs 构造的唯一来源。
   idx 必须是条目在 state.items 里的原始下标:勾选框的 data-libidx、
   toggleLibCheck、state.checked、delSelected / libDownload 全按原始下标索引
   state.items。曾对过滤后的数组 forEach 取遍历位置当下标(拿到的是「第几个
   命中」),搜索态下删除 / 下载会打到另一条记录 —— 删除还会连带 unlink 那条
   记录的磁盘文件(视频 + 封面 + 字幕 + 弹幕),不可恢复。
   所以这里遍历 state.items 本身,idx 天然就是原始下标;搜索态按谓词筛选,
   非搜索态按 idx < state.shown 截前缀(等价于 slice(0, state.shown))。 */
export function visiblePairs(q) {
  const pairs = [];
  state.items.forEach((it, idx) => {
    if (q ? matchesQuery(it, q) : idx < state.shown) pairs.push({ it, idx });
  });
  return pairs;
}

/* 标准视频卡列表:搜索过滤 + _shown 分块 + meta 计数 + 不足一屏自动补齐。
   pairs 一律走上面那份唯一来源,不在这里自建第二份过滤 / 建对规则。 */
export function renderDefaultList(d = libDom()) {
  if (!d || !d.list) return;
  const ctrl = page();
  const q = getLibSearchQuery();
  /* 这行必须留在取窗之前:非搜索态的入选谓词是 idx < state.shown,
     shown 还是 0 时谓词恒假,首屏会渲染成空列表。旧代码把它放在过滤块之后、
     取窗之前,顺序不可挪动。 */
  if (!state.shown) state.shown = curChunk();
  const pairs = visiblePairs(q);
  renderLibList(pairs, d.list);
  if (d.meta) {
    /* 搜索态的「匹配 N」就是 pairs 的条数:搜索态下 pairs 是全部命中项、
       不受 state.shown 截断,与旧代码单独维护的 filtered.length 同值。 */
    d.meta.textContent = q
      ? `共 ${state.items.length} 条 · 匹配 ${pairs.length}`
      : (ctrl.showCount === false ? '' : `共 ${state.items.length} 条`);
  }
  maybeFillScreen();
}

/* ---------- 无限滚 ---------- */
/* 内容不足一屏时滚动条不出现、scroll 事件不触发,无限滚会卡死;
   这里主动补块直到可滚动或没有更多。 */
let _filling = false;

/* 无限滚模式归一:支持静态字符串或返回字符串的函数 */
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

/* 推进一块:页面自定义了 advanceChunk 且返回值有效时用它(如关注列表的
   自管显示条数),否则走标准 _shown 推进;本地耗尽时触发 fetchMore(如有) */
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
  /* 复用 visiblePairs():「(条目, 原始下标)」这条规则全项目只留一份,
     旧写法 state.items.slice(0, state.shown).map((it, idx) => …) 结果虽然正确
     (从 0 起的前缀切片,位置恰好等于原始下标),但是同一规则的第二份副本,
     下次改规则时漏掉这里就会重新长出 B1 那类下标错位。
     这里只调 visiblePairs() + renderLibList(),**不**调 renderDefaultList():
     renderDefaultList 末尾有 maybeFillScreen(),而 maybeFillScreen 又回调
     advanceDefaultChunk(),会形成 renderDefaultList → maybeFillScreen →
     advanceDefaultChunk 的间接递归 —— _filling 虽然挡得住,但那层保护依赖
     调用路径,脆弱。
     q 取 getLibSearchQuery():两个调用方 maybeFillScreen / maybeLoadMore 都对
     非空搜索词提前返回(搜索态不走无限滚),此处 q 恒为空字符串,
     visiblePairs('') 与旧代码无条件的前缀切片逐位等价。 */
  renderLibList(visiblePairs(getLibSearchQuery()), libDom().list);
  return true;
}

export function scrollToTop() {
  document.querySelector('.content').scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---------- 「缓存优先 → 后台拉新 → 有新数据提示」 ----------
   打开页面立即渲染 /api/cache 的上次数据(绝不干等),同时后台请求真接口;
   拉到的数据和当前展示的不一样时,页面顶部提示「查看最新」,不打断浏览。 */
const _freshApply = {};  // page -> 应用新数据的函数

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
  bar.innerHTML = `<span class="fh-text">获取到新内容</span>
    <button class="primary sm" onclick="applyFresh('${pageName}')">查看最新</button>
    <button class="quiet sm" onclick="dismissFresh('${pageName}')">暂不</button>`;
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

/* opts: { page, api, cacheKey, extract(body)->数组, apply(body), stillValid()->bool,
           fast?:bool(快数据源:拉新后直接无感替换,不弹提示条),
           showLoading()?, showError(msg)? }
   缓存渲染每次都做;同源后台请求在途时不叠加(切走再切回靠缓存立即渲染,
   在途请求完成后按 stillValid 决定直接应用或提示)。
   慢源(关注动态串行几十秒)有变化只提示不打断;快源(合集/收藏夹/稍后再看,
   单接口几百毫秒)提示条反而是噪音——重启后首开必弹一次(基准是上次会话的
   缓存),无感替换才对齐官网「打开即最新」。 */
const _bgInFlight = {};  // cacheKey -> bool

export async function cachedLoad(opts) {
  const { page: pageName, api: apiUrl, cacheKey, extract, apply, stillValid } = opts;
  let shown = null;
  try {
    const r = await getJSON('/api/cache/' + cacheKey);
    if (r.ok && r.data && r.data.ok) {
      shown = extract(r.data);
      if (stillValid()) apply(r.data);
    }
  } catch (e) { /* 无缓存,走加载态 */ }
  if (shown === null && opts.showLoading && stillValid()) opts.showLoading();
  if (_bgInFlight[cacheKey]) return;
  _bgInFlight[cacheKey] = true;
  try {
    const r2 = await getJSON(apiUrl);
    if (!r2.ok) {
      if (shown === null && opts.showError) opts.showError(r2.error || '加载失败');
      return;
    }
    const fresh = extract(r2);
    if (shown === null) { if (stillValid()) apply(r2); return; }
    if (sig(fresh) !== sig(shown) && stillValid()) {
      // 快源无感替换;但用户已勾选条目时绝不静默重渲染(勾选索引会错位),退回提示
      if (opts.fast && state.checked.size === 0) apply(r2);
      else showFreshHint(pageName, () => { if (stillValid()) apply(r2); });
    }
  } catch (e) {
    if (shown === null && opts.showError) opts.showError(e.message);
  } finally { _bgInFlight[cacheKey] = false; }
}

/* ---------- 列表项渲染(标准视频卡) ---------- */
/* 下载记录页的差异项通过 setItemExtras 注入,保持本框架对任务页无感知:
   isProgressCard(it)  是否渲染成进行中任务进度卡
   progressCard(...)   进度卡渲染 (it, isGrid, checkHtml, checkedCls) => html
   warnHtml(it)        条目内的文件名对齐提示条 html
   isDownloaded(url)   「已下载」角标判定
   onChecksEmpty()     勾选清零时的回调(下载页补上被打断的刷新) */
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
      : (q ? '没有匹配结果(试试其他关键词)' : '暂无内容(可能 B 站限流或账号无数据)');
    if (ctrl.emptyRetry === false) {
      container.innerHTML = `<div class="empty">${hint}</div>`;
    } else {
      container.innerHTML = `<div class="empty">${hint}<br><button class="ghost sm" data-lib-retry>${icon('refresh')} 重新加载</button></div>`;
    }
    return;
  }
  const isGrid = state.view === 'grid';
  container.className = 'lib-list' + (isGrid ? ' lib-grid' : '');
  container.innerHTML = pairs.map(({ it, idx: origIdx }) => {
    const checked = origIdx >= 0 && state.checked.has(origIdx);
    const checkedAttr = checked ? 'checked' : '';
    const checkedCls = checked ? ' checked' : '';
    const checkHtml = `<input type="checkbox" data-libidx="${origIdx}" ${checkedAttr} onchange="toggleLibCheck(${origIdx}, this.checked)" class="vi-check">`;
    // 下载记录页:进行中/失败/取消的任务渲染成进度卡(队列已并入)
    if (_itemExtras.isProgressCard(it)) {
      return _itemExtras.progressCard(it, isGrid, checkHtml, checkedCls);
    }
    const badgeHtml = (state.mode !== 'downloads' && _itemExtras.isDownloaded(it.url))
      ? `<span class="dl-badge">${icon('check')}已下载</span>` : '';
    const pct = it.watched_pct || 0;
    const showProgress = it.duration && pct > 0 && pct < 100;
    const watchbar = showProgress
      ? `<div class="vi-watchbar"><i style="width:${Math.min(pct, 100)}%"></i></div>`
      : '';
    const upName = escapeHtml(it.uploader || '-');
    const uploaderMid = safeDecimalId(it.uploader_mid);
    const metaParts = [uploaderMid
      ? `<a href="https://space.bilibili.com/${uploaderMid}" target="_blank" rel="noopener">${upName}</a>`
      : upName];
    if (it.view_at) metaParts.push(escapeHtml(formatTime(it.view_at)));
    const targetUrl = safeHttpUrl(it.url);
    const titleHtml = targetUrl
      ? `<a href="${escapeAttr(targetUrl)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>`
      : escapeHtml(it.title);
    const durHtml = it.duration ? `<span class="thumb-dur">${escapeHtml(formatDuration(it.duration))}</span>` : '';
    const thumbnail = safeHttpUrl(it.thumbnail, { httpsOnly: true });
    // 下载记录:封面右下角播放按钮 → 在 Finder 中定位该视频下载到的文件
    const taskArg = escapeAttr(JSON.stringify(String(it.task_id ?? '')));
    const playHtml = (state.mode === 'downloads' && it.task_id)
      ? `<button class="vi-play" title="在 Finder 中显示该视频" onclick="event.stopPropagation();revealTask(${taskArg})">${icon('play')}</button>` : '';
    const thumbCls = playHtml ? 'thumb-wrap has-play' : 'thumb-wrap';
    const warnHtml = _itemExtras.warnHtml(it);
    if (!isGrid) {
      return `
      <div class="video-item lv${checkedCls}">
        ${checkHtml}
        <div class="${thumbCls}">
          <img src="${escapeAttr(thumbnail)}" onerror="this.style.visibility='hidden'" loading="lazy">
          ${durHtml}${playHtml}
        </div>
        <div class="lv-body">
          <div class="vi-title">${titleHtml} ${badgeHtml}</div>
          <div class="vi-meta">${metaParts.join(' · ')}</div>
          ${watchbar}
          ${warnHtml}
        </div>
      </div>`;
    }
    return `
    <div class="video-item${checkedCls}">
      ${checkHtml}
      <div class="${thumbCls}">
        <img src="${escapeAttr(thumbnail)}" onerror="this.style.visibility='hidden'" loading="lazy">
        ${durHtml}${playHtml}
      </div>
      <div class="vi-title">${titleHtml} ${badgeHtml}</div>
      <div class="vi-meta">${metaParts.join(' · ')}</div>
      ${watchbar}
      ${warnHtml}
    </div>`;
  }).join('');
  updateLibCount();
}

/* ---------- 列表/网格视图切换 ---------- */
export function updateLibViewBtn() {
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.innerHTML = state.view === 'grid' ? icon('list') : icon('grid');
    btn.title = state.view === 'grid' ? '切换为列表视图' : '切换为网格视图';
  });
}

export function toggleLibView() {
  state.view = state.view === 'list' ? 'grid' : 'list';
  localStorage.setItem('bd_lib_view', state.view);
  updateLibViewBtn();
  state.shown = 0;            // 换布局重算首屏
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

export function applyLibFilter() {
  renderPage();
}

/* ---------- 勾选与批量下载(所有列表页统一的底部操作条) ---------- */
export function toggleLibCheck(origIdx, checked) {
  if (checked) state.checked.add(origIdx); else state.checked.delete(origIdx);
  const cb = document.querySelector(`.page.active input[data-libidx="${origIdx}"]`);
  const item = cb ? cb.closest('.video-item') : null;
  if (item) item.classList.toggle('checked', checked);
  updateLibCount();
}

/* 点击封面图切换勾选(勾选框太小不好点)。播放按钮/链接内部已 stopPropagation,不会误触发。 */
document.addEventListener('click', (e) => {
  const thumb = e.target.closest('.video-item .thumb-wrap');
  if (!thumb) return;
  const cb = thumb.closest('.video-item').querySelector('input.vi-check');
  if (!cb) return;
  cb.checked = !cb.checked;  // 先更新 DOM,让 计数/操作条 看到新状态
  toggleLibCheck(parseInt(cb.dataset.libidx), cb.checked);
});

/* 空态「重新加载」按钮(委托,不给内联 onclick 留全局依赖) */
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
    const idx = parseInt(cb.dataset.libidx);
    cb.checked = checked;
    item.classList.toggle('checked', checked);
    if (checked) state.checked.add(idx); else state.checked.delete(idx);
  });
  updateLibCount();
}

export function updateLibCount() {
  const n = document.querySelectorAll('.page.active input[data-libidx]:checked').length;
  document.querySelectorAll('.lib-count').forEach(el => el.textContent = n);
  // 下载页勾选期间被打断的列表刷新,清空勾选后补上
  if (n === 0 && _itemExtras.onChecksEmpty) _itemExtras.onChecksEmpty();
  updateActionBar();
}

export function updateActionBar() {
  const bar = document.getElementById('lib-action-bar');
  const ctrl = page();
  const onLibPage = !!ctrl
    && (ctrl.actionBarVisible ? ctrl.actionBarVisible() : true)
    && !!document.querySelector(`.page.active[data-page="${state.mode}"]`);
  const n = document.querySelectorAll('.page.active input[data-libidx]:checked').length;
  bar.classList.toggle('show', !!onLibPage && n > 0);
  // 按页面切换动作组:下载记录页勾选的是任务/记录 → 删除;其它页 → 批量下载
  const isDelete = ctrl && ctrl.actionBar === 'delete';
  const show = (id, on) => { const el = document.getElementById(id); if (el) el.style.display = on ? '' : 'none'; };
  show('ab-quality', !isDelete);
  show('ab-download-btn', !isDelete);
  show('ab-del-rec', isDelete);
  show('ab-del-file', isDelete);
}

