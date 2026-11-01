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
  // 未来时间戳(定时发布/本机时钟偏慢)夹到 0:显示「刚刚」级别的
  // 文案,不能出现「-3分钟前」。
  const diff = Math.max(0, (now - d) / 1000);
  if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
  if (diff < 604800) return Math.floor(diff / 86400) + '天前';
  return (d.getMonth() + 1) + '/' + d.getDate();
}

/* 卡片上的「哪一天」:今年内给「M月D日」,跨年补上年份。
   关注时间与最近投稿时间跨度可能是几年,只给 M/D 会把去年和今年混在一起。 */
export function formatDay(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  const d = new Date(n * 1000);
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  return d.getFullYear() === new Date().getFullYear()
    ? md : `${d.getFullYear()}年${md}`;
}

export function formatBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + units[i];
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
export function escapeAttr(s) { return escapeHtml(s); }

/* 只允许可点击/可加载资源使用 HTTP(S)，避免 javascript:/data: 进入 href/src。
   协议相对的 B 站图片统一提升到 HTTPS；凭据型 URL 不进入页面。 */
export function safeHttpUrl(value, { httpsOnly = false } = {}) {
  let raw = String(value ?? '').trim();
  if (!raw) return '';
  if (raw.startsWith('//')) raw = 'https:' + raw;
  try {
    const parsed = new URL(raw);
    if (parsed.username || parsed.password) return '';
    if (httpsOnly && parsed.protocol === 'http:') parsed.protocol = 'https:';
    const allowed = parsed.protocol === 'https:' || (!httpsOnly && parsed.protocol === 'http:');
    if (!allowed) return '';
    return parsed.href;
  } catch (e) {
    return '';
  }
}

/* 远端 ID 只保留正十进制，供 URL path / data-* 使用。 */
export function safeDecimalId(value) {
  const text = String(value ?? '').trim();
  return /^[1-9]\d*$/.test(text) ? text : '';
}

/* 远端计数先数值化再进入 HTML，非有限数/负数归零。 */
export function nonNegativeInt(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.trunc(number) : 0;
}

export function bvFromUrl(url) {
  const m = /BV[0-9A-Za-z]{10}/.exec(url || '');
  return m ? m[0] : null;
}

/* ---------- 已下载判定的键规则((bvid, 分P) 粒度) ----------

   与 bvFromUrl 同层的纯字符串逻辑,是前端键规则的唯一来源:downloads.js
   只 import 不复制第二份(tests 有「downloads.js 不得出现 function dlKey」的断言)。

   两侧取 page 的方式是刻意不对称的,改动前务必读懂:
     - 查询侧(isDownloaded)入参只有 URL,page 只能从 ?p= 解析 → 用 pageFromUrl;
     - 任务侧读 t.page,不能解析 t.url —— 任务表里的 url 是用户粘贴的原始链接
       (通常不带 ?p=),page 才是权威值。
   写反任一侧都会让判定悄悄退回「只看 BV」的缺陷形态,且不会报任何错。 */

/* 已下载判定的键分隔符。必须与后端 extras.DL_KEY_SEP 一致(tests 有绑定断言)
   —— 两侧键规则漂移会让角标与批量跳过各说各话,且不会报错。 */
export const DL_KEY_SEP = '#p';

/* 从视频 URL 取分 P 序号;无 ?p= / 非法值视为 1。语义与后端 extras.page_from_url 对齐。
   带其它查询参数时分 P 以 `&p=` 出现(如 ?spm_id_from=333.999&p=2),故 `[?&]` 两种都认。
   归一到 1 是「不带 ?p= 的单 P URL 仍判已下载」的前提:单 P 视频 URL 不带 ?p=,
   其任务 page 是 1,两边都落到 1 才能对上。 */
export function pageFromUrl(url) {
  const m = /[?&]p=(\d+)/.exec(url || '');
  return m ? parseInt(m[1], 10) : 1;
}

/* 「已下载」判定的唯一键:(bv, 分P) → "BVxxxxxxxxxx#p1"。语义与后端 extras.dl_key 对齐。
   判定粒度必须是 (bv, page):只看 bv 会让合集下完 P1 后其余分 P 全被当已下载而跳过。
   page 缺失 / 0 / null / undefined / NaN 一律归一为 1 —— parseInt 会先把入参转成字符串,
   null / undefined / NaN 都得到 NaN,连同 0 一起被 `|| 1` 兜到 1。 */
export function dlKey(bv, page) {
  return bv + DL_KEY_SEP + (parseInt(page, 10) || 1);
}

/* 缓存优先刷新用的签名:长度 + 首尾标识,够判断"有没有新内容" */
export function sig(arr) {
  if (!Array.isArray(arr) || arr.length === 0) return "0";
  const id = (x) => x.bvid || x.mid || x.id || x.url || "";
  return arr.length + "|" + id(arr[0]) + "|" + id(arr[arr.length - 1]);
}
