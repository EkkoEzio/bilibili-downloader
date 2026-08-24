/* 设置(账号抽屉下半部分):改动即生效,无保存按钮。 */
import { getJSON, postJSON } from './net.js';
import { applyTheme } from './theme.js';

export let DL_DIR = '~/Downloads/bilibili';

export function setDlDir(v) { DL_DIR = v; }

export function loadConfigIntoUI() {
  getJSON('/api/config').then(r => {
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
    return r;
  } catch (e) { alert('保存失败: ' + e.message); }
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
  } catch (e) { /* 入口兜底,失败用默认 */ }
}
