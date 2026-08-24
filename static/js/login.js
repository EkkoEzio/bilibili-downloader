/* 登录:状态刷新、扫码轮询、Edge 切换、cookies.txt 导入。含登录 tab 切换。 */
import { getJSON, postText, postEmpty } from './net.js';
import { icon } from './icons.js';

let _loggedIn = false;

export async function refreshLogin() {
  try {
    const r = await getJSON('/api/login/status');
    _loggedIn = !!r.logged_in;
    const sideText = document.getElementById('account-side-text');
    const dot = document.getElementById('account-dot');
    const acctName = document.getElementById('acct-name');
    const acctSub = document.getElementById('acct-sub');
    const logoutBtn = document.getElementById('logout-btn');
    const tabsWrap = document.getElementById('login-tabs-wrap');
    if (r.logged_in) {
      const vip = r.vip ? ' · 大会员' : '';
      sideText.textContent = r.uname;
      dot.className = 'dot ok';
      acctName.textContent = r.uname + vip;
      acctSub.textContent = '已登录';
      logoutBtn.style.display = '';
      tabsWrap.style.display = 'none';
    } else {
      sideText.textContent = '未登录';
      dot.className = 'dot warn';
      acctName.textContent = '未登录';
      acctSub.textContent = '登录后可下载高清画质、同步历史记录';
      logoutBtn.style.display = 'none';
      tabsWrap.style.display = '';
    }
  } catch (e) { _loggedIn = false; }
}

export async function logout() {
  if (!confirm('确定退出登录?')) return;
  await postEmpty('/api/login/logout');
  await refreshLogin();
  document.getElementById('qrcode-img').innerHTML = '';
  document.getElementById('qr-status').textContent = '';
}

/* ---------- 扫码登录 ---------- */
let _qrPollTimer = null;

export async function showQrcode() {
  const imgBox = document.getElementById('qrcode-img');
  const statusBox = document.getElementById('qr-status');
  imgBox.innerHTML = '<div class="empty"><span class="spinner"></span>生成中...</div>';
  statusBox.textContent = '';
  try {
    const r = await getJSON('/api/login/qrcode');
    if (!r.ok) throw new Error(r.error || '生成失败');
    imgBox.innerHTML = `<img src="/api/login/qrcode-image?url=${encodeURIComponent(r.url)}" width="220" height="220" alt="二维码">`;
    statusBox.textContent = '请用手机 B 站 App 扫码';
    if (_qrPollTimer) clearInterval(_qrPollTimer);
    _qrPollTimer = setInterval(() => pollLogin(r.qrcode_key), 2000);
  } catch (e) {
    imgBox.innerHTML = '';
    statusBox.textContent = e.message;
  }
}

async function pollLogin(key) {
  try {
    const r = await getJSON('/api/login/qrcode/poll?key=' + key);
    const statusBox = document.getElementById('qr-status');
    const imgBox = document.getElementById('qrcode-img');
    if (r.status === 'success') {
      clearInterval(_qrPollTimer); _qrPollTimer = null;
      imgBox.innerHTML = '';
      statusBox.innerHTML = icon('check') + ' 登录成功!';
      await refreshLogin();
    } else if (r.status === 'scanned') {
      statusBox.textContent = '已扫码,请在手机上点击确认';
    } else if (r.status === 'expired') {
      clearInterval(_qrPollTimer); _qrPollTimer = null;
      statusBox.textContent = '二维码已失效,请重新生成';
    } else {
      statusBox.textContent = '等待扫码...';
    }
  } catch (e) { /* ignore */ }
}

export async function useBrowser() {
  try {
    await postEmpty('/api/login/use-browser');
    document.getElementById('qr-status').textContent = '已切换为 Edge 登录态模式';
    refreshLogin();
  } catch (e) { alert('切换失败: ' + e.message); }
}

export async function uploadCookieTxt() {
  const text = document.getElementById('cookie-txt-input').value.trim();
  if (!text) { alert('请粘贴 cookies.txt 内容'); return; }
  try {
    const r = await postText('/api/login/cookie-txt', text);
    if (r.ok) { alert('导入成功'); refreshLogin(); }
    else { alert('导入失败: ' + (r.error || '未知错误')); }
  } catch (e) { alert('导入失败: ' + e.message); }
}

/* ---------- 登录 tabs ---------- */
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('panel-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab !== 'qrcode' && _qrPollTimer) { clearInterval(_qrPollTimer); _qrPollTimer = null; }
  });
});
