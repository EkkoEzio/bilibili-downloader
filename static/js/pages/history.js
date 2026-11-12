/* 观看历史：服务器分页（100/页）+ 本地无限滚；滚到底自动请求更早内容。 */
import { registerPage, state, libDom, renderPage, getLibSearchQuery, curChunk,
         replaceItems, appendItems } from '../lib.js';
import { getJSON, isAbortError } from '../net.js';
import { emptyStateHtml, loadingStateHtml } from '../lib.js';
import { icon } from '../icons.js';
import { escapeHtml } from '../util.js';
import { announce } from '../confirm.js';

let _histServerPage = 0;
let _histHasMore = false;
let _histLoading = false;
let _histGeneration = 0;
let _histController = null;

function cancelHistoryLoad() {
  _histGeneration++;
  if (_histController) _histController.abort();
  _histController = null;
  _histLoading = false;
}

function historyRequestIsCurrent(generation, controller) {
  return generation === _histGeneration
    && controller === _histController
    && !controller.signal.aborted
    && state.mode === 'history';
}

export async function loadHistory() {
  cancelHistoryLoad();
  const generation = _histGeneration;
  const controller = new AbortController();
  _histController = controller;
  _histLoading = true;

  const d = libDom();
  d.list.innerHTML = loadingStateHtml('正在获取…');
  d.meta.textContent = '';
  replaceItems([]);
  state.shown = 0;
  _histServerPage = 0;
  _histHasMore = false;
  try {
    await fetchHistoryPage(generation, controller);
  } finally {
    if (historyRequestIsCurrent(generation, controller)) _histLoading = false;
  }
}

async function fetchHistoryPage(generation, controller) {
  const nextPage = _histServerPage + 1;
  try {
    const r = await getJSON(`/api/history?page=${nextPage}&size=100`, {
      signal: controller.signal,
    });
    if (!historyRequestIsCurrent(generation, controller)) return false;
    if (!r.ok) throw new Error(r.error || '加载失败');

    const items = Array.isArray(r.items) ? r.items : [];
    _histServerPage = nextPage;
    _histHasMore = items.length === 100;
    if (nextPage === 1) replaceItems(items);
    else appendItems(items);
    renderPage();
    return true;
  } catch (error) {
    if (isAbortError(error) || !historyRequestIsCurrent(generation, controller)) return false;
    if (nextPage === 1) {
      libDom().list.innerHTML = emptyStateHtml(escapeHtml(error.message || '加载失败'), { retry: 'loadHistory()' });
      _histHasMore = false;
    } else {
      // 翻页失败:保留已渲染的首页内容,不把整列表换成错误占位;
      // _histHasMore 保持 true,下次滚到底自动重试这一页。
      announce(error.message || '加载更多失败，稍后滚动到底部可重试', true);
    }
    return false;
  }
}

export async function maybeLoadMoreHistory() {
  if (state.mode !== 'history' || !_histHasMore || _histLoading || !_histController) return;
  if (getLibSearchQuery()) return;
  const generation = _histGeneration;
  const controller = _histController;
  _histLoading = true;
  state.shown += curChunk();
  try {
    await fetchHistoryPage(generation, controller);
  } finally {
    if (historyRequestIsCurrent(generation, controller)) _histLoading = false;
  }
}

registerPage('history', {
  load: loadHistory,
  retry: loadHistory,
  // 上游 /x/v2/history 只回当页列表、给不出总数,所以不写「共 N 条」——
  // 但也不能整行留空:原来 showCount:false 会在页头下面留一条 32px 的空白带,
  // 看着像统计坏了。改成说清楚"已加载多少、还有没有更早"。
  metaText: (q, pairs) => {
    const loaded = state.items.length;
    if (q) return `已加载 ${loaded} 条 · 匹配 ${pairs.length}`;
    return _histHasMore ? `已加载 ${loaded} 条 · 继续下滑加载更早` : `已加载 ${loaded} 条`;
  },
  fetchMore: maybeLoadMoreHistory,
  deactivate: cancelHistoryLoad,
});
