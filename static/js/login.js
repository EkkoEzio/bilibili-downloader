/* 登录：状态刷新、扫码轮询、浏览器切换、cookies.txt 导入。 */
import { getJSON, postText, postEmpty, isAbortError } from './net.js';
import { icon } from './icons.js';
import { announce, confirmAction, withPending } from './confirm.js';

const STATUS_SUPERSEDED = Symbol('login-status-superseded');
const MUTATION_SUPERSEDED = Symbol('auth-mutation-superseded');

let _loggedIn = false;
let _authGeneration = null;
let _authVersion = null;
let _statusSequence = 0;
let _statusController = null;
let _mutationSequence = 0;
let _qrPollTimer = null;
let _qrController = null;
let _qrGeneration = 0;

const AUTH_SOURCE_LABELS = {
  qr: '扫码会话',
  cookie_file: 'cookies.txt',
  browser: 'Edge 浏览器',
  none: '未选择',
};

function invalidateLoginStatus() {
  _statusSequence++;
  const controller = _statusController;
  _statusController = null;
  if (controller) controller.abort();
}

function beginLoginStatusRequest() {
  invalidateLoginStatus();
  const controller = new AbortController();
  const sequence = _statusSequence;
  _statusController = controller;
  return { sequence, controller };
}

function loginStatusIsCurrent(sequence, controller) {
  return sequence === _statusSequence
    && controller === _statusController
    && !controller.signal.aborted;
}

function beginAuthMutation() {
  _mutationSequence++;
  invalidateLoginStatus();
  return _mutationSequence;
}

function authMutationIsCurrent(sequence) {
  return sequence === _mutationSequence;
}

function stopQrPolling() {
  _qrGeneration++;
  if (_qrPollTimer) clearTimeout(_qrPollTimer);
  _qrPollTimer = null;
  if (_qrController) _qrController.abort();
  _qrController = null;
}

function qrRequestIsCurrent(generation, controller = null) {
  return generation === _qrGeneration
    && (!controller || (controller === _qrController && !controller.signal.aborted));
}

function sourceLabel(source) {
  return AUTH_SOURCE_LABELS[source] || '未知来源';
}

function emitAuthChange(status) {
  const generation = Number(status && status.auth_generation);
  if (!Number.isFinite(generation)) return;
  const incarnation = String((status && status.auth_incarnation) || '');
  const version = `${incarnation}:${generation}`;
  const previousGeneration = _authGeneration;
  const previousVersion = _authVersion;
  _authGeneration = generation;
  _authVersion = version;
  if (previousVersion === null || previousVersion === version) return;
  document.dispatchEvent(new CustomEvent('auth:changed', {
    detail: {
      previousGeneration,
      generation,
      incarnation,
      source: status.auth_source || 'none',
      authenticated: !!status.authenticated,
      loggedIn: !!status.logged_in,
    },
  }));
}

function renderLoginStatus(status, networkError = '') {
  const r = status || {};
  const loggedIn = !!r.logged_in;
  const source = r.auth_source || 'none';
  const authenticated = !!r.authenticated;
  const errorMessage = networkError || r.error || '';
  _loggedIn = loggedIn;

  const sideText = document.getElementById('account-side-text');
  const dot = document.getElementById('account-dot');
  const acctName = document.getElementById('acct-name');
  const acctSub = document.getElementById('acct-sub');
  const acctError = document.getElementById('acct-error');
  const logoutBtn = document.getElementById('logout-btn');
  const tabsWrap = document.getElementById('login-tabs-wrap');
  if (!sideText || !dot || !acctName || !acctSub || !logoutBtn || !tabsWrap) return;

  if (loggedIn) {
    const vip = r.vip ? ' · 大会员' : '';
    const uname = r.uname || '已登录账号';
    sideText.textContent = uname;
    dot.className = 'dot ok';
    acctName.textContent = uname + vip;
    acctSub.textContent = `账号已验证 · 来源：${sourceLabel(source)}`;
    logoutBtn.style.display = '';
    tabsWrap.style.display = 'none';
  } else {
    sideText.textContent = networkError ? '状态未知' : '未登录';
    dot.className = 'dot warn';
    acctName.textContent = networkError ? '无法确认登录状态' : '未登录';
    if (source === 'none') {
      acctSub.textContent = '未选择登录来源；登录后可下载账号有权观看的内容';
    } else if (authenticated) {
      acctSub.textContent = `已保存来源：${sourceLabel(source)}；本次账号校验未通过`;
    } else {
      acctSub.textContent = `已选择来源：${sourceLabel(source)}；尚未验证到账号`;
    }
    logoutBtn.style.display = source === 'none' ? 'none' : '';
    tabsWrap.style.display = '';
  }
  if (acctError) {
    acctError.textContent = errorMessage ? `状态错误：${errorMessage}` : '';
    acctError.hidden = !errorMessage;
  }
}

export async function refreshLogin() {
  const { sequence, controller } = beginLoginStatusRequest();
  try {
    const status = await getJSON('/api/login/status', { signal: controller.signal });
    if (!loginStatusIsCurrent(sequence, controller)) return STATUS_SUPERSEDED;
    renderLoginStatus(status);
    emitAuthChange(status);
    return status;
  } catch (error) {
    if (isAbortError(error) || !loginStatusIsCurrent(sequence, controller)) {
      return STATUS_SUPERSEDED;
    }
    renderLoginStatus({
      logged_in: false,
      auth_source: 'none',
      authenticated: false,
    }, error.message || '登录状态请求失败');
    return null;
  } finally {
    if (loginStatusIsCurrent(sequence, controller)) _statusController = null;
  }
}

export async function logout() {
  const accepted = await confirmAction({
    title: '退出当前账号？',
    message: '将清除本工具保存的登录来源与会话。正在下载的任务不会被取消。',
    confirmLabel: '退出登录',
  });
  if (!accepted) return;
  stopQrPolling();
  const mutationSequence = beginAuthMutation();
  const button = document.getElementById('logout-btn');
  let status = null;
  try {
    await withPending(button, '退出中…', async () => {
      const r = await postEmpty('/api/login/logout');
      if (!authMutationIsCurrent(mutationSequence)) return;
      if (r.ok === false) throw new Error(r.error || '退出失败');
      status = await refreshLogin();
      if (status === STATUS_SUPERSEDED) return;
      if (!authMutationIsCurrent(mutationSequence)) return;
      document.getElementById('qrcode-img').innerHTML = '';
      document.getElementById('qr-status').textContent = '';
    });
    if (status === STATUS_SUPERSEDED
        || !authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    announce('已退出登录');
  } catch (error) {
    if (!authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    announce(error.message || '退出失败', true);
    alert(error.message || '退出失败');
  }
}

/* ---------- 扫码登录 ---------- */
function scheduleQrPoll(key, generation, delay = 2000) {
  if (!qrRequestIsCurrent(generation)) return;
  if (_qrPollTimer) clearTimeout(_qrPollTimer);
  _qrPollTimer = setTimeout(() => {
    _qrPollTimer = null;
    pollLogin(key, generation);
  }, delay);
}

function retryDelay(error) {
  const raw = error && error.retryAfter;
  if (raw) {
    const seconds = Number(raw);
    if (Number.isFinite(seconds) && seconds >= 0) {
      return Math.min(30000, Math.max(2000, seconds * 1000));
    }
    const at = Date.parse(raw);
    if (Number.isFinite(at)) return Math.min(30000, Math.max(2000, at - Date.now()));
  }
  return 3000;
}

export async function showQrcode() {
  stopQrPolling();
  const generation = _qrGeneration;
  const controller = new AbortController();
  _qrController = controller;
  const imgBox = document.getElementById('qrcode-img');
  const statusBox = document.getElementById('qr-status');
  const button = document.getElementById('qrcode-btn');
  imgBox.innerHTML = '<div class="empty"><span class="spinner"></span>生成中...</div>';
  statusBox.textContent = '';
  try {
    await withPending(button, '生成中…', async () => {
      const r = await getJSON('/api/login/qrcode', { signal: controller.signal });
      if (!qrRequestIsCurrent(generation, controller)) return;
      if (!r.ok) throw new Error(r.error || '生成失败');
      imgBox.innerHTML = `<img src="/api/login/qrcode-image?url=${encodeURIComponent(r.url)}" width="220" height="220" alt="B 站登录二维码">`;
      statusBox.textContent = '请用手机 B 站 App 扫码';
      scheduleQrPoll(r.qrcode_key, generation);
    });
  } catch (error) {
    if (isAbortError(error) || !qrRequestIsCurrent(generation, controller)) return;
    imgBox.innerHTML = '';
    statusBox.textContent = error.message || '生成失败';
  }
}

async function pollLogin(key, generation) {
  if (!qrRequestIsCurrent(generation)) return;
  const controller = new AbortController();
  _qrController = controller;
  let nextDelay = 2000;
  let shouldContinue = false;
  const mutationSequence = beginAuthMutation();
  try {
    const r = await getJSON('/api/login/qrcode/poll?key=' + encodeURIComponent(key), {
      signal: controller.signal,
    });
    if (!qrRequestIsCurrent(generation, controller)
        || !authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    const statusBox = document.getElementById('qr-status');
    const imgBox = document.getElementById('qrcode-img');
    if (r.status === 'success') {
      stopQrPolling();
      const status = await refreshLogin();
      if (status === STATUS_SUPERSEDED) return;
      if (!authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
      imgBox.innerHTML = '';
      statusBox.innerHTML = icon('check') + ' 登录成功！';
      announce('B 站账号登录成功');
    } else if (r.status === 'scanned') {
      statusBox.textContent = '已扫码，请在手机上点击确认';
      shouldContinue = true;
    } else if (r.status === 'expired') {
      statusBox.textContent = '二维码已失效，请重新生成';
      stopQrPolling();
    } else {
      statusBox.textContent = '等待扫码...';
      shouldContinue = true;
    }
  } catch (error) {
    if (!authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    if (!isAbortError(error) && qrRequestIsCurrent(generation, controller)) {
      nextDelay = retryDelay(error);
      const seconds = Math.max(1, Math.ceil(nextDelay / 1000));
      document.getElementById('qr-status').textContent =
        `网络异常，${seconds} 秒后重试：${error.message || '请求失败'}`;
      shouldContinue = true;
    }
  } finally {
    if (shouldContinue
        && authMutationIsCurrent(mutationSequence)
        && qrRequestIsCurrent(generation, controller)) {
      scheduleQrPoll(key, generation, nextDelay);
    }
  }
}

export async function useBrowser() {
  stopQrPolling();
  const mutationSequence = beginAuthMutation();
  const button = document.getElementById('browser-login-btn');
  try {
    let status = null;
    await withPending(button, '验证中…', async () => {
      const r = await postEmpty('/api/login/use-browser');
      if (!authMutationIsCurrent(mutationSequence)) return;
      if (r.ok === false) throw new Error(r.error || '切换失败');
      status = await refreshLogin();
      if (status === STATUS_SUPERSEDED) return;
      if (!authMutationIsCurrent(mutationSequence)) return;
    });
    if (status === STATUS_SUPERSEDED
        || !authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    announce(status && status.logged_in
      ? '已使用 Edge 登录态验证账号'
      : '已选择 Edge 登录来源，但尚未验证到账号');
  } catch (error) {
    if (!authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    announce('Edge 登录态切换失败：' + error.message, true);
    alert('切换失败：' + error.message);
  }
}

export async function uploadCookieTxt() {
  const text = document.getElementById('cookie-txt-input').value.trim();
  if (!text) { alert('请粘贴 cookies.txt 内容'); return; }
  stopQrPolling();
  const mutationSequence = beginAuthMutation();
  const button = document.getElementById('cookie-import-btn');
  try {
    let status = null;
    await withPending(button, '导入中…', async () => {
      const r = await postText('/api/login/cookie-txt', text);
      if (!authMutationIsCurrent(mutationSequence)) return;
      if (!r.ok) throw new Error(r.error || '导入失败');
      status = await refreshLogin();
      if (status === STATUS_SUPERSEDED) return;
      if (!authMutationIsCurrent(mutationSequence)) return;
    });
    if (status === STATUS_SUPERSEDED
        || !authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    announce(status && status.logged_in
      ? 'cookies.txt 已导入并验证账号'
      : 'cookies.txt 已导入，但尚未验证到账号');
  } catch (error) {
    if (!authMutationIsCurrent(mutationSequence)) return MUTATION_SUPERSEDED;
    announce('cookies.txt 导入失败：' + error.message, true);
    alert('导入失败：' + error.message);
  }
}

/* ---------- 登录 tabs ---------- */
function activateTab(tab, focus = false) {
  if (!tab) return;
  document.querySelectorAll('.tab').forEach((item) => {
    const active = item === tab;
    item.classList.toggle('active', active);
    item.setAttribute('aria-selected', active ? 'true' : 'false');
    item.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('.tab-panel').forEach((panel) => {
    const active = panel.id === 'panel-' + tab.dataset.tab;
    panel.classList.toggle('active', active);
    panel.hidden = !active;
  });
  if (tab.dataset.tab !== 'qrcode') stopQrPolling();
  if (focus) tab.focus();
}

const tabs = [...document.querySelectorAll('.tab')];
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', (event) => {
    let target = null;
    if (event.key === 'ArrowRight') target = tabs[(index + 1) % tabs.length];
    if (event.key === 'ArrowLeft') target = tabs[(index - 1 + tabs.length) % tabs.length];
    if (event.key === 'Home') target = tabs[0];
    if (event.key === 'End') target = tabs[tabs.length - 1];
    if (!target) return;
    event.preventDefault();
    activateTab(target, true);
  });
});

document.addEventListener('drawer:closed', stopQrPolling);
document.addEventListener('drawer:opened', (event) => {
  if (event.detail && event.detail.name === 'account') refreshLogin();
});
