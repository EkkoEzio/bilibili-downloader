/* 抽屉壳：打开/关闭、焦点圈定、背景 inert 与生命周期事件。 */
import { loadConfigIntoUI } from './settings.js';

let _returnFocus = null;
let _activeName = null;

function drawerElement() { return document.getElementById('drawer'); }

function setBackgroundInert(inert) {
  document.querySelectorAll('.app-shell, #lib-action-bar, #back-top').forEach((element) => {
    element.inert = !!inert;
  });
}

function focusableElements() {
  const drawer = drawerElement();
  if (!drawer) return [];
  return [...drawer.querySelectorAll(
    'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  )].filter((element) => !element.hidden && element.getClientRects().length > 0);
}

export function openDrawer(name) {
  const drawer = drawerElement();
  document.querySelectorAll('.drawer-panel').forEach(p => p.classList.remove('active'));
  const panel = document.getElementById('dpanel-' + name);
  if (!drawer || !panel) return;

  if (!drawer.classList.contains('open')) _returnFocus = document.activeElement;
  _activeName = name;
  panel.classList.add('active');
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  document.getElementById('drawer-mask').classList.add('open');
  document.body.classList.add('drawer-open');
  setBackgroundInert(true);
  if (name === 'account') loadConfigIntoUI();
  document.dispatchEvent(new CustomEvent('drawer:opened', { detail: { name } }));

  const closeButton = panel.querySelector('[data-drawer-close]');
  (closeButton || focusableElements()[0] || drawer).focus();
}

export function closeDrawer() {
  const drawer = drawerElement();
  if (!drawer || !drawer.classList.contains('open')) return;
  const closedName = _activeName;
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  document.getElementById('drawer-mask').classList.remove('open');
  document.body.classList.remove('drawer-open');
  setBackgroundInert(false);
  _activeName = null;
  document.dispatchEvent(new CustomEvent('drawer:closed', { detail: { name: closedName } }));
  if (_returnFocus && _returnFocus.isConnected && typeof _returnFocus.focus === 'function') {
    _returnFocus.focus();
  }
  _returnFocus = null;
}

document.addEventListener('keydown', (event) => {
  const drawer = drawerElement();
  if (!drawer || !drawer.classList.contains('open')) return;
  if (document.querySelector('dialog[open]')) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeDrawer();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = focusableElements();
  if (!focusable.length) {
    event.preventDefault();
    drawer.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});
