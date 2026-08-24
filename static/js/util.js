/* 纯工具:格式化 / 转义 / 提取。无 DOM 依赖。 */

export function formatDuration(sec) {
  sec = Math.floor(sec || 0);
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m >= 60) { const h = Math.floor(m / 60); return h + ':' + String(m % 60).padStart(2, '0') + ':' + String(s).padStart(2, '0'); }
  return m + ':' + String(s).padStart(2, '0');
}

export function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
  if (diff < 604800) return Math.floor(diff / 86400) + '天前';
  return (d.getMonth() + 1) + '/' + d.getDate();
}

export function formatBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + units[i];
}

export function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
export function escapeAttr(s) { return escapeHtml(s); }

export function bvFromUrl(url) {
  const m = /BV[0-9A-Za-z]{10}/.exec(url || '');
  return m ? m[0] : null;
}

/* 缓存优先刷新用的签名:长度 + 首尾标识,够判断"有没有新内容" */
export function sig(arr) {
  if (!Array.isArray(arr) || arr.length === 0) return "0";
  const id = (x) => x.bvid || x.mid || x.id || x.url || "";
  return arr.length + "|" + id(arr[0]) + "|" + id(arr[arr.length - 1]);
}
