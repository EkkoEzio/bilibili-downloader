/* 设置（账号抽屉下半部分）：改动即生效，无保存按钮。 */
import { getJSON, postJSON } from './net.js';
import { applyTheme } from './theme.js';
import { toast } from './confirm.js';

export let DL_DIR = '~/Downloads/bilibili';
let _statusTimer = null;

export function setDlDir(v) { DL_DIR = v; }

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
