/* 抽屉壳:打开/关闭。account 面板打开时同步设置项到 UI。 */
import { loadConfigIntoUI } from './settings.js';

export function openDrawer(name) {
  document.querySelectorAll('.drawer-panel').forEach(p => p.classList.remove('active'));
  const panel = document.getElementById('dpanel-' + name);
  if (!panel) return;
  panel.classList.add('active');
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-mask').classList.add('open');
  if (name === 'account') loadConfigIntoUI();
}

export function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-mask').classList.remove('open');
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
