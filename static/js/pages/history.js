/* 观看历史:服务器分页(100/页)+ 本地无限滚;滚到底自动向服务器要更早的。 */
import { registerPage, state, libDom, renderPage, getLibSearchQuery, curChunk } from '../lib.js';
import { getJSON } from '../net.js';
import { icon } from '../icons.js';

let _histServerPage = 0;
let _histHasMore = false;
let _histLoading = false;

export async function loadHistory() {
  const d = libDom();
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>正在获取...</div>';
  d.meta.textContent = '';
  state.items = [];
  state.shown = 0;
  _histServerPage = 0;
  await fetchHistoryPage();
}

async function fetchHistoryPage() {
  try {
    const r = await getJSON(`/api/history?page=${_histServerPage + 1}&size=100`);
    if (!r.ok) throw new Error(r.error || '加载失败');
    _histServerPage++;
    const items = r.items || [];
    _histHasMore = items.length === 100;
    if (_histServerPage === 1) {
      state.items = items;
    } else {
      const seen = new Set(state.items.map(it => it.bvid));
      state.items = state.items.concat(items.filter(it => !seen.has(it.bvid)));
    }
    renderPage();
  } catch (e) {
    libDom().list.innerHTML =
      `<div class="empty">${e.message}<br><button class="ghost sm" onclick="loadHistory()">${icon('refresh')} 重新加载</button></div>`;
    _histHasMore = false;
  }
}

/* 滚到底自动加载更早历史;没有更多/正在加载/搜索中时不触发 */
export async function maybeLoadMoreHistory() {
  if (state.mode !== 'history' || !_histHasMore || _histLoading) return;
  if (getLibSearchQuery()) return;
  _histLoading = true;
  state.shown += curChunk();
  await fetchHistoryPage();
  _histLoading = false;
}

registerPage('history', {
  load: loadHistory,
  retry: loadHistory,
  showCount: false,
  fetchMore: maybeLoadMoreHistory,
});
