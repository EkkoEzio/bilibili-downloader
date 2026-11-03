/* 稍后再看：快数据源，缓存优先 + 后台无感替换。 */
import { registerPage, state, libDom, renderPage, cachedLoad, replaceItems } from '../lib.js';
import { icon } from '../icons.js';
import { escapeHtml } from '../util.js';

export async function loadWatchlater() {
  await cachedLoad({
    page: 'later',
    api: '/api/watchlater',
    cacheKey: 'watchlater',
    fast: true,
    extract: (b) => b.items || [],
    stillValid: () => state.mode === 'later',
    showLoading: () => {
      const d = libDom();
      d.meta.textContent = '';
      d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载稍后再看...</div>';
    },
    showError: (msg) => {
      const d = libDom();
      d.list.innerHTML = `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost" onclick="loadWatchlater()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      replaceItems(b.items || []);
      renderPage();
      libDom().meta.textContent = `共 ${state.items.length} 条`;
    },
  });
}

registerPage('later', {
  load: loadWatchlater,
  retry: loadWatchlater,
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '暂无内容（可能 B 站限流或账号无数据）',
});
