/* 关注动态页:三个源——
   bili  关注动态(关注 UP 的近期投稿,慢源:缓存优先+变化提示)
   all   关注列表(站点关注的全部 UP,UP 卡片,可一键加入清单)
   mine  自动下载清单(本地管理:添加/清晰度/移除/立即检查) */
import { registerPage, state, libDom, renderDefaultList, renderPage, cachedLoad,
         getLibSearchQuery, upCardChunk, defaultChunk, dismissFresh, refreshLib,
         maybeFillScreen } from '../lib.js';
import { getJSON, postJSON, postEmpty } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml } from '../util.js';
import { qualityOptionsHtml } from '../quality.js';

let _feedSource = (() => {
  const s = localStorage.getItem('bd_feed_src');
  return (s === 'all' || s === 'mine') ? s : 'bili';  // 旧值 'up' 已废弃
})();
let _myUps = [];
let _followAll = [];       // {mid, uname, face}
let _followAllTotal = 0;
let _followAllShown = 0;   // 无限滚:当前已显示的条数
let _localMids = new Set();

export function setFeedSource(src) {
  _feedSource = src;
  localStorage.setItem('bd_feed_src', src);
  syncSeg();
  state.items = [];
  state.shown = 0;
  _followAllShown = 0;
  state.checked.clear();
  dismissFresh('feed');
  document.getElementById('feed-mine-tools').style.display = src === 'mine' ? '' : 'none';
  refreshLib();
}

export function syncSeg() {
  document.querySelectorAll('#feed-seg button').forEach(b => b.classList.toggle('active', b.dataset.src === _feedSource));
  const tools = document.getElementById('feed-mine-tools');
  if (tools) tools.style.display = _feedSource === 'mine' ? '' : 'none';
}

/* ---------- 关注动态(bili) ---------- */
async function loadFollowBili() {
  // 缓存优先(串行拉 30 个 UP 要几十秒,绝不让人干等);后台拉新,有变化提示
  await cachedLoad({
    page: 'feed',
    api: '/api/follow-updates',
    cacheKey: 'follow_updates',
    extract: (b) => b.items || [],
    stillValid: () => state.mode === 'feed' && _feedSource === 'bili',
    showLoading: () => {
      const d = libDom();
      d.meta.textContent = '';
      d.list.innerHTML = '<div class="empty">正在后台拉取关注 UP 的近期投稿(串行防限流,约需数十秒),完成后自动展示。</div>';
    },
    showError: (msg) => {
      libDom().list.innerHTML = `<div class="empty">${msg}<br><button class="ghost sm" onclick="loadFollowBili()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      state.items = b.items || [];
      renderPage();
      libDom().meta.textContent = `共 ${state.items.length} 条`;
    },
  });
}

/* ---------- 关注列表(all):UP 卡片 + 无限滚 ---------- */
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
      d.list.innerHTML = `<div class="empty">${msg}<br><button class="ghost sm" onclick="loadFollowAll()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _followAll = b.items || [];
      _followAllTotal = b.total || _followAll.length;
      _localMids = new Set(b.local_mids || []);
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
    d.list.innerHTML = `<div class="empty">${q ? '没有匹配结果(试试其他关键词)' : '暂无关注的 UP'}</div>`;
  } else {
    d.list.className = 'lib-list';
    d.list.innerHTML = `<div class="${state.view === 'grid' ? 'up-grid' : 'up-list'}">` + shownItems.map(u => {
      const inLocal = _localMids.has(u.mid);
      const btn = inLocal
        ? '<span class="dl-badge">' + icon('check') + '已加清单</span>'
        : `<button class="ghost sm" onclick="upAddLocal(this.dataset.mid, this)" data-mid="${escapeHtml(String(u.mid))}">加入清单</button>`;
      return `
      <div class="up-card">
        <img src="${escapeHtml(u.face)}" onerror="this.style.visibility='hidden'">
        <div class="uc-name" title="${escapeHtml(u.uname)}"><a href="https://space.bilibili.com/${u.mid}" target="_blank" rel="noopener">${escapeHtml(u.uname || ('UP ' + u.mid))}</a></div>
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
    const r = await postJSON('/api/up-list/add', { input: String(mid) });
    if (!r.ok) throw new Error(r.error || '添加失败');
    _localMids.add(mid);
    if (btnEl) btnEl.outerHTML = '<span class="dl-badge">' + icon('check') + '已加清单</span>';
  } catch (e) { alert(e.message); }
}

/* ---------- 自动下载清单(mine):页面直接管理 ---------- */
export async function loadMyUps() {
  const d = libDom();
  d.meta.textContent = '';
  try {
    const r = await getJSON('/api/up-list');
    if (!r.ok) throw new Error(r.error || '加载失败');
    _myUps = r.ups || [];
    renderMyUps();
  } catch (e) {
    d.list.innerHTML = `<div class="empty">${e.message}<br><button class="ghost sm" onclick="loadMyUps()">${icon('refresh')} 重试</button></div>`;
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
    d.list.innerHTML = `<div class="empty">${q ? '没有匹配结果(试试其他关键词)'
      : '清单为空。在上方粘贴 UP 主页链接添加,或到「关注列表」一键加入。<br>加入清单的 UP,新投稿会自动进入下载队列。'}</div>`;
  } else {
    d.list.innerHTML = `<div class="${state.view === 'grid' ? 'up-grid' : 'up-list'}">` + ups.map(u => `
      <div class="up-card mgr">
        <img src="${escapeHtml(u.face)}" onerror="this.style.visibility='hidden'">
        <div class="uc-name" title="${escapeHtml(u.uname)}"><a href="https://space.bilibili.com/${u.mid}" target="_blank" rel="noopener">${escapeHtml(u.uname || ('UP ' + u.mid))}</a></div>
        <span class="dl-badge">${icon('check')}自动下载</span>
        <div class="uc-ctl">
          <select onchange="upSetQuality(this.dataset.mid, this.value)" data-mid="${escapeHtml(String(u.mid))}" title="该 UP 新视频自动下载的默认清晰度">${qualityOptionsHtml(u.quality || '')}</select>
          <button class="danger sm" onclick="upRemove(this.dataset.mid)" data-mid="${escapeHtml(String(u.mid))}">移除</button>
        </div>
      </div>`).join('') + '</div>';
  }
  if (d.meta) {
    d.meta.textContent = q
      ? `清单 ${_myUps.length} 个 UP · 匹配 ${ups.length}`
      : `共 ${_myUps.length} 个 UP 在自动下载名单 · 新投稿自动进入下载队列`;
  }
}

export async function upAdd() {
  const input = document.getElementById('up-add-input');
  const v = input.value.trim();
  if (!v) { alert('请粘贴 UP 主页链接或 mid'); return; }
  try {
    const r = await postJSON('/api/up-list/add', { input: v });
    if (!r.ok) throw new Error(r.error || '添加失败');
    input.value = '';
    alert(`已加入清单:${r.up.uname || ('UP ' + r.up.mid)}\n之后 TA 的新投稿会自动进入下载队列。`);
    if (state.mode === 'feed' && _feedSource === 'mine') loadMyUps();
  } catch (e) { alert(e.message); }
}

export async function upSetQuality(mid, quality) {
  try {
    const r = await postJSON('/api/up-list/set-quality', { mid: parseInt(mid), quality });
    if (!r.ok) { alert('设置失败'); loadMyUps(); }
  } catch (e) { alert(e.message); }
}

export async function upRemove(mid) {
  if (!confirm('从清单移除后,该 UP 的新投稿不再自动下载。确定移除?')) return;
  const r = await postJSON('/api/up-list/remove', { mid: parseInt(mid) });
  if (r.ok && state.mode === 'feed' && _feedSource === 'mine') loadMyUps();
}

export async function upCheckNow(btn) {
  try {
    await postEmpty('/api/up-list/check');
    if (btn) {
      const old = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>检查中...';
      // 串行检查耗时取决于 UP 数,先给个固定反馈窗口,期间队列轮询会带回新任务
      setTimeout(() => { btn.disabled = false; btn.innerHTML = old; }, 30000);
    }
  } catch (e) { alert(e.message); }
}

/* ---------- 页控制器 ---------- */
registerPage('feed', {
  load: () => {
    if (_feedSource === 'bili') loadFollowBili();
    else if (_feedSource === 'all') loadFollowAll();
    else loadMyUps();
  },
  retry: () => retryFeed(),
  render: (d) => {
    if (_feedSource === 'all') { renderFollowAll(); maybeFillScreen(); }
    else if (_feedSource === 'mine') { renderMyUps(); }
    else renderDefaultList(d);
  },
  infinite: () => (_feedSource === 'mine' ? 'none' : (_feedSource === 'all' ? 'own' : 'items')),
  advanceChunk: () => (_feedSource === 'all' ? advanceFollowAll() : undefined),
  chunk: () => (_feedSource === 'bili' ? defaultChunk() : upCardChunk()),
  actionBarVisible: () => _feedSource === 'bili',
  emptyHint: (q) => q ? '没有匹配结果(试试其他关键词)' : '暂无内容(可能 B 站限流或账号无数据)',
  onViewToggle: () => { _followAllShown = 0; },
});

/* 各源的错误态重试(错误 HTML 里内联 onclick 引用这些名字) */
function retryFeed() {
  if (_feedSource === 'bili') loadFollowBili();
  else if (_feedSource === 'all') loadFollowAll();
  else loadMyUps();
}

export { loadFollowBili, loadFollowAll };
