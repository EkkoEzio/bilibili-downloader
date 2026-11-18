/* 主题:light / dark / auto(跟随系统)。localStorage 持久化,设置里可改。 */
import { icon } from './icons.js';
import { postJSON } from './net.js';

export function applyTheme(theme) {
  if (theme === 'auto') {
    document.body.classList.remove('dark');
    const sysDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (sysDark) document.body.classList.add('dark');
  } else if (theme === 'dark') {
    document.body.classList.add('dark');
  } else {
    document.body.classList.remove('dark');
  }
  syncThemeBtn();
}

/* 顶栏昼夜按钮跟随当前实际状态:暗色亮太阳(点击回亮色),亮色亮月亮(点击进暗色) */
function syncThemeBtn() {
  const btn = document.getElementById('tb-theme');
  if (!btn) return;
  const dark = document.body.classList.contains('dark');
  btn.innerHTML = icon(dark ? 'sun' : 'moon');
  btn.title = dark ? '切换为亮色' : '切换为暗色';
}

/* 点击顶栏昼夜切换:按当前呈现的明/暗直接取反(auto 也落成明确值),
   与设置抽屉的「外观」select 及 /api/config 保持一致。 */
export function toggleTheme() {
  const next = document.body.classList.contains('dark') ? 'light' : 'dark';
  localStorage.setItem('bd_theme', next);
  applyTheme(next);
  const sel = document.getElementById('config-theme');
  if (sel) sel.value = next;
  postJSON('/api/config', { theme: next }).catch(() => {});
}
