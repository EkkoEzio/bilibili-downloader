/* 搜索栏:把页面顶部的输入框变成真正的站内搜索。

   原来那行输入框只是本地过滤当前列表(输入什么就筛这一页的条目),
   想要「搜整个 B 站」是做不到的。现在:
     · 边打字仍然本地过滤,响应即时、不打网络;
     · 回车 → 调 /api/search 拿站内结果,直接铺进同一个列表;
     · 清空输入 或 按 Esc → 退出搜索模式,回到原来的列表。

   同一个模块服务两个页面:首页推荐搜视频、关注 UP 搜用户。
   目标页只需通过 registerSearchTarget 提供两个能力:进入/退出搜索模式。
*/

const targets = new Map();

/* pageName → { kind, apply(items, q), exit() } */
export function registerSearchTarget(pageName, target) {
  targets.set(pageName, target);
}

/* 会话内结果缓存:同页同词直接复用上次结果,连后端的 5 分钟缓存都不用打。
   典型场景:搜过「傅立叶」清空换词,再改回来——0ms 上屏。 */
const searchCache = new Map();
const SEARCH_CACHE_MAX = 30;

function cacheKey(pageName, kind, q) {
  return pageName + ':' + kind + ':' + q;
}

function cacheGet(key) {
  return searchCache.get(key) || null;
}

function cacheSet(key, items) {
  if (searchCache.has(key)) searchCache.delete(key);
  searchCache.set(key, items);
  if (searchCache.size > SEARCH_CACHE_MAX) {
    searchCache.delete(searchCache.keys().next().value);
  }
}

let active = null;          // { page, q } 当前处于搜索模式的页
let inflight = null;

/* 该页是否处于站内搜索结果态(结果已由 /api/search 铺进列表)。
   渲染层用它跳过本地过滤:B 站搜索自带纠错/模糊匹配,搜「傅立叶」
   返回的标题是「傅里叶」,再用原文 includes 过滤会把结果筛空
   (2026-11-17 首页实测 18 条命中被筛成「匹配 0」)。 */
export function isSearchMode(pageName) {
  return !!active && active.page === pageName;
}
let _searchTimer = null;

function currentPage() {
  const page = document.querySelector('.page.active');
  return page ? page.dataset.page : '';
}

function inputFor(pageName) {
  const page = document.querySelector(`.page[data-page="${pageName}"]`);
  return page ? page.querySelector('.lib-search') : null;
}

function setHint(pageName, text) {
  const page = document.querySelector(`.page[data-page="${pageName}"]`);
  if (!page) return;
  let bar = page.querySelector('.search-hint');
  if (!text) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'search-hint';
    (page.querySelector('.sub-info') || page.querySelector('.page-head')).after(bar);
  }
  bar.innerHTML = `<span>${text}</span><button class="quiet" type="button" data-search-exit>退出搜索</button>`;
}

async function runSearch(pageName) {
  const target = targets.get(pageName);
  const input = inputFor(pageName);
  if (!target || !input) return;
  const q = (input.value || '').trim();
  if (!q) { exitSearch(pageName); return; }

  // 会话内同词秒回:命中缓存直接上屏,不再发请求。
  const cached = cacheGet(cacheKey(pageName, target.kind, q));
  if (cached) {
    const nowInput = inputFor(pageName);
    if (nowInput && (nowInput.value || '').trim() === q) {
      active = { page: pageName, q };
      target.apply(cached, q);
      setHint(pageName, '搜索「' + q + '」· 命中 ' + cached.length + ' 个结果');
      return;
    }
  }

  if (inflight) inflight.abort();
  const controller = new AbortController();
  inflight = controller;
  setHint(pageName, `搜索「${q}」中…`);

  try {
    const url = `/api/search?type=${encodeURIComponent(target.kind)}`
      + `&q=${encodeURIComponent(q)}&page=1`;
    const response = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok || !payload || !payload.ok) {
      setHint(pageName, payload && payload.error ? payload.error : '搜索失败');
      return;
    }
    // 用户可能已清空输入或换了关键词,旧结果不覆盖
    const currentInput = inputFor(pageName);
    if (!currentInput || (currentInput.value || '').trim() !== q) return;
    active = { page: pageName, q };
    const items = payload.items || [];
    cacheSet(cacheKey(pageName, target.kind, q), items);
    target.apply(items, q);
    setHint(pageName, `搜索「${q}」· 命中 ${items.length} 个结果`);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    setHint(pageName, '搜索失败,请稍后再试');
  } finally {
    if (inflight === controller) inflight = null;
  }
}

export function exitSearch(pageName = active?.page) {
  if (!pageName) return;
  const target = targets.get(pageName);
  setHint(pageName, '');
  if (active && active.page === pageName) {
    active = null;
    if (target && target.exit) target.exit();
  }
}

/* 回车触发搜索;Esc 退出;清空输入也退出 */
document.addEventListener('keydown', (event) => {
  const input = event.target.closest?.('.lib-search');
  if (!input) return;
  const pageName = input.closest('.page')?.dataset.page || '';
  if (!targets.has(pageName)) return;
  if (event.key === 'Enter') {
    event.preventDefault();
    runSearch(pageName);
  } else if (event.key === 'Escape') {
    input.value = '';
    exitSearch(pageName);
  }
});

function onSearchInput(event) {
  const input = event.target.closest?.('.lib-search');
  if (!input) return;
  const pageName = input.closest('.page')?.dataset.page || '';
  const q = (input.value || '').trim();

  // 清除按钮显隐
  const wrap = input.closest('.search-wrap');
  if (wrap) {
    const clear = wrap.querySelector('.search-clear');
    if (clear) clear.hidden = !input.value;
  }

  if (!q) {
    window._bdSearchActive = false;
    exitSearch(pageName);
    return;
  }

  // 有搜索目标的页面:防抖自动搜索(不用等回车)。250ms 是打字停顿与
  // 出结果速度的折中:再大感知明显变慢,再小连续拼音上屏会打出多余请求。
  if (targets.has(pageName)) {
    window._bdSearchActive = true;
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => {
      window._bdSearchActive = false;
      runSearch(pageName);
    }, 250);
  }
}

/* 输入监听必须用捕获阶段:页面的内联 oninput="applyLibFilter()" 在目标
   阶段先于 document 冒泡监听执行。捕获置位 _bdSearchActive 之后,
   applyLibFilter 才会让位给站内搜索;否则第一个按键仍会按原文调度
   一次本地过滤重绘,输入过程中列表先闪一次空。 */
document.addEventListener('input', onSearchInput, true);

document.addEventListener('click', (event) => {
  if (!event.target.closest('[data-search-exit]')) return;
  const pageName = currentPage();
  const input = inputFor(pageName);
  if (input) input.value = '';
  exitSearch(pageName);
});
