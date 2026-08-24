/* 主题:light / dark / auto(跟随系统)。localStorage 持久化,设置里可改。 */
import { icon } from './icons.js';

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
  const btn = document.getElementById('theme-btn');
  if (btn) {
    const isDark = document.body.classList.contains('dark');
    btn.innerHTML = (isDark ? icon('sun') : icon('moon')) + '<span>' + (isDark ? '浅色模式' : '深色模式') + '</span>';
    btn.title = isDark ? '当前深色,点击切换' : '当前浅色,点击切换';
  }
}

export function toggleTheme() {
  const cur = localStorage.getItem('bd_theme') || 'auto';
  const next = cur === 'light' ? 'dark' : cur === 'dark' ? 'auto' : 'light';
  localStorage.setItem('bd_theme', next);
  applyTheme(next);
}
