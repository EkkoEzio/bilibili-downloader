/* 关注动态页:只保留关注 UP 的投稿流(浏览行为)。
   UP 订阅管理(加 UP / 自动下载开关 / 取关回关)已独立到「关注 UP」页。 */
import { registerPage, state, libDom, renderDefaultList, renderPage, cachedLoad,
         dismissFresh, replaceItems } from '../lib.js';
import { icon } from '../icons.js';
import { escapeHtml } from '../util.js';

export async function loadFollowBili() {
  await cachedLoad({
    page: 'feed',
    api: '/api/follow-updates',
    cacheKey: 'follow_updates',
    extract: (b) => b.items || [],
    stillValid: () => state.mode === 'feed',
    showLoading: () => {
      const d = libDom();
      d.meta.textContent = '';
      d.list.innerHTML = '<div class="empty">正在后台拉取关注 UP 的近期投稿（串行防限流，约需数十秒），完成后自动展示。</div>';
    },
    showError: (msg) => {
      libDom().list.innerHTML = `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost sm" onclick="loadFollowBili()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      replaceItems(b.items || []);
      renderPage();
      libDom().meta.textContent = `共 ${state.items.length} 条`;
    },
  });
}

function deactivateFeed() {
  dismissFresh('feed');
}

registerPage('feed', {
  load: loadFollowBili,
  retry: loadFollowBili,
  render: (d) => renderDefaultList(d),
  infinite: 'items',
  actionBar: 'download',
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '暂无内容（可能 B 站限流或账号无数据）',
  deactivate: deactivateFeed,
});
