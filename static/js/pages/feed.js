/* 关注动态页：三个源——
   bili  关注动态（慢源：缓存优先 + 变化提示）
   all   关注列表（UP 卡片，可一键加入清单）
   mine  自动下载清单（本地管理） */
import { registerPage, state, libDom, renderDefaultList, renderPage, cachedLoad,
         getLibSearchQuery, upCardChunk, defaultChunk, dismissFresh, refreshLib,
         maybeFillScreen, replaceItems, cancelCachedLoads } from '../lib.js';
import { getJSON, postJSON, postEmpty, isAbortError } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, safeDecimalId, safeHttpUrl } from '../util.js';
import { qualityOptionsHtml } from '../quality.js';
import { loadAutoRefreshStatus } from '../settings.js';
import { announce, confirmAction, withPending } from '../confirm.js';

let _feedSource = (() => {
  const source = localStorage.getItem('bd_feed_src');
  return (source === 'all' || source === 'mine') ? source : 'bili';
})();
let _myUps = [];
let _followAll = [];
let _followAllTotal = 0;
let _followAllShown = 0;
let _localMids = new Set();
let _mineGeneration = 0;
let _mineController = null;

function cancelMineLoad() {
  _mineGeneration++;
  if (_mineController) _mineController.abort();
  _mineController = null;
}

function mineRequestIsCurrent(generation, controller) {
  return generation === _mineGeneration
    && controller === _mineController
    && !controller.signal.aborted
    && state.mode === 'feed'
    && _feedSource === 'mine';
}

export function setFeedSource(src) {
  const next = ['bili', 'all', 'mine'].includes(src) ? src : 'bili';
  cancelCachedLoads('feed');
  cancelMineLoad();
  _feedSource = next;
  localStorage.setItem('bd_feed_src', next);
  syncSeg();
  replaceItems([]);
  state.shown = 0;
  _followAllShown = 0;
  state.checked.clear();
  dismissFresh('feed');
  document.getElementById('feed-mine-tools').style.display = next === 'mine' ? '' : 'none';
  if (next === 'mine') loadAutoRefreshStatus();
  refreshLib();
}

export function syncSeg() {
  document.querySelectorAll('#feed-seg button').forEach((button) => {
    const active = button.dataset.src === _feedSource;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const tools = document.getElementById('feed-mine-tools');
  if (tools) tools.style.display = _feedSource === 'mine' ? '' : 'none';
}

/* ---------- 关注动态（bili） ---------- */
async function loadFollowBili() {
  await cachedLoad({
    page: 'feed',
    api: '/api/follow-updates',
    cacheKey: 'follow_updates',
    extract: (b) => b.items || [],
    stillValid: () => state.mode === 'feed' && _feedSource === 'bili',
    showLoading: () => {
      const d = libDom();
      d.meta.textContent = '';
      d.list.innerHTML = '<div class="empty">正在后台拉取关注 UP 的近期投稿（串行防限流，约需数十秒），完成后自动展示。</div>';
    },
    showError: (msg) => {
      libDom().list.innerHTML = `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost sm" onclick="loadFollowBili()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      replaceItems(b.items || []);
      renderPage();
      libDom().meta.textContent = `共 ${state.items.length} 条`;
    },
  });
}

/* ---------- 关注列表（all） ---------- */
async function loadFollowAll() {
  await cachedLoad({
    page: 'feed',
    api: '/api/followings',
    cacheKey: 'followings',
    extract: (b) => b.items || [],
    stillValid: () => state.mode === 'feed' && _feedSource === 'all',
    showLoading: () => {
      const d = libDom();
      d.meta.textContent = '';
      d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载你关注的 UP...</div>';
    },
    showError: (msg) => {
      const d = libDom();
      d.list.innerHTML = `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost sm" onclick="loadFollowAll()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _followAll = b.items || [];
      _followAllTotal = b.total || _followAll.length;
      _localMids = new Set((b.local_mids || []).map(safeDecimalId).filter(Boolean));
      _followAllShown = 0;
      renderPage();
    },
  });
}

function renderFollowAll() {
  const d = libDom();
  const q = getLibSearchQuery();
  let filtered = _followAll;
  if (q) filtered = _followAll.filter(u => (u.uname || '').toLowerCase().includes(q));
  if (!_followAllShown) _followAllShown = upCardChunk();
  const shownItems = q ? filtered : filtered.slice(0, _followAllShown);
  if (shownItems.length === 0) {
    d.list.innerHTML = `<div class="empty">${q ? '没有匹配结果（试试其他关键词）' : '暂无关注的 UP'}</div>`;
  } else {
    d.list.className = 'lib-list';
    d.list.innerHTML = `<div class="${state.view === 'grid' ? 'up-grid' : 'up-list'}">` + shownItems.map(u => {
      const mid = safeDecimalId(u.mid);
      const inLocal = !!mid && _localMids.has(mid);
      const btn = !mid
        ? '<span class="dl-badge">ID 无效</span>'
        : (inLocal
          ? '<span class="dl-badge">' + icon('check') + '已加清单</span>'
          : `<button class="ghost sm" onclick="upAddLocal(this.dataset.mid, this)" data-mid="${escapeAttr(mid)}">加入清单</button>`);
      const face = safeHttpUrl(u.face, { httpsOnly: true });
      const name = escapeHtml(u.uname || (mid ? ('UP ' + mid) : '未知 UP'));
      const nameHtml = mid
        ? `<a href="https://space.bilibili.com/${mid}" target="_blank" rel="noopener">${name}</a>`
        : name;
      return `
      <div class="up-card">
        <img src="${escapeAttr(face)}" alt="" onerror="this.style.visibility='hidden'">
        <div class="uc-name" title="${escapeAttr(u.uname || '')}">${nameHtml}</div>
        ${btn}
      </div>`;
    }).join('') + '</div>';
  }
  if (d.meta) {
    d.meta.textContent = q
      ? `共关注 ${_followAllTotal} 个 UP · 匹配 ${filtered.length}`
      : `共关注 ${_followAllTotal} 个 UP`;
  }
}

function advanceFollowAll() {
  if (getLibSearchQuery()) return false;
  if (_followAllShown >= _followAll.length) return false;
  _followAllShown += upCardChunk();
  renderFollowAll();
  return true;
}

export async function upAddLocal(mid, btnEl) {
  try {
    const normalizedMid = safeDecimalId(mid);
    if (!normalizedMid) throw new Error('UP ID 无效');
    const r = await postJSON('/api/up-list/add', { input: normalizedMid });
    if (!r.ok) throw new Error(r.error || '添加失败');
    _localMids.add(normalizedMid);
    if (btnEl) btnEl.outerHTML = '<span class="dl-badge">' + icon('check') + '已加清单</span>';
  } catch (error) { alert(error.message); }
}

/* ---------- 自动下载清单（mine） ---------- */
export async function loadMyUps() {
  loadAutoRefreshStatus();
  cancelMineLoad();
  const generation = _mineGeneration;
  const controller = new AbortController();
  _mineController = controller;
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载自动下载清单...</div>';
  try {
    const r = await getJSON('/api/up-list', { signal: controller.signal });
    if (!mineRequestIsCurrent(generation, controller)) return;
    if (!r.ok) throw new Error(r.error || '加载失败');
    _myUps = r.ups || [];
    renderMyUps();
  } catch (error) {
    if (isAbortError(error) || !mineRequestIsCurrent(generation, controller)) return;
    d.list.innerHTML = `<div class="empty">${escapeHtml(error.message || '加载失败')}<br><button class="ghost sm" onclick="loadMyUps()">${icon('refresh')} 重试</button></div>`;
  }
}

function renderMyUps() {
  const d = libDom();
  const q = getLibSearchQuery();
  let ups = _myUps;
  if (q) ups = _myUps.filter(u =>
    (u.uname || '').toLowerCase().includes(q) || String(u.mid).includes(q));
  d.list.className = 'lib-list';
  if (ups.length === 0) {
    d.list.innerHTML = `<div class="empty">${q ? '没有匹配结果（试试其他关键词）'
      : '清单为空。在上方粘贴 UP 主页链接添加，或到「关注列表」一键加入。<br>加入清单的 UP，新投稿会自动进入下载队列。'}</div>`;
  } else {
    d.list.innerHTML = `<div class="${state.view === 'grid' ? 'up-grid' : 'up-list'}">` + ups.map(u => {
      const mid = safeDecimalId(u.mid);
      const face = safeHttpUrl(u.face, { httpsOnly: true });
      const name = escapeHtml(u.uname || (mid ? ('UP ' + mid) : '未知 UP'));
      const nameHtml = mid
        ? `<a href="https://space.bilibili.com/${mid}" target="_blank" rel="noopener">${name}</a>`
        : name;
      const controls = mid ? `
          <select aria-label="${escapeAttr(u.uname || ('UP ' + mid))}的新视频默认清晰度" onchange="upSetQuality(this.dataset.mid, this.value)" data-mid="${escapeAttr(mid)}" title="该 UP 新视频自动下载的默认清晰度">${qualityOptionsHtml(u.quality || '')}</select>
          <button class="danger sm" onclick="upRemove(this.dataset.mid, this)" data-mid="${escapeAttr(mid)}">移除</button>`
        : '<span class="dl-badge">ID 无效</span>';
      return `
      <div class="up-card mgr">
        <img src="${escapeAttr(face)}" alt="" onerror="this.style.visibility='hidden'">
        <div class="uc-name" title="${escapeAttr(u.uname || '')}">${nameHtml}</div>
        <span class="dl-badge">${icon('check')}自动下载</span>
        <div class="uc-ctl">${controls}</div>
      </div>`;
    }).join('') + '</div>';
  }
  if (d.meta) {
    d.meta.textContent = q
      ? `清单 ${_myUps.length} 个 UP · 匹配 ${ups.length}`
      : `共 ${_myUps.length} 个 UP 在自动下载名单 · 新投稿自动进入下载队列`;
  }
}

export async function upAdd() {
  const input = document.getElementById('up-add-input');
  const button = document.getElementById('up-add-btn');
  const value = input.value.trim();
  if (!value) { alert('请粘贴 UP 主页链接或 mid'); return; }
  try {
    await withPending(button, '添加中…', async () => {
      const r = await postJSON('/api/up-list/add', { input: value });
      if (!r.ok) throw new Error(r.error || '添加失败');
      input.value = '';
      announce(`已加入自动下载清单：${r.up.uname || ('UP ' + r.up.mid)}`);
      alert(`已加入清单：${r.up.uname || ('UP ' + r.up.mid)}\n之后 TA 的新投稿会自动进入下载队列。`);
      if (state.mode === 'feed' && _feedSource === 'mine') loadMyUps();
    });
  } catch (error) { alert(error.message); }
}

export async function upSetQuality(mid, quality) {
  try {
    const r = await postJSON('/api/up-list/set-quality', { mid: parseInt(mid), quality });
    if (!r.ok) { alert('设置失败'); loadMyUps(); }
  } catch (error) { alert(error.message); }
}

export async function upRemove(mid, button = null) {
  const normalizedMid = safeDecimalId(mid);
  if (!normalizedMid) { alert('UP ID 无效'); return; }
  const accepted = await confirmAction({
    title: '移出自动下载清单？',
    message: '移除后，该 UP 的新投稿将不再自动加入下载队列。已有任务不会被删除。',
    confirmLabel: '确认移除',
  });
  if (!accepted) return;
  try {
    await withPending(button, '移除中…', async () => {
      const r = await postJSON('/api/up-list/remove', { mid: parseInt(normalizedMid, 10) });
      if (!r.ok) throw new Error(r.error || '移除失败');
    });
    announce('已从自动下载清单移除 UP');
    if (state.mode === 'feed' && _feedSource === 'mine') loadMyUps();
  } catch (error) { alert(error.message); }
}

export async function upCheckNow(btn) {
  try {
    await withPending(btn, '检查中…', async () => {
      const r = await postEmpty('/api/up-list/check');
      const message = r && r.started === false
        ? '已有检查在进行中，本次未启动新一轮。稍后队列会自动带回新任务。'
        : '已在后台开始检查，新视频会自动进入下载队列。';
      announce(message);
      alert(message);
    });
    window.setTimeout(loadAutoRefreshStatus, 300);
  } catch (error) {
    announce(error.message || '检查启动失败', true);
    alert(error.message);
  }
}

function retryFeed() {
  if (_feedSource === 'bili') loadFollowBili();
  else if (_feedSource === 'all') loadFollowAll();
  else loadMyUps();
}

function deactivateFeed() {
  cancelMineLoad();
  cancelCachedLoads('feed');
}

registerPage('feed', {
  load: () => {
    if (_feedSource === 'bili') loadFollowBili();
    else if (_feedSource === 'all') loadFollowAll();
    else loadMyUps();
  },
  retry: retryFeed,
  render: (d) => {
    if (_feedSource === 'all') { renderFollowAll(); maybeFillScreen(); }
    else if (_feedSource === 'mine') renderMyUps();
    else renderDefaultList(d);
  },
  infinite: () => (_feedSource === 'mine' ? 'none' : (_feedSource === 'all' ? 'own' : 'items')),
  advanceChunk: () => (_feedSource === 'all' ? advanceFollowAll() : undefined),
  chunk: () => (_feedSource === 'bili' ? defaultChunk() : upCardChunk()),
  actionBarVisible: () => _feedSource === 'bili',
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '暂无内容（可能 B 站限流或账号无数据）',
  onViewToggle: () => { _followAllShown = 0; },
  deactivate: deactivateFeed,
});

export { loadFollowBili, loadFollowAll };
