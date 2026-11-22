/* 设置（账号抽屉下半部分）：改动即生效，无保存按钮。 */
import { getJSON, postJSON, postEmpty } from './net.js';
import { applyTheme } from './theme.js';
import { toast, confirmAction } from './confirm.js';

export let DL_DIR = '~/Downloads/bilibili';
let _statusTimer = null;

export function setDlDir(v) { DL_DIR = v; }

/* 「退出应用」入口(仅打包版)。
   窗口关掉只是最小化到 Dock,后台照常监控下载;想彻底停掉走这里,
   不用去猜菜单栏那个小图标在哪(2026-11-22 用户报障「退不出去」)。 */
export async function quitApp() {
  const info = await getJSON('/api/app/info').catch(() => null);
  if (!info || !info.packaged) {
    toast('源码运行模式:请在启动终端按 Ctrl-C 退出');
    return;
  }
  const ok = await confirmAction({
    title: '退出应用',
    message: '退出会停掉后台的自动监控与下载队列，正在下载的任务会中断。',
    detail: '确定退出吗？',
    confirmLabel: '退出',
    danger: true,
  });
  if (!ok) return;
  try {
    await postEmpty('/api/app/quit');
    toast('正在退出…');
  } catch (error) {
    toast(error.message || '退出请求失败');
  }
}

/* 打包版才显示「退出应用」那一行。 */
export async function refreshAppExitRow() {
  const row = document.getElementById('app-exit-row');
  if (!row) return;
  const info = await getJSON('/api/app/info').catch(() => null);
  row.hidden = !(info && info.packaged);
}

function formatStatusTime(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '';
  return new Date(timestamp * 1000).toLocaleString('zh-CN', { hour12: false });
}

function renderAutoRefreshStatus(status) {
  const lines = [];
  if (status.running) lines.push('正在检查自动下载清单');
  else if (status.scheduler_running) lines.push('后台定时检查已启动');
  else lines.push('后台定时检查未运行');

  const next = formatStatusTime(status.next_run_at);
  if (next && !status.running) lines.push(`下次检查：${next}`);
  const lastSuccess = formatStatusTime(status.last_success_at);
  if (lastSuccess) lines.push(`上次完成：${lastSuccess}，入队 ${Number(status.last_enqueued) || 0} 条`);
  const partial = Number(status.last_partial_errors) || 0;
  // 这个计数同时包含 UP 与合集/收藏夹订阅的失败,文案不能只写「UP」。
  if (partial) lines.push(`上轮有 ${partial} 项检查失败（UP 或合集），其余已继续处理`);
  if (status.last_error) lines.push(`上轮错误：${status.last_error}`);
  return lines.join(' · ');
}

export async function loadAutoRefreshStatus() {
  const targets = [
    document.getElementById('auto-refresh-status'),
    document.getElementById('auto-refresh-subs-status'),
  ].filter(Boolean);
  if (!targets.length) return null;
  try {
    const status = await getJSON('/api/auto-refresh/status');
    const text = renderAutoRefreshStatus(status);
    targets.forEach((element) => { element.textContent = text; });
    return status;
  } catch (error) {
    targets.forEach((element) => {
      element.textContent = `自动检查状态读取失败：${error.message || '请求失败'}`;
    });
    return null;
  }
}

export function loadConfigIntoUI() {
  loadAutoRefreshStatus();
  return getJSON('/api/config').then(r => {
    if (!r.ok) return;
    document.getElementById('config-dir-input').value = r.download_dir || DL_DIR;
    document.getElementById('config-concurrent').value = String(r.max_concurrent || 2);
    document.getElementById('config-ratelimit').value = String(r.ratelimit_mbps || 0);
    const theme = r.theme || localStorage.getItem('bd_theme') || 'auto';
    const el = document.getElementById('config-theme');
    if (el) el.value = theme;
    const checks = {
      'config-save-danmaku': r.save_danmaku,
      'config-save-subtitle': r.save_subtitle,
      'config-save-cover': r.save_cover,
      'config-sort-by-up': r.sort_by_up,
      'config-notify': r.notify,
    };
    for (const [id, v] of Object.entries(checks)) {
      const c = document.getElementById(id);
      if (c) c.checked = !!v;
    }
    const interval = document.getElementById('config-auto-interval');
    if (interval) interval.value = String(r.auto_refresh_minutes || 30);
  }).catch((error) => {
    const status = document.getElementById('auto-refresh-status');
    if (status) status.textContent = `设置读取失败：${error.message || '请求失败'}`;
  });
}

function flashSaved() {
  const el = document.getElementById('save-flash');
  if (!el) return;
  el.classList.add('show');
  clearTimeout(flashSaved._t);
  flashSaved._t = setTimeout(() => el.classList.remove('show'), 1400);
}

export async function saveConfigField(patch) {
  try {
    const r = await postJSON('/api/config', patch);
    if (!r.ok) throw new Error(r.error || '保存失败');
    if ('download_dir' in patch) setDlDir(r.download_dir);
    flashSaved();
    if ('auto_refresh_minutes' in patch) loadAutoRefreshStatus();
    return r;
  } catch (e) { toast('保存失败: ' + e.message); }
}

export function saveChk(name, el) {
  const patch = {};
  patch[name] = el.checked;
  saveConfigField(patch);
}

export function saveDir() {
  const v = document.getElementById('config-dir-input').value.trim();
  if (v) saveConfigField({ download_dir: v });
}

export function saveTheme(v) {
  localStorage.setItem('bd_theme', v);
  applyTheme(v);
  saveConfigField({ theme: v });
}

export function resetConfigDir() {
  document.getElementById('config-dir-input').value = '~/Downloads/bilibili';
  saveConfigField({ download_dir: '~/Downloads/bilibili' });
}

export async function loadConfig() {
  try {
    const r = await getJSON('/api/config');
    if (r.ok) setDlDir(r.download_dir);
  } catch (e) { /* 入口兜底，失败用默认 */ }
}

document.addEventListener('drawer:opened', (event) => {
  if (!event.detail || event.detail.name !== 'account') return;
  clearInterval(_statusTimer);
  _statusTimer = window.setInterval(loadAutoRefreshStatus, 5000);
});
document.addEventListener('drawer:closed', () => {
  clearInterval(_statusTimer);
  _statusTimer = null;
});
