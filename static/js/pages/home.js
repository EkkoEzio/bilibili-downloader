/* 首页推荐页：B 站首页个性化推荐流，当 B 站刷。

   数据特征与其它列表页不同：
   - B 站风控时 rcmd 接口不报错、只返回空列表，裸等现拉会让首屏停在空态；
     仍落一份「上次首页」磁盘缓存（pagecache 的 home_feed 键，后端只在
     fresh_idx=1 且非空时写入），打开先出上次内容再后台刷新；
   - 空结果/失败自动重试几次，重试耗尽才落空态/错误态；
   - 翻页用 fresh_idx 递增（B 站推荐流约定），本地已见条目渲染完再拉下一页；
   - 首屏必须铺满视口：按网格容量一次要够条数，不够再补拉，不留半屏空白；
   - 推荐理由 / 播放点赞数是本页独有的卡片信息。 */
import { registerPage, state, libDom, renderPage, appendItems, replaceItems,
         getLibSearchQuery, defaultChunk, cancelCachedLoads, showFreshHint,
         dismissFresh } from '../lib.js';
import { getJSON, isAbortError } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml } from '../util.js';
import { announce } from '../confirm.js';

/* 首屏一次请求的条数：按当前视口网格容量算，夹在接口允许的 12–30 之间。
   B 站 rcmd 接口 ps 上限 30，web 端惯用 12。 */
const MIN_FIRST_PS = 12;
const MAX_FIRST_PS = 30;
/* 首屏补齐上限：一次补齐最多再拉 3 页，防止超长屏无限拉取。 */
const MAX_FILL_ROUNDS = 3;
/* 防御边界：单会话最多翻 200 页（约 2400 条）。 */
const MAX_PAGES = 200;
/* 空结果/失败的自动重试：B 站风控的空列表通常几秒内恢复，自动重试
   替代用户手点「重新加载」。固定间隔与后端 sensitive 接口 1.5s 间距同量级；
   上游给了 Retry-After 时按它等，但一旦超过 GIVE_UP 阈值（412/429 的
   分钟级冷却）就不再空耗，立刻落错误态让用户看到「请约 N 秒后重试」。 */
const AUTO_RETRIES = 3;
const RETRY_DELAY_MS = 2000;
const RETRY_GIVE_UP_MS = 10000;
const HOME_CACHE_KEY = 'home_feed';

let _freshIdx = 0;
let _reachedEnd = false;
let _loading = false;
let _filling = false;
let _generation = 0;
let _controller = null;
/* 本轮会话每页条数。首屏按视口容量算出后固定复用:服务端按
   fetch_row = 1+(fresh_idx-1)*ps 推行偏移,翻页换 ps 会让第二页落到
   首屏已消费的行区间,整页都是已见条目(白白多打一次上游)。 */
let _pagePs = MIN_FIRST_PS;

function requestIsCurrent(generation, controller) {
  return generation === _generation
    && controller === _controller
    && !controller.signal.aborted
    && state.mode === 'home';
}

function itemLibId(it) {
  return `video:${it.bvid}#p${it.page || 1}`;
}

/* 首屏条数：网格容量（列×行），列表视图按列表容量。 */
function firstPageSize() {
  const need = defaultChunk() || 0;
  return Math.max(MIN_FIRST_PS, Math.min(MAX_FIRST_PS, need));
}

/* 内容区是否已经铺满视口：不满足则继续拉，避免底部留白。 */
function viewportFilled() {
  const sc = document.querySelector('.content');
  if (!sc) return true;
  return sc.scrollHeight > sc.clientHeight + 2;
}

/* 拉页：至少 minPages 页，之后只要还没铺满视口就继续拉，最多 MAX_FILL_ROUNDS 页。
   首屏加载完 minPages=0（铺满即停）；滚到底/切视图 minPages=1（保证每次都有新增）。
   _filling 保证同一时刻只有一个循环在跑，避免并发重复请求。 */
async function pullPages(minPages) {
  if (_filling || _reachedEnd) return 0;
  _filling = true;
  let added = 0;
  try {
    for (let i = 0; i < MAX_FILL_ROUNDS; i++) {
      if (_reachedEnd) break;
      if (i >= minPages && viewportFilled()) break;
      const n = await fetchMoreHome();
      if (!n) break;
      added += n;
    }
  } finally {
    _filling = false;
  }
  return added;
}

/* 可中断延时：页面切走/手动重新加载会 abort controller，等待随之取消。 */
function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/* 本次重试等多久：普通空结果/失败固定间隔；上游给了 Retry-After 就按它，
   是否长到值得放弃由调用方对照 RETRY_GIVE_UP_MS 判断。 */
function retryDelayMs(error) {
  const retryAfter = Number(error && error.retryAfter);
  if (Number.isFinite(retryAfter) && retryAfter > 0) {
    return Math.max(RETRY_DELAY_MS, retryAfter * 1000);
  }
  return RETRY_DELAY_MS;
}

function loadingHtml(text) {
  return `<div class="empty"><span class="spinner"></span>${escapeHtml(text)}</div>`;
}

/* 第一页现拉成功后的上屏。有缓存且用户已勾选条目时不打断勾选，
   改走「获取到新内容」提示条（与关注动态同一交互）；否则无缝替换。 */
function applyFirstPage(items, restored, isCurrent) {
  const apply = () => {
    replaceItems(items);
    state.shown = state.items.length;  // 已拉到的条目全部上屏
    renderPage();
    pullPages(0);
  };
  if (restored && state.checked.size > 0) {
    showFreshHint('home', () => { if (isCurrent()) apply(); });
    return;
  }
  apply();
}

export async function loadHomeFeed() {
  cancelHomeLoad();
  _freshIdx = 0;
  _reachedEnd = false;
  // 整个初始阶段(缓存读取 + 现拉 + 重试)占住 _loading,挡住滚动触发的
  // fetchMoreHome,避免它和首屏请求并发拉同一页;成功路径在补拉前先放开。
  // 与上一轮被 abort 的翻页互不干扰:它们的 finally 只在仍持代际时收尾。
  _loading = true;
  _pagePs = firstPageSize();
  const generation = _generation;
  const controller = new AbortController();
  _controller = controller;
  const d = libDom();
  const current = () => requestIsCurrent(generation, controller);
  d.meta.textContent = '';
  d.list.innerHTML = loadingHtml('正在加载首页推荐...');
  dismissFresh('home');
  try {
    // 缓存优先:上次的首页先上屏,后面现拉成功再无缝替换。
    let restored = 0;
    try {
      const cached = await getJSON(`/api/cache/${HOME_CACHE_KEY}`,
                                   { signal: controller.signal });
      if (current() && cached.ok && cached.data && cached.data.ok
          && Array.isArray(cached.data.items) && cached.data.items.length) {
        replaceItems(cached.data.items);
        state.shown = state.items.length;
        renderPage();
        d.meta.textContent = `共 ${state.items.length} 条 · 正在后台刷新…`;
        restored = state.items.length;
      }
    } catch (error) {
      if (isAbortError(error)) return;
      // 缓存读不到不算失败,继续现拉。
    }

    // 现拉第一页:空结果或失败自动重试,重试耗尽才落死端。
    let lastError = null;
    for (let attempt = 0; ; attempt++) {
      try {
        const r = await getJSON(`/api/home-feed?fresh_idx=1&ps=${_pagePs}`,
                                { signal: controller.signal });
        if (!current()) return;
        if (r.ok) {
          const items = Array.isArray(r.items) ? r.items : [];
          _freshIdx = 1;
          if (items.length) {
            _loading = false;  // 首屏落定,放开翻页闸门再补拉
            applyFirstPage(items, restored, current);
            return;
          }
          // ok 但空:B 站风控中的典型表现,继续重试。
        } else {
          lastError = new Error(r.error || '加载失败');
        }
      } catch (error) {
        if (isAbortError(error)) return;
        if (!current()) return;
        lastError = error;
        // 未登录是稳定态,几秒内不会变化,空耗重试只会推迟错误提示。
        if (error && error.code === 'auth_required') break;
      }
      const waitMs = retryDelayMs(lastError);
      if (attempt >= AUTO_RETRIES || waitMs > RETRY_GIVE_UP_MS) break;
      if (!restored) {
        d.list.innerHTML = loadingHtml(
          `正在加载首页推荐...（第 ${attempt + 1}/${AUTO_RETRIES} 次重试）`);
      }
      await delay(waitMs, controller.signal);
    }
    // 重试耗尽:有缓存就留在缓存上,把状态写进计数行;没缓存才落死端提示。
    if (restored) {
      d.meta.textContent = `共 ${state.items.length} 条 · 暂时无法刷新`;
      announce(lastError ? '推荐流刷新失败，先展示之前的内容'
                         : 'B 站暂时没给新推荐，先展示之前的内容');
    } else if (lastError) {
      d.list.innerHTML = `<div class="empty">${escapeHtml(lastError.message || '加载失败')}<br><button class="ghost" onclick="loadHomeFeed()">${icon('refresh')} 重试</button></div>`;
    } else {
      replaceItems([]);
      renderPage();  // 落回标准空态:空态文案 + 重新加载按钮
    }
  } finally {
    // 只有仍持有当前 controller 时才收尾:被新一轮加载取代时,闸门归它管;
    // maybeFillScreen() 触发的 fetchMoreHome 接管 _controller 后,这里也
    // 不能覆盖它的引用,否则页面切走时 abort 不到真正在途的请求。
    if (current()) {
      _controller = null;
      _loading = false;
    }
  }
}

/* 无限滚：本地条目见底时拉下一页并追加（去重）。返回实际新增条数。 */
export async function fetchMoreHome() {
  if (_loading || _reachedEnd || getLibSearchQuery()) return 0;
  if (_freshIdx >= MAX_PAGES) { _reachedEnd = true; return 0; }
  _loading = true;
  _generation++;
  const generation = _generation;
  const controller = new AbortController();
  _controller = controller;
  const nextIdx = _freshIdx + 1;
  try {
    const r = await getJSON(`/api/home-feed?fresh_idx=${nextIdx}&ps=${_pagePs}`,
                            { signal: controller.signal });
    if (!requestIsCurrent(generation, controller)) return 0;
    if (!r.ok) throw new Error(r.error || '加载失败');
    const items = Array.isArray(r.items) ? r.items : [];
    _freshIdx = nextIdx;
    if (!items.length) {
      _reachedEnd = true;
      announce('推荐流没有更多内容了');
      return 0;
    }
    const seen = new Set(state.itemById.keys());
    const fresh = items.filter(it => !seen.has(itemLibId(it)));
    if (!fresh.length) return 0;  // 整页都是已见过的，别再空转
    appendItems(fresh);
    state.shown = state.items.length;
    renderPage();
    return fresh.length;
  } catch (error) {
    if (isAbortError(error) || !requestIsCurrent(generation, controller)) return 0;
    announce(error.message || '加载更多失败', true);
    return 0;
  } finally {
    // 与 loadHomeFeed 同一约定:被新一轮加载/翻页取代(代际已变)时不清
    // _loading,当前持有者会自己置位并收尾;无条件清会把它的闸门冲掉,
    // 初始拉取期间就会放进并发的同页翻页请求。
    if (requestIsCurrent(generation, controller)) {
      _loading = false;
      _controller = null;
    }
  }
}

function cancelHomeLoad() {
  _generation++;
  if (_controller) _controller.abort();
  _controller = null;
}

function deactivateHome() {
  cancelHomeLoad();
  cancelCachedLoads('home');
  dismissFresh('home');
}

registerPage('home', {
  load: loadHomeFeed,
  retry: loadHomeFeed,
  fetchMore: fetchMoreHome,
  // 本地没有"待上屏的隐藏条目"可推进：滚到底/铺不满时拉下一页。
  advanceChunk: () => { pullPages(1); return false; },
  onViewToggle: () => { state.shown = state.items.length; },
  chunk: () => defaultChunk(),
  actionBar: 'download',
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）'
    : '暂无推荐内容（可能 B 站限流或未登录）',
  deactivate: deactivateHome,
});
