/* 共享确认、忙碌状态和非阻塞状态播报。 */
let _activeResolve = null;
const _busyState = new WeakMap();

function settle(value) {
  const resolve = _activeResolve;
  _activeResolve = null;
  if (resolve) resolve(!!value);
}

function fallbackMessage({ title, message, detail }) {
  return [title, message, detail].filter(Boolean).join('\n\n');
}

export function confirmAction(options = {}) {
  const config = {
    title: '请确认操作',
    message: '',
    detail: '',
    confirmLabel: '确认',
    danger: true,
    ...options,
  };
  const dialog = document.getElementById('confirm-dialog');
  if (!dialog || typeof dialog.showModal !== 'function') {
    return Promise.resolve(window.confirm(fallbackMessage(config)));
  }

  if (_activeResolve || dialog.open) {
    // 危险操作确认串行化：已有确认框时拒绝后续重入，避免旧 close 事件
    // 意外结算新 Promise，也防止双击叠加多个操作。
    return Promise.resolve(false);
  }
  document.getElementById('confirm-title').textContent = config.title;
  document.getElementById('confirm-message').textContent = config.message;
  const detail = document.getElementById('confirm-detail');
  detail.textContent = config.detail;
  detail.hidden = !config.detail;
  const accept = document.getElementById('confirm-accept');
  accept.textContent = config.confirmLabel;
  accept.className = config.danger ? 'danger' : 'primary';
  dialog.returnValue = '';

  return new Promise((resolve) => {
    _activeResolve = resolve;
    try {
      dialog.showModal();
    } catch (_) {
      _activeResolve = null;
      resolve(window.confirm(fallbackMessage(config)));
    }
  });
}

export function setBusy(elements, busy, label = '') {
  const targets = (Array.isArray(elements) ? elements : [elements]).filter(Boolean);
  targets.forEach((element) => {
    if (busy) {
      if (_busyState.has(element)) return;
      _busyState.set(element, {
        disabled: !!element.disabled,
        html: element.innerHTML,
      });
      element.disabled = true;
      element.setAttribute('aria-busy', 'true');
      if (label) element.textContent = label;
      return;
    }
    const previous = _busyState.get(element);
    if (!previous) return;
    element.disabled = previous.disabled;
    element.innerHTML = previous.html;
    element.removeAttribute('aria-busy');
    _busyState.delete(element);
  });
}

export async function withPending(elements, label, action) {
  setBusy(elements, true, label);
  try {
    return await action();
  } finally {
    setBusy(elements, false);
  }
}

export function announce(message, assertive = false) {
  const region = document.getElementById('app-status');
  if (!region) return;
  region.setAttribute('aria-live', assertive ? 'assertive' : 'polite');
  region.textContent = '';
  window.requestAnimationFrame(() => { region.textContent = String(message || ''); });
}

/* 非阻塞轻提示:可见反馈通道(替代原生 alert 的阻塞弹窗)。无障碍播报仍走
   announce(),这里刻意不挂 aria-live,避免同一句话被读屏播两遍。 */
let _toastEl = null;
let _toastTimer = null;

export function toast(message) {
  if (!_toastEl) {
    _toastEl = document.createElement('div');
    _toastEl.className = 'toast';
    _toastEl.setAttribute('role', 'presentation');
    document.body.appendChild(_toastEl);
  }
  _toastEl.textContent = String(message ?? '');
  _toastEl.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => _toastEl && _toastEl.classList.remove('show'), 3000);
}

const dialog = document.getElementById('confirm-dialog');
if (dialog) {
  dialog.addEventListener('close', () => settle(dialog.returnValue === 'confirm'));
  dialog.addEventListener('cancel', () => {
    // 原生 Esc 行为保留；close 事件负责完成 Promise。
    dialog.returnValue = 'cancel';
  });
}
