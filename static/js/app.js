const API = (p) => p;  // 同源
let _qrPollTimer = null;
let _activeSSE = {};
let DL_DIR = '~/Downloads/bilibili';

/* ---------- SVG 图标(动态渲染处使用) ---------- */
const _I = (p) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
const ICONS = {
  refresh: _I('<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'),
  list: _I('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>'),
  grid: _I('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>'),
  sun: _I('<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>'),
  moon: _I('<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>'),
  check: _I('<polyline points="20 6 9 17 4 12"/>'),
  chevron: _I('<polyline points="9 18 15 12 9 6"/>'),
  play: _I('<polygon points="7 4 19 12 7 20 7 4" fill="currentColor" stroke="none"/>'),
  folder: _I('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),
  layers: _I('<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>'),
};
const icon = (name) => ICONS[name] || '';

/* ---------- 抽屉 ---------- */
function openDrawer(name) {
  document.querySelectorAll('.drawer-panel').forEach(p => p.classList.remove('active'));
  const panel = document.getElementById('dpanel-' + name);
  if (!panel) return;
  panel.classList.add('active');
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-mask').classList.add('open');
  if (name === 'account') loadConfigIntoUI();
}
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-mask').classList.remove('open');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

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

/* ---------- 页面路由 ---------- */
const LIB_PAGES = ['downloads', 'history', 'feed', 'seasons', 'fav', 'later'];
let _libMode = 'history';

function goPage(name, skipLoad) {
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.page === name));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.dataset.page === name));
  document.querySelector('.content').scrollTop = 0;  // 换页回顶
  if (LIB_PAGES.includes(name)) {
    _libMode = name;
    _libItems = [];
    _shown = 0;
    _libChecked.clear();
    if (name === 'fav') { _favCurrent = null; favShowFolders(); }
    if (name === 'seasons') { _seasonsCurrent = null; seasonShowOverview(); }
    const pg = document.querySelector(`.page[data-page="${name}"]`);
    const s = pg && pg.querySelector('.lib-search');
    if (s) s.value = '';
    if (!skipLoad) refreshLib();
  }
}

/* ---------- 登录状态 ---------- */
let _loggedIn = false;

async function refreshLogin() {
  try {
    const r = await (await fetch(API('/api/login/status'))).json();
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

async function logout() {
  if (!confirm('确定退出登录?')) return;
  await fetch(API('/api/login/logout'), { method: 'POST' });
  await refreshLogin();
  document.getElementById('qrcode-img').innerHTML = '';
  document.getElementById('qr-status').textContent = '';
}

/* ---------- 扫码登录 ---------- */
async function showQrcode() {
  const imgBox = document.getElementById('qrcode-img');
  const statusBox = document.getElementById('qr-status');
  imgBox.innerHTML = '<div class="empty"><span class="spinner"></span>生成中...</div>';
  statusBox.textContent = '';
  try {
    const r = await (await fetch(API('/api/login/qrcode'))).json();
    if (!r.ok) throw new Error(r.error || '生成失败');
    imgBox.innerHTML = `<img src="${API('/api/login/qrcode-image')}?url=${encodeURIComponent(r.url)}" width="220" height="220" alt="二维码">`;
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
    const r = await (await fetch(API('/api/login/qrcode/poll?key=' + key))).json();
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

async function useBrowser() {
  try {
    await fetch(API('/api/login/use-browser'), { method: 'POST' });
    document.getElementById('qr-status').textContent = '已切换为 Edge 登录态模式';
    refreshLogin();
  } catch (e) { alert('切换失败: ' + e.message); }
}

async function uploadCookieTxt() {
  const text = document.getElementById('cookie-txt-input').value.trim();
  if (!text) { alert('请粘贴 cookies.txt 内容'); return; }
  try {
    const r = await (await fetch(API('/api/login/cookie-txt'), { method: 'POST', body: text })).json();
    if (r.ok) { alert('导入成功'); refreshLogin(); }
    else { alert('导入失败: ' + (r.error || '未知错误')); }
  } catch (e) { alert('导入失败: ' + e.message); }
}

/* ---------- 解析:单视频/批量自动判断 ---------- */
let _singleUrl = '';
let _singleInfo = null;  // 最近一次解析结果(带 uploader_mid,下载时回传)

async function smartParse() {
  const text = document.getElementById('dl-input').value.trim();
  if (!text) { alert('请粘贴链接'); return; }
  const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
  const isList = lines.length > 1 || /favlist|medialist|watchlater|space\.bilibili/i.test(lines[0]);
  if (isList) batchParse(lines); else parseVideo(lines[0]);
}

async function parseVideo(url) {
  const btn = document.getElementById('parse-btn');
  const box = document.getElementById('parse-result');
  btn.disabled = true;
  box.innerHTML = '<div class="empty"><span class="spinner"></span>解析中...</div>';
  document.getElementById('batch-result').innerHTML = '';
  try {
    const r = await (await fetch(API('/api/parse'), {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ url })
    })).json();
    if (!r.ok) throw new Error(r.error || '解析失败');
    _singleUrl = url;
    _singleInfo = r.info;
    renderParseResult(r.info, url, r.logged_in);
  } catch (e) {
    box.innerHTML = '<div class="empty">' + e.message + '</div>';
  } finally {
    btn.disabled = false;
  }
}

function renderParseResult(info, url, loggedIn) {
  const box = document.getElementById('parse-result');
  let warnHtml = '';
  if (!loggedIn) {
    warnHtml = '<div class="warn">未登录,可能只能下载公开画质。如需 1080P+/4K 大会员画质,请先登录。</div>';
  }
  if (info.is_multi_part && info.parts && info.parts.length > 0) {
    const partOptions = info.parts.map(p =>
      `<option value="${p.page}">P${p.page} · ${escapeHtml(p.title)} (${formatDuration(p.duration)})</option>`
    ).join('');
    box.innerHTML = `
      ${warnHtml}
      <div class="video-card">
        <div class="video-info">
          <div class="vt">${escapeHtml(info.title)} <span style="color:var(--accent);font-size:12.5px;">(共 ${info.parts.length} P)</span></div>
          <div class="vm">UP主:${escapeHtml(info.uploader || '-')}</div>
          <div class="btn-row">
            <select id="part-select" onchange="loadPartFormats('${escapeAttr(url)}')">${partOptions}</select>
          </div>
          <div id="part-formats" style="margin-top:10px;"><div class="hint"><span class="spinner"></span>加载清晰度...</div></div>
        </div>
      </div>`;
    loadPartFormats(url);
    return;
  }
  box.innerHTML = `
    ${warnHtml}
    <div class="video-card">
      <img src="${info.thumbnail || ''}" onerror="this.style.visibility='hidden'">
      <div class="video-info">
        <div class="vt">${escapeHtml(info.title)}</div>
        <div class="vm">UP主:${escapeHtml(info.uploader || '-')} ${info.duration ? '· 时长:' + formatDuration(info.duration) : ''}</div>
        ${qualitySelectHtml(info.formats, info.best_label)}
        <div class="btn-row" style="margin-top:10px;">
          <button class="primary" onclick="startDownload()">开始下载</button>
        </div>
      </div>
    </div>`;
}

async function loadPartFormats(url) {
  const page = document.getElementById('part-select').value;
  const fbox = document.getElementById('part-formats');
  fbox.innerHTML = '<div class="hint"><span class="spinner"></span>加载清晰度...</div>';
  try {
    const r = await (await fetch(API('/api/parse-part'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url, page: parseInt(page) })
    })).json();
    if (!r.ok) throw new Error(r.error || '加载失败');
    _singleInfo = r.info;
    fbox.innerHTML = `
      ${qualitySelectHtml(r.info.formats, r.info.best_label)}
      <div class="btn-row" style="margin-top:10px;"><button class="primary" onclick="startDownload()">开始下载</button></div>`;
  } catch (e) {
    fbox.innerHTML = '<div class="hint">' + e.message + '</div>';
  }
}

function qualitySelectHtml(formats, bestLabel) {
  const videoOnly = formats.filter(f => f.kind === 'video' || f.kind === 'av');
  let options = videoOnly.map(f => {
    const sel = (f.label === bestLabel) ? 'selected' : '';
    const sizeTxt = f.size_mb ? ` (${f.size_mb} MB)` : '';
    return `<option value="${f.label}" ${sel}>${f.label}${sizeTxt}</option>`;
  }).join('');
  const audio = formats.find(f => f.kind === 'audio');
  if (audio) {
    const sizeTxt = audio.size_mb ? ` (${audio.size_mb} MB)` : '';
    options += `<option value="音频">仅音频${sizeTxt}</option>`;
  }
  if (!options) return '<div class="hint">无可用清晰度</div>';
  return `<div class="btn-row" style="margin-top:8px;"><select id="quality-select">${options}</select></div>`;
}

/* ---------- 下载 ---------- */
async function startDownload() {
  const url = _singleUrl;
  const label = document.getElementById('quality-select')?.value || '';
  const partSel = document.getElementById('part-select');
  const page = partSel ? parseInt(partSel.value) : 1;
  if (!url || !label) return;
  try {
    const r = await (await fetch(API('/api/download'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url, label, page, uploader_mid: (_singleInfo && _singleInfo.uploader_mid) || 0 })
    })).json();
    if (!r.ok) {
      if (r.task_id) alert(r.error + ',请在「下载记录」查看该任务');
      else alert(r.error || '启动下载失败');
      return;
    }
    subscribeProgress(r.task_id);
    closeDrawer();
    goPage('downloads');
  } catch (e) { alert(e.message); }
}

/* ---------- 批量下载 ---------- */
let _batchItems = [];

async function batchParse(lines) {
  const box = document.getElementById('batch-result');
  const btn = document.getElementById('parse-btn');
  btn.disabled = true;
  document.getElementById('parse-result').innerHTML = '';
  box.innerHTML = '<div class="empty"><span class="spinner"></span>解析中...(收藏夹/合集可能需要一点时间)</div>';
  const isSingleList = lines.length === 1;
  try {
    let r;
    if (isSingleList) {
      r = await (await fetch(API('/api/batch/parse'), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ url: lines[0] })
      })).json();
    } else {
      r = await (await fetch(API('/api/batch/parse'), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ urls: lines })
      })).json();
    }
    if (!r.ok) throw new Error(r.error || '解析失败');
    _batchItems = r.items || [];
    renderBatchList(r.title || (isSingleList ? '收藏夹/合集' : '多链接'));
  } catch (e) {
    box.innerHTML = '<div class="empty">' + e.message + '</div>';
  } finally {
    btn.disabled = false;
  }
}

function renderBatchList(title) {
  const box = document.getElementById('batch-result');
  if (_batchItems.length === 0) {
    box.innerHTML = '<div class="empty">未解析出任何视频</div>';
    return;
  }
  const itemRows = _batchItems.map((it, i) => `
    <label class="batch-check-row">
      <input type="checkbox" data-bidx="${i}" checked>
      <span class="bc-title">${escapeHtml(it.title)}</span>
      ${it.duration ? `<span class="bc-meta">${formatDuration(it.duration)}</span>` : ''}
      ${it.error ? `<span class="bc-meta" style="color:var(--red);">${escapeHtml(it.error)}</span>` : ''}
    </label>`).join('');
  box.innerHTML = `
    <div class="hint" style="margin:14px 0 8px;">「${escapeHtml(title)}」共 ${_batchItems.length} 项</div>
    <div class="btn-row" style="margin-bottom:8px;">
      <button class="ghost sm" onclick="batchToggleAll(true)">全选</button>
      <button class="ghost sm" onclick="batchToggleAll(false)">全不选</button>
      <span style="color:var(--text-mute);font-size:12px;">已选 <b id="batch-count">0</b> 项</span>
    </div>
    <div style="max-height:280px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;">
      ${itemRows}
    </div>
    <div class="btn-row" style="margin-top:10px;">
      <select id="batch-quality">
        <option value="">最高画质(自动)</option>
        <option value="1080P">1080P</option>
        <option value="720P">720P</option>
        <option value="480P">480P</option>
        <option value="360P">360P</option>
        <option value="音频">仅音频</option>
      </select>
      <button class="primary" onclick="batchDownload()">批量下载</button>
    </div>
    <div class="hint">批量下载按设置的并发数同时进行,其余排队;已下载过的项会自动跳过。</div>`;
  document.querySelectorAll('input[data-bidx]').forEach(cb => cb.addEventListener('change', updateBatchCount));
  updateBatchCount();
}

function batchToggleAll(checked) {
  document.querySelectorAll('input[data-bidx]').forEach(cb => cb.checked = checked);
  updateBatchCount();
}

function updateBatchCount() {
  const n = document.querySelectorAll('input[data-bidx]:checked').length;
  const el = document.getElementById('batch-count');
  if (el) el.textContent = n;
}

async function batchDownload() {
  const checked = [...document.querySelectorAll('input[data-bidx]:checked')];
  if (checked.length === 0) { alert('请至少勾选一项'); return; }
  const label = document.getElementById('batch-quality').value;
  let items = checked.map(cb => {
    const idx = parseInt(cb.dataset.bidx);
    const it = _batchItems[idx];
    return { url: it.url, title: it.title, page: it.url.includes('?p=') ? parseInt(it.url.split('?p=')[1]) : 1,
             thumbnail: it.thumbnail || '', uploader: it.uploader || '',
             uploader_mid: it.uploader_mid || 0, duration: it.duration || 0 };
  });
  const skipped = items.filter(it => isUrlDownloaded(it.url)).length;
  items = items.filter(it => !isUrlDownloaded(it.url));
  if (items.length === 0) { alert('所选项均已下载过'); return; }
  try {
    const r = await (await fetch(API('/api/batch/download'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ items, label })
    })).json();
    if (!r.ok) throw new Error(r.error || '启动失败');
    alert(`已加入 ${r.count} 个下载任务${skipped ? `(跳过 ${skipped} 个已下载)` : ''}`);
    r.task_ids.forEach(subscribeProgress);
  } catch (e) { alert(e.message); }
}

/* ---------- 列表页公共(搜索/渲染/无限滚) ---------- */
let _libItems = [];

const withIdx = (arr) => arr.map((it, idx) => ({ it, idx }));

/* 每页条数自适应:列数按 CSS auto-fill 规则由容器宽度推,行数按视口高度推。
   网格首屏刻意多渲染一行——正好铺满一屏时滚动条不出现,scroll 事件永远
   不触发,无限滚会卡死(列表视图不受影响,由用户自选条数)。
   rowH 为单行卡片高度:视频卡约 232,UP 行卡约 76。 */
function computePageSize(minCol = 200, gap = 14, rowH = 232) {
  if (_libView === 'list') {
    return Math.max(10, parseInt(localStorage.getItem('bd_list_size') || '30', 10));
  }
  const d = libDom();
  const w = (d && d.list) ? d.list.clientWidth : 1064;
  const cols = Math.max(2, Math.floor((w + gap) / (minCol + gap)));
  const rows = Math.max(2, Math.floor((window.innerHeight - 150) / rowH) + 1);
  return cols * rows;
}

function scrollToTop() {
  document.querySelector('.content').scrollTo({ top: 0, behavior: 'smooth' });
}

function libDom() {
  const m = _libMode;
  return {
    list: document.getElementById('list-' + m),
    meta: document.getElementById('meta-' + m),
  };
}

/* 所有列表页统一无限滚:首屏渲染 _shown 条,滚到底追加一块;
   历史页本地加载完后还能向服务器继续要更早的。 */
let _shown = 0;

/* UP 卡片类视图(关注列表 / 自动下载清单)不走视频卡那套分块 */
const isUpCardsView = () => _libMode === 'feed' && _feedSource !== 'bili';

function currentChunk() {
  if (isUpCardsView()) {
    return _libView === 'list' ? computePageSize() : computePageSize(240, 12, 76);
  }
  return _libView === 'list' ? computePageSize() : computePageSize(200, 14, 232);
}

function renderPage() {
  const d = libDom();
  if (!d.list) return;
  if (_libMode === 'feed' && _feedSource === 'all') { renderFollowAll(); maybeFillScreen(); return; }
  if (_libMode === 'feed' && _feedSource === 'mine') { renderMyUps(); return; }
  // 收藏夹/追更合集首屏(卡片视图):切视图/resize 时也要重渲染
  if (_libMode === 'fav' && !_favCurrent) { renderFavFolders(); return; }
  if (_libMode === 'seasons' && !_seasonsCurrent) { renderSeasonsOverview(); return; }
  const q = getLibSearchQuery();
  let filtered = _libItems;
  if (q) {
    filtered = _libItems.filter(it => {
      const t = (it.title || '').toLowerCase();
      const u = (it.uploader || '-').toLowerCase();
      return t.includes(q) || u.includes(q);
    });
  }
  if (!_shown) _shown = currentChunk();
  const source = q ? filtered : _libItems.slice(0, _shown);
  const pairs = [];
  source.forEach((it, idx) => pairs.push({ it, idx }));
  renderLibList(pairs, d.list);
  if (d.meta) {
    d.meta.textContent = q
      ? `共 ${_libItems.length} 条 · 匹配 ${filtered.length}`
      : (_libMode === 'history' ? '' : `共 ${_libItems.length} 条`);
  }
  maybeFillScreen();
}

/* 内容不足一屏时滚动条不出现、scroll 事件不触发,无限滚会卡死;
   这里主动补块直到可滚动或没有更多(历史还能向服务器要更早的)。 */
let _filling = false;
function maybeFillScreen() {
  if (_filling || getLibSearchQuery()) return;
  if (_libMode === 'feed' && _feedSource === 'mine') return;  // UP 清单一次全渲染
  if (_libMode === 'fav' && !_favCurrent) return;             // 收藏夹首屏无分页
  if (_libMode === 'seasons' && !_seasonsCurrent) return;     // 合集首屏无分页
  const sc = document.querySelector('.content');
  if (!sc || sc.scrollHeight > sc.clientHeight + 2) return;
  _filling = true;
  try {
    for (let i = 0; i < 10 && sc.scrollHeight <= sc.clientHeight + 2; i++) {
      if (_libMode === 'feed' && _feedSource === 'all') {
        if (_followAllShown >= _followAll.length) break;
        _followAllShown += currentChunk();
        renderFollowAll();
      } else {
        if (_shown >= _libItems.length) {
          if (_libMode === 'history' && _histHasMore) maybeLoadMoreHistory();
          break;
        }
        _shown += currentChunk();
        renderLibList(_libItems.slice(0, _shown).map((it, idx) => ({ it, idx })), libDom().list);
      }
    }
  } finally { _filling = false; }
}

function maybeLoadMore() {
  if (getLibSearchQuery()) return;
  if (_libMode === 'feed' && _feedSource === 'all') { maybeLoadMoreFollowAll(); return; }
  if (_libMode === 'feed' && _feedSource === 'mine') return;
  if (_libMode === 'fav' && !_favCurrent) return;
  if (_libMode === 'seasons' && !_seasonsCurrent) return;
  if (!LIB_PAGES.includes(_libMode)) return;
  if (_shown < _libItems.length) {
    _shown += currentChunk();
    renderPage();
    return;
  }
  // 历史:本地展示完了,向服务器要更早的
  if (_libMode === 'history') maybeLoadMoreHistory();
}

/* ---------- 观看历史 ---------- */
let _histServerPage = 0;
let _histHasMore = false;

async function loadHistory() {
  const d = libDom();
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>正在获取...</div>';
  d.meta.textContent = '';
  _libItems = [];
  _shown = 0;
  _histServerPage = 0;
  await fetchHistoryPage();
}

async function fetchHistoryPage() {
  try {
    const r = await (await fetch(API(`/api/history?page=${_histServerPage + 1}&size=100`))).json();
    if (!r.ok) throw new Error(r.error || '加载失败');
    _histServerPage++;
    const items = r.items || [];
    _histHasMore = items.length === 100;
    if (_histServerPage === 1) {
      _libItems = items;
    } else {
      const seen = new Set(_libItems.map(it => it.bvid));
      _libItems = _libItems.concat(items.filter(it => !seen.has(it.bvid)));
    }
    renderPage();
  } catch (e) {
    libDom().list.innerHTML =
      `<div class="empty">${e.message}<br><button class="ghost sm" onclick="loadHistory()">${icon('refresh')} 重新加载</button></div>`;
    _histHasMore = false;
  }
}

let _histLoading = false;

async function maybeLoadMoreHistory() {
  // 滚到底自动加载更早历史;没有更多/正在加载/搜索中时不触发
  if (_libMode !== 'history' || !_histHasMore || _histLoading) return;
  if (getLibSearchQuery()) return;
  _histLoading = true;
  _shown += currentChunk();
  await fetchHistoryPage();
  _histLoading = false;
}

/* ---------- 统一「缓存优先 → 后台拉新 → 有新数据提示」 ----------
   打开页面立即渲染 /api/cache 的上次数据(绝不干等),同时后台请求真接口;
   拉到的数据和当前展示的不一样时,页面顶部提示「查看最新」,不打断浏览。 */
const _freshApply = {};  // page -> 应用新数据的函数

function _sig(arr) {
  if (!Array.isArray(arr) || arr.length === 0) return "0";
  const id = (x) => x.bvid || x.mid || x.id || x.url || "";
  return arr.length + "|" + id(arr[0]) + "|" + id(arr[arr.length - 1]);
}

function showFreshHint(page, applyFn) {
  const pg = document.querySelector(`.page[data-page="${page}"]`);
  if (!pg) return;
  _freshApply[page] = applyFn;
  let bar = pg.querySelector('.fresh-hint');
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'fresh-hint';
    (pg.querySelector('.sub-info') || pg.querySelector('.page-head')).after(bar);
  }
  bar.innerHTML = `<span class="fh-text">获取到新内容</span>
    <button class="primary sm" onclick="applyFresh('${page}')">查看最新</button>
    <button class="quiet sm" onclick="dismissFresh('${page}')">暂不</button>`;
  bar.style.display = '';
}

function applyFresh(page) {
  const fn = _freshApply[page];
  dismissFresh(page);
  if (fn) fn();
}

function dismissFresh(page) {
  const pg = document.querySelector(`.page[data-page="${page}"]`);
  const bar = pg && pg.querySelector('.fresh-hint');
  if (bar) bar.style.display = 'none';
  delete _freshApply[page];
}

/* opts: { page, api, cacheKey, extract(body)->数组, apply(body), stillValid()->bool,
           fast?:bool(快数据源:拉新后直接无感替换,不弹提示条),
           showLoading()?, showError(msg)? }
   缓存渲染每次都做;同源后台请求在途时不叠加(切走再切回靠缓存立即渲染,
   在途请求完成后按 stillValid 决定直接应用或提示)。
   慢源(关注动态串行几十秒)有变化只提示不打断;快源(合集/收藏夹/稍后再看,
   单接口几百毫秒)提示条反而是噪音——重启后首开必弹一次(基准是上次会话的
   缓存),无感替换才对齐官网「打开即最新」。 */
const _bgInFlight = {};  // cacheKey -> bool
async function cachedLoad(opts) {
  const { page, api, cacheKey, extract, apply, stillValid } = opts;
  let shown = null;
  try {
    const r = await (await fetch(API('/api/cache/' + cacheKey))).json();
    if (r.ok && r.data && r.data.ok) {
      shown = extract(r.data);
      if (stillValid()) apply(r.data);
    }
  } catch (e) { /* 无缓存,走加载态 */ }
  if (shown === null && opts.showLoading && stillValid()) opts.showLoading();
  if (_bgInFlight[cacheKey]) return;
  _bgInFlight[cacheKey] = true;
  try {
    const r2 = await (await fetch(API(api))).json();
    if (!r2.ok) {
      if (shown === null && opts.showError) opts.showError(r2.error || '加载失败');
      return;
    }
    const fresh = extract(r2);
    if (shown === null) { if (stillValid()) apply(r2); return; }
    if (_sig(fresh) !== _sig(shown) && stillValid()) {
      // 快源无感替换;但用户已勾选条目时绝不静默重渲染(勾选索引会错位),退回提示
      if (opts.fast && _libChecked.size === 0) apply(r2);
      else showFreshHint(page, () => { if (stillValid()) apply(r2); });
    }
  } catch (e) {
    if (shown === null && opts.showError) opts.showError(e.message);
  } finally { _bgInFlight[cacheKey] = false; }
}

/* ---------- UP 更新(关注动态 / 关注列表 / 自动下载清单) ---------- */
let _feedSource = (() => {
  const s = localStorage.getItem('bd_feed_src');
  return (s === 'all' || s === 'mine') ? s : 'bili';  // 旧值 'up' 已废弃
})();
let _myUps = [];

function setFeedSource(src) {
  _feedSource = src;
  localStorage.setItem('bd_feed_src', src);
  document.querySelectorAll('#feed-seg button').forEach(b => b.classList.toggle('active', b.dataset.src === src));
  _libItems = [];
  _shown = 0;
  _followAllShown = 0;
  _libChecked.clear();
  dismissFresh('feed');
  document.getElementById('feed-mine-tools').style.display = src === 'mine' ? '' : 'none';
  refreshLib();
}

function syncFeedSeg() {
  document.querySelectorAll('#feed-seg button').forEach(b => b.classList.toggle('active', b.dataset.src === _feedSource));
  const tools = document.getElementById('feed-mine-tools');
  if (tools) tools.style.display = _feedSource === 'mine' ? '' : 'none';
}

async function loadFollowBili() {
  // 缓存优先(串行拉 30 个 UP 要几十秒,绝不让人干等);后台拉新,有变化提示
  await cachedLoad({
      page: 'feed',
      api: '/api/follow-updates',
      cacheKey: 'follow_updates',
      extract: (b) => b.items || [],
      stillValid: () => _libMode === 'feed' && _feedSource === 'bili',
      showLoading: () => {
        const d = libDom();
        d.meta.textContent = '';
        d.list.innerHTML = '<div class="empty">正在后台拉取关注 UP 的近期投稿(串行防限流,约需数十秒),完成后自动展示。</div>';
      },
      showError: (msg) => {
        libDom().list.innerHTML = `<div class="empty">${msg}<br><button class="ghost sm" onclick="loadFollowBili()">${icon('refresh')} 重试</button></div>`;
      },
      apply: (b) => {
        _libItems = b.items || [];
        renderPage();
        libDom().meta.textContent = `共 ${_libItems.length} 条`;
      },
    });
}

/* ---------- 自动下载清单(页面直接管理:添加/清晰度/移除/立即检查) ---------- */
async function loadMyUps() {
  const d = libDom();
  d.meta.textContent = '';
  try {
    const r = await (await fetch(API('/api/up-list'))).json();
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
    d.list.innerHTML = `<div class="${_libView === 'grid' ? 'up-grid' : 'up-list'}">` + ups.map(u => `
      <div class="up-card mgr">
        <img src="${escapeHtml(u.face)}" onerror="this.style.visibility='hidden'">
        <div class="uc-name" title="${escapeHtml(u.uname)}"><a href="https://space.bilibili.com/${u.mid}" target="_blank" rel="noopener">${escapeHtml(u.uname || ('UP ' + u.mid))}</a></div>
        <span class="dl-badge">${icon('check')}自动下载</span>
        <div class="uc-ctl">
          ${upQualitySelect(u.mid, u.quality || '')}
          <button class="danger sm" onclick="upRemove(${u.mid})">移除</button>
        </div>
      </div>`).join('') + '</div>';
  }
  if (d.meta) {
    d.meta.textContent = q
      ? `清单 ${_myUps.length} 个 UP · 匹配 ${ups.length}`
      : `共 ${_myUps.length} 个 UP 在自动下载名单 · 新投稿自动进入下载队列`;
  }
}

/* ---------- 我的收藏(收藏夹列表,一级页) ---------- */
let _favFolders = [];
let _favCurrent = null;  // 已进入的收藏夹 id

function favShowFolders() {
  document.getElementById('fav-folder-list').style.display = '';
  document.getElementById('list-fav').style.display = 'none';
  document.getElementById('fav-back-btn').style.display = 'none';
  document.getElementById('fav-search').style.display = '';
  document.getElementById('fav-page-title').textContent = '我的收藏';
  document.getElementById('meta-fav').textContent =
    _favFolders.length ? `共 ${_favFolders.length} 个收藏夹` : '';
}

async function favLoadFolders() {
  _favCurrent = null;
  favShowFolders();
  await cachedLoad({
    page: 'fav',
    api: '/api/fav-folders',
    cacheKey: 'fav_folders',
    fast: true,
    extract: (b) => b.folders || [],
    stillValid: () => _libMode === 'fav' && !_favCurrent,
    showLoading: () => {
      document.getElementById('fav-folder-list').innerHTML =
        '<div class="empty"><span class="spinner"></span>加载收藏夹列表...</div>';
    },
    showError: (msg) => {
      document.getElementById('fav-folder-list').innerHTML =
        `<div class="empty">${msg}<br><button class="ghost sm" onclick="favLoadFolders()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _favFolders = b.folders || [];
      favShowFolders();
      renderFavFolders();
    },
  });
}

/* 我的收藏首屏:收藏夹卡片/行,支持列表/网格两种视图与标题搜索 */
function renderFavFolders() {
  const box = document.getElementById('fav-folder-list');
  if (!box) return;
  const q = getLibSearchQuery();
  let list = _favFolders;
  if (q) list = _favFolders.filter(f => (f.title || '').toLowerCase().includes(q));
  const metaEl = document.getElementById('meta-fav');
  if (metaEl) {
    metaEl.textContent = q
      ? `共 ${_favFolders.length} 个收藏夹 · 匹配 ${list.length}`
      : (_favFolders.length ? `共 ${_favFolders.length} 个收藏夹` : '');
  }
  if (list.length === 0) {
    box.innerHTML = `<div class="empty">${q ? '没有匹配的收藏夹(试试其他关键词)' : '暂无收藏夹'}</div>`;
    return;
  }
  box.innerHTML = _libView === 'grid'
    ? `<div class="lib-grid">${list.map(favFolderCard).join('')}</div>`
    : `<div class="panel"><div class="panel-body tight">${list.map(favFolderRow).join('')}</div></div>`;
}

const _favCard = (cover, count, title, subHtml, onclickAttr) => `
  <div class="fav-card" onclick="${onclickAttr}">
    <div class="fc-cover">
      <img src="${escapeHtml(cover)}" onerror="this.style.visibility='hidden'" loading="lazy">
      <span class="fc-count">${count} 个视频</span>
    </div>
    <div class="fc-title">${escapeHtml(title)}</div>
    <div class="fc-sub">${subHtml}</div>
  </div>`;

const favFolderCard = (f) =>
  _favCard(f.cover || '', f.media_count || 0, f.title || '(未命名)',
           icon('folder') + '收藏夹', `favEnter(${f.id})`);

const favSeasonCard = (s) =>
  _favCard(s.cover || '', s.media_count || 0, s.title || '(未命名)',
           icon('layers') + '追更中', `seasonEnter(${s.id})`);

function favFolderRow(f) {
  return `
    <div class="fav-folder" onclick="favEnter(${f.id})">
      <div class="fav-icon">${escapeHtml((f.title || '?').slice(0, 1))}</div>
      <div>
        <div class="fav-name">${escapeHtml(f.title)}</div>
        <div class="fav-count">${f.media_count || 0} 个视频</div>
      </div>
      <span class="fav-arrow">${icon('chevron')}</span>
    </div>`;
}

function favSeasonRow(s) {
  return `
    <div class="fav-folder" onclick="seasonEnter(${s.id})">
      <div class="fav-icon">${icon('layers')}</div>
      <div>
        <div class="fav-name">${escapeHtml(s.title)}<span class="fav-tag">追更中</span></div>
        <div class="fav-count">${s.media_count || 0} 个视频</div>
      </div>
      <span class="fav-arrow">${icon('chevron')}</span>
    </div>`;
}

function _favShowList(title) {
  document.getElementById('fav-folder-list').style.display = 'none';
  document.getElementById('list-fav').style.display = '';
  document.getElementById('fav-back-btn').style.display = '';
  document.getElementById('fav-search').style.display = '';
  document.getElementById('fav-page-title').textContent = title;
}

async function favEnter(fid) {
  // fid 参数实际传的是文件夹的媒体列表 id(长 id),接口按 media_id 查
  const folder = _favFolders.find(f => f.id === fid);
  if (!folder) return;
  _favCurrent = fid;
  _favShowList(folder.title);
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载收藏夹内容...</div>';
  try {
    const r = await (await fetch(API(`/api/fav-content?media_id=${fid}`))).json();
    if (!r.ok) throw new Error(r.error || '加载失败');
    _libItems = r.items || [];
    _libChecked.clear();
    _shown = 0;
    renderPage();
    d.meta.textContent = `共 ${_libItems.length} 条`;
  } catch (e) {
    d.list.innerHTML = `<div class="empty">${e.message}<br><button class="ghost sm" onclick="favEnter(${fid})">${icon('refresh')} 重试</button></div>`;
  }
}

function favBack() {
  favLoadFolders();
}

function favRefresh() {
  if (_favCurrent) favEnter(_favCurrent);
  else favLoadFolders();
}

/* ---------- 追更合集(独立一级页) ---------- */
let _seasons = [];
let _seasonsCurrent = null;  // 已进入的合集 season_id

function seasonShowOverview() {
  document.getElementById('season-overview').style.display = '';
  document.getElementById('list-seasons').style.display = 'none';
  document.getElementById('season-back-btn').style.display = 'none';
  document.getElementById('season-page-title').textContent = '追更合集';
  document.getElementById('meta-seasons').textContent =
    _seasons.length ? `共 ${_seasons.length} 个追更中的合集` : '';
}

async function loadSeasons() {
  _seasonsCurrent = null;
  seasonShowOverview();
  await cachedLoad({
    page: 'seasons',
    api: '/api/fav-seasons',
    cacheKey: 'fav_seasons',
    fast: true,
    extract: (b) => b.seasons || [],
    stillValid: () => _libMode === 'seasons' && !_seasonsCurrent,
    showLoading: () => {
      document.getElementById('season-overview').innerHTML =
        '<div class="empty"><span class="spinner"></span>加载追更合集...</div>';
    },
    showError: (msg) => {
      document.getElementById('season-overview').innerHTML =
        `<div class="empty">${msg}<br><button class="ghost sm" onclick="loadSeasons()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _seasons = b.seasons || [];
      seasonShowOverview();
      renderSeasonsOverview();
    },
  });
}

/* 追更合集首屏:合集卡片/行,可按标题搜索 */
function renderSeasonsOverview() {
  const box = document.getElementById('season-overview');
  if (!box) return;
  const q = getLibSearchQuery();
  let list = _seasons;
  if (q) list = _seasons.filter(s => (s.title || '').toLowerCase().includes(q));
  const metaEl = document.getElementById('meta-seasons');
  if (metaEl) {
    metaEl.textContent = q
      ? `共 ${_seasons.length} 个合集 · 匹配 ${list.length}`
      : (_seasons.length ? `共 ${_seasons.length} 个追更中的合集` : '');
  }
  if (list.length === 0) {
    box.innerHTML = `<div class="empty">${q ? '没有匹配结果(试试其他关键词)' : '还没有追的合集。在 B 站视频页点「追合集」后,会出现在这里。'}</div>`;
    return;
  }
  box.innerHTML = _libView === 'grid'
    ? `<div class="lib-grid">${list.map(favSeasonCard).join('')}</div>`
    : `<div class="panel"><div class="panel-body tight">${list.map(favSeasonRow).join('')}</div></div>`;
}

async function seasonEnter(sid) {
  const season = _seasons.find(s => s.id === sid);
  if (!season) return;
  _seasonsCurrent = sid;
  document.getElementById('season-overview').style.display = 'none';
  document.getElementById('list-seasons').style.display = '';
  document.getElementById('season-back-btn').style.display = '';
  document.getElementById('season-page-title').textContent = season.title;
  const d = libDom();
  d.meta.textContent = '';
  d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载合集内容...</div>';
  try {
    const r = await (await fetch(API(`/api/season-content?season_id=${sid}`))).json();
    if (!r.ok) throw new Error(r.error || '加载失败');
    _libItems = r.items || [];
    _libChecked.clear();
    _shown = 0;
    renderPage();
    d.meta.textContent = `共 ${_libItems.length} 条`;
  } catch (e) {
    d.list.innerHTML = `<div class="empty">${e.message}<br><button class="ghost sm" onclick="seasonEnter(${sid})">${icon('refresh')} 重试</button></div>`;
  }
}

function seasonBack() {
  loadSeasons();
}

function seasonRefresh() {
  if (_seasonsCurrent) seasonEnter(_seasonsCurrent);
  else loadSeasons();
}

/* ---------- UP 清单操作(管理界面在「自动下载」视图里) ---------- */
const QUALITY_OPTIONS = [
  ['', '最高画质(自动)'], ['1080P', '1080P'], ['720P', '720P'],
  ['480P', '480P'], ['360P', '360P'], ['音频', '仅音频'],
];

const upQualitySelect = (mid, cur) =>
  `<select onchange="upSetQuality(${mid}, this.value)" title="该 UP 新视频自动下载的默认清晰度">` +
  QUALITY_OPTIONS.map(([v, l]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`).join('') +
  `</select>`;

async function upAdd() {
  const input = document.getElementById('up-add-input');
  const v = input.value.trim();
  if (!v) { alert('请粘贴 UP 主页链接或 mid'); return; }
  try {
    const r = await (await fetch(API('/api/up-list/add'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ input: v })
    })).json();
    if (!r.ok) throw new Error(r.error || '添加失败');
    input.value = '';
    alert(`已加入清单:${r.up.uname || ('UP ' + r.up.mid)}\n之后 TA 的新投稿会自动进入下载队列。`);
    if (_libMode === 'feed' && _feedSource === 'mine') loadMyUps();
  } catch (e) { alert(e.message); }
}

async function upSetQuality(mid, quality) {
  try {
    const r = await (await fetch(API('/api/up-list/set-quality'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ mid, quality })
    })).json();
    if (!r.ok) { alert('设置失败'); loadMyUps(); }
  } catch (e) { alert(e.message); }
}

async function upRemove(mid) {
  if (!confirm('从清单移除后,该 UP 的新投稿不再自动下载。确定移除?')) return;
  const r = await (await fetch(API('/api/up-list/remove'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ mid })
  })).json();
  if (r.ok && _libMode === 'feed' && _feedSource === 'mine') loadMyUps();
}

async function upCheckNow(btn) {
  try {
    await fetch(API('/api/up-list/check'), { method: 'POST' });
    if (btn) {
      const old = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>检查中...';
      // 串行检查耗时取决于 UP 数,先给个固定反馈窗口,期间队列轮询会带回新任务
      setTimeout(() => { btn.disabled = false; btn.innerHTML = old; }, 30000);
    }
  } catch (e) { alert(e.message); }
}

/* ---------- 稍后再看 ---------- */
async function loadWatchlater() {
  await cachedLoad({
    page: 'later',
    api: '/api/watchlater',
    cacheKey: 'watchlater',
    fast: true,
    extract: (b) => b.items || [],
    stillValid: () => _libMode === 'later',
    showLoading: () => {
      const d = libDom();
      d.meta.textContent = '';
      d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载稍后再看...</div>';
    },
    showError: (msg) => {
      const d = libDom();
      d.list.innerHTML = `<div class="empty">${msg}<br><button class="ghost sm" onclick="loadWatchlater()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _libItems = b.items || [];
      renderPage();
      libDom().meta.textContent = `共 ${_libItems.length} 条`;
    },
  });
}

/* 各页面进入时的加载分发 */
function refreshLib() {
  if (_libMode === 'downloads') loadDownloads();
  else if (_libMode === 'feed') {
    if (_feedSource === 'bili') loadFollowBili();
    else if (_feedSource === 'all') loadFollowAll();
    else loadMyUps();
  }
  else if (_libMode === 'seasons') loadSeasons();
  else if (_libMode === 'fav') favLoadFolders();
  else if (_libMode === 'later') loadWatchlater();
  else loadHistory();
}

/* ---------- 下载记录(队列已并入:进行中任务在前,已完成在后) ---------- */
const TASK_ACTIVE = ['queued', 'downloading', 'merging', 'cancelling', 'retrying'];
const TASK_STATUS_CN = {
  queued: '排队中', downloading: '下载中', merging: '合并中',
  cancelling: '取消中', retrying: '重试中', failed: '失败',
  cancelled: '已取消', interrupted: '已中断',
};

async function loadDownloads() {
  const d = libDom();
  d.meta.textContent = '';
  try {
    const r = await (await fetch(API('/api/tasks'))).json();
    const tasks = r.tasks || [];
    _doneBvids = new Set(tasks.filter(t => t.status === 'done')
      .map(t => bvFromUrl(t.url)).filter(Boolean));
    updateDownloadsBadge(tasks);
    // 进行中在最前(时间倒序),其余(完成/失败/取消)按完成时间倒序
    _libItems = tasks.map(t => {
      const it = {
        task_id: t.task_id, status: t.status,
        url: t.url, title: t.title || '(解析中)', label: t.label || '',
        uploader: t.uploader || '', uploader_mid: t.uploader_mid || 0,
        thumbnail: t.thumbnail || '', duration: t.duration || 0,
        view_at: t.finished_at || t.created_at || 0,
        progress: t.progress || 0, speed: t.speed || 0, eta: t.eta || 0,
        error: t.error || '',
        rename_conflict: t.rename_conflict || null,
        rename_error: t.rename_error || '',
        rename_skipped: t.rename_skipped || '',
        _sort: (TASK_ACTIVE.includes(t.status) ? 0 : 1) * 1e10 - (t.finished_at || t.created_at || 0),
      };
      return it;
    }).sort((a, b) => a._sort - b._sort);
    renderPage();
    const doneN = _libItems.filter(x => x.status === 'done').length;
    const activeN = _libItems.filter(x => TASK_ACTIVE.includes(x.status)).length;
    d.meta.textContent = `已下载 ${doneN} 条` + (activeN ? ` · 进行中 ${activeN}` : '');
    loadStats();
    tasks.filter(t => TASK_ACTIVE.includes(t.status)).forEach(t => subscribeProgress(t.task_id));
  } catch (e) {
    d.list.innerHTML = `<div class="empty">${e.message}<br><button class="ghost sm" onclick="loadDownloads()">${icon('refresh')} 重试</button></div>`;
  }
}

/* 进行中任务数 → 侧边栏「下载记录」角标 */
function updateDownloadsBadge(tasks) {
  const active = tasks.filter(t => TASK_ACTIVE.includes(t.status)).length;
  const badge = document.getElementById('dl-badge');
  if (badge) {
    badge.style.display = active ? '' : 'none';
    badge.textContent = active > 99 ? '99+' : active;
  }
}

/* SSE 进度 → 就地更新任务卡,不打断浏览 */
function updateTaskCard(t) {
  const card = document.getElementById('vt-' + t.task_id);
  if (!card) { maybeReloadDownloads(); return; }
  const pct = Math.min(100, Math.max(0, t.progress || 0));
  const bar = card.querySelector('.dp-bar i');
  if (bar) bar.style.width = pct + '%';
  const set = (sel, val) => { const el = card.querySelector(sel); if (el && val != null) el.textContent = val; };
  set('.dp-pct', pct + '%');
  if (t.speed) set('.dp-spd', t.speed > 1024 ? (t.speed / 1024).toFixed(1) + ' MB/s' : t.speed.toFixed(0) + ' KB/s');
  if (t.eta) set('.dp-eta', '剩余 ' + t.eta + 's');
  const tag = card.querySelector('.status-tag');
  if (tag) { tag.className = 'status-tag ' + t.status; tag.textContent = TASK_STATUS_CN[t.status] || t.status; }
  if (['done', 'failed', 'cancelled', 'interrupted'].includes(t.status)) maybeReloadDownloads();
}

/* 勾选期间不整体重拉(列表会重排、勾选索引移位),清空勾选后再拉 */
let _dlDirty = false;
function maybeReloadDownloads() {
  if (document.querySelectorAll('.page.active input[data-libidx]:checked').length) {
    _dlDirty = true;
    return;
  }
  loadDownloads().then(() => {
    if (_libMode !== 'downloads' && LIB_PAGES.includes(_libMode)) renderPage();  // 刷其它页的「已下载」角标
  });
}

/* 30 秒兜底轮询(与 SSE 互为保险):角标 + 下载记录页静默刷新 */
async function pollTasks() {
  try {
    const tasks = await (await fetch(API('/api/tasks'))).json().then(r => r.tasks || []);
    _doneBvids = new Set(tasks.filter(t => t.status === 'done')
      .map(t => bvFromUrl(t.url)).filter(Boolean));
    updateDownloadsBadge(tasks);
    if (_libMode === 'downloads'
        && !document.querySelectorAll('.page.active input[data-libidx]:checked').length) {
      loadDownloads();
    }
  } catch (e) { /* ignore */ }
}

/* ---------- 关注列表(站点关注的全部 UP,可加入本地清单) ---------- */
let _followAll = [];      // {mid, uname, face}
let _followAllTotal = 0;
let _followAllShown = 0;  // 无限流:当前已显示的条数
let _localMids = new Set();

async function loadFollowAll() {
  await cachedLoad({
    page: 'feed',
    api: '/api/followings',
    cacheKey: 'followings',
    extract: (b) => b.items || [],
    stillValid: () => _libMode === 'feed' && _feedSource === 'all',
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
  const chunk = _libView === 'list' ? computePageSize() : computePageSize(240, 12, 76);
  if (!_followAllShown) _followAllShown = chunk;
  const shownItems = q ? filtered : filtered.slice(0, _followAllShown);
  if (shownItems.length === 0) {
    d.list.innerHTML = `<div class="empty">${q ? '没有匹配结果(试试其他关键词)' : '暂无关注的 UP'}</div>`;
  } else {
    d.list.className = 'lib-list';
    d.list.innerHTML = `<div class="${_libView === 'grid' ? 'up-grid' : 'up-list'}">` + shownItems.map(u => {
      const inLocal = _localMids.has(u.mid);
      const btn = inLocal
        ? '<span class="dl-badge">' + icon('check') + '已加清单</span>'
        : `<button class="ghost sm" onclick="upAddLocal(${u.mid}, this)">加入清单</button>`;
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

function maybeLoadMoreFollowAll() {
  if (_libMode !== 'feed' || _feedSource !== 'all' || getLibSearchQuery()) return;
  if (_followAllShown >= _followAll.length) return;
  _followAllShown += _libView === 'list' ? computePageSize() : computePageSize(240, 12, 76);
  renderFollowAll();
}

async function upAddLocal(mid, btnEl) {
  try {
    const r = await (await fetch(API('/api/up-list/add'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ input: String(mid) })
    })).json();
    if (!r.ok) throw new Error(r.error || '添加失败');
    _localMids.add(mid);
    if (btnEl) btnEl.outerHTML = '<span class="dl-badge">' + icon('check') + '已加清单</span>';
  } catch (e) { alert(e.message); }
}

/* ---------- 列表项渲染 ---------- */

/* 下载记录:文件名对齐提示(名称冲突 / 自动改名失败 / 同名同大小跳过) */
function taskWarnHtml(it) {
  if (it.rename_conflict) {
    return `<div class="dp-err">名称冲突:已有同名文件
      <button class="ghost sm" onclick="resolveTaskConflict('${it.task_id}','rename_with_index')">加 (1) 改名</button>
      <button class="ghost sm" onclick="resolveTaskConflict('${it.task_id}','open_existing')">打开现有文件</button>
      <button class="ghost sm" onclick="resolveTaskConflict('${it.task_id}','cancel')">忽略</button></div>`;
  }
  if (it.rename_error) {
    return `<div class="dp-err" title="${escapeAttr(it.rename_error)}">自动改名失败:${escapeHtml(String(it.rename_error).slice(0, 60))}</div>`;
  }
  if (it.rename_skipped) {
    return `<div class="dp-err">已有同名同大小的文件,未覆盖改名</div>`;
  }
  return '';
}

/* 处理名称冲突(加后缀改名 / 打开现有文件 / 忽略) */
async function resolveTaskConflict(taskId, action) {
  try {
    const r = await (await fetch(API('/api/task/' + taskId + '/resolve-conflict'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action })
    })).json();
    if (!r.ok) throw new Error(r.error || '处理失败');
    loadDownloads();
  } catch (e) { alert(e.message); }
}

/* 下载记录页的任务卡:进行中带进度条(供 SSE 就地更新),失败显示原因 */
function taskCardHtml(it, isGrid, checkHtml, checkedCls) {
  const active = TASK_ACTIVE.includes(it.status);
  const pct = Math.min(100, Math.max(0, it.progress || 0));
  const spd = it.speed > 1024 ? (it.speed / 1024).toFixed(1) + ' MB/s' : (it.speed || 0).toFixed(0) + ' KB/s';
  const bar = active
    ? `<div class="dp-bar"><i style="width:${pct}%"></i></div>` : '';
  const err = ['failed', 'interrupted'].includes(it.status) && it.error
    ? `<div class="dp-err" title="${escapeAttr(it.error)}">${escapeHtml(String(it.error).slice(0, 80))}</div>` : '';
  const live = it.status === 'downloading'
    ? `<span class="dp-pct">${pct}%</span><span class="dp-spd">${spd}</span>` +
      (it.eta ? `<span class="dp-eta">剩余 ${it.eta}s</span>` : '')
    : (it.status === 'merging' ? '<span class="dp-eta">正在合并音视频...</span>' : '');
  const metaInner = `<span class="status-tag ${it.status}">${TASK_STATUS_CN[it.status] || it.status}</span>${live}` +
    (it.label ? `<span class="q-label">[${escapeHtml(it.label)}]</span>` : '');
  const cancelBtn = active
    ? `<button class="danger sm" onclick="cancelTask('${it.task_id}')">取消</button>` : '';
  const titleHtml = it.url
    ? `<a href="${escapeAttr(it.url)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>`
    : escapeHtml(it.title);
  const thumb = `<div class="thumb-wrap"><img src="${escapeHtml(it.thumbnail)}" onerror="this.style.visibility='hidden'" loading="lazy"></div>`;
  if (!isGrid) {
    return `
    <div class="video-item lv${checkedCls}" id="vt-${it.task_id}">
      ${checkHtml}
      ${thumb}
      <div class="lv-body">
        <div class="vi-title">${titleHtml}</div>
        ${bar}
        <div class="dp-meta">${metaInner}${cancelBtn}</div>
        ${err}
        ${taskWarnHtml(it)}
      </div>
    </div>`;
  }
  return `
  <div class="video-item${checkedCls}" id="vt-${it.task_id}">
    ${checkHtml}
    ${thumb}
    <div class="vi-title">${titleHtml}</div>
    ${bar}
    <div class="dp-meta">${metaInner}${cancelBtn}</div>
    ${err}
    ${taskWarnHtml(it)}
  </div>`;
}
function renderLibList(pairs, container) {
  if (pairs.length === 0) {
    if (_libMode === 'downloads') {
      const q = getLibSearchQuery();
      container.innerHTML = `<div class="empty">${q ? '没有匹配结果(试试其他关键词)' : '还没有下载记录,点右上角「新建下载」开始'}</div>`;
      return;
    }
    const q = getLibSearchQuery();
    const isFiltering = q.length > 0;
    const retryMap = { history: 'loadHistory', feed: 'refreshLib', fav: 'favRefresh',
                       seasons: 'seasonRefresh', later: 'loadWatchlater' };
    const retryFn = retryMap[_libMode] || 'loadHistory';
    let hint;
    if (isFiltering) hint = '没有匹配结果(试试其他关键词)';
    else if (_libMode === 'fav') hint = '这里还没有收藏任何视频';
    else if (_libMode === 'seasons') hint = '这个合集还没有视频';
    else hint = '暂无内容(可能 B 站限流或账号无数据)';
    container.innerHTML = `<div class="empty">${hint}<br><button class="ghost sm" onclick="${retryFn}()">${icon('refresh')} 重新加载</button></div>`;
    return;
  }
  const isGrid = _libView === 'grid';
  container.className = 'lib-list' + (isGrid ? ' lib-grid' : '');
  container.innerHTML = pairs.map(({ it, idx: origIdx }) => {
    const checked = origIdx >= 0 && _libChecked.has(origIdx);
    const checkedAttr = checked ? 'checked' : '';
    const checkedCls = checked ? ' checked' : '';
    const checkHtml = `<input type="checkbox" data-libidx="${origIdx}" ${checkedAttr} onchange="toggleLibCheck(${origIdx}, this.checked)" class="vi-check">`;
    // 下载记录页:进行中/失败/取消的任务渲染成进度卡(队列已并入)
    if (_libMode === 'downloads' && it.status && it.status !== 'done') {
      return taskCardHtml(it, isGrid, checkHtml, checkedCls);
    }
    const badgeHtml = (_libMode !== 'downloads' && isUrlDownloaded(it.url))
      ? `<span class="dl-badge">${icon('check')}已下载</span>` : '';
    const pct = it.watched_pct || 0;
    const showProgress = it.duration && pct > 0 && pct < 100;
    const watchbar = showProgress
      ? `<div class="vi-watchbar"><i style="width:${Math.min(pct, 100)}%"></i></div>`
      : '';
    const upName = escapeHtml(it.uploader || '-');
    const metaParts = [it.uploader_mid
      ? `<a href="https://space.bilibili.com/${it.uploader_mid}" target="_blank" rel="noopener">${upName}</a>`
      : upName];
    if (it.view_at) metaParts.push(formatTime(it.view_at));
    const titleHtml = it.url
      ? `<a href="${escapeAttr(it.url)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>`
      : escapeHtml(it.title);
    const durHtml = it.duration ? `<span class="thumb-dur">${formatDuration(it.duration)}</span>` : '';
    // 下载记录:封面右下角播放按钮 → 在 Finder 中定位该视频下载到的文件
    const playHtml = (_libMode === 'downloads' && it.task_id)
      ? `<button class="vi-play" title="在 Finder 中显示该视频" onclick="event.stopPropagation();revealTask('${it.task_id}')">${icon('play')}</button>` : '';
    const thumbCls = playHtml ? 'thumb-wrap has-play' : 'thumb-wrap';
    if (!isGrid) {
      return `
      <div class="video-item lv${checkedCls}">
        ${checkHtml}
        <div class="${thumbCls}">
          <img src="${escapeHtml(it.thumbnail)}" onerror="this.style.visibility='hidden'" loading="lazy">
          ${durHtml}${playHtml}
        </div>
        <div class="lv-body">
          <div class="vi-title">${titleHtml} ${badgeHtml}</div>
          <div class="vi-meta">${metaParts.join(' · ')}</div>
          ${watchbar}
          ${taskWarnHtml(it)}
        </div>
      </div>`;
    }
    return `
    <div class="video-item${checkedCls}">
      ${checkHtml}
      <div class="${thumbCls}">
        <img src="${escapeHtml(it.thumbnail)}" onerror="this.style.visibility='hidden'" loading="lazy">
        ${durHtml}${playHtml}
      </div>
      <div class="vi-title">${titleHtml} ${badgeHtml}</div>
      <div class="vi-meta">${metaParts.join(' · ')}</div>
      ${watchbar}
      ${taskWarnHtml(it)}
    </div>`;
  }).join('');
  updateLibCount();
}

/* ---------- 列表/网格视图切换 ---------- */
let _libView = localStorage.getItem('bd_lib_view') || 'grid';

function updateLibViewBtn() {
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.innerHTML = _libView === 'grid' ? icon('list') : icon('grid');
    btn.title = _libView === 'grid' ? '切换为列表视图' : '切换为网格视图';
  });
}

function toggleLibView() {
  _libView = _libView === 'list' ? 'grid' : 'list';
  localStorage.setItem('bd_lib_view', _libView);
  updateLibViewBtn();
  _shown = 0;            // 换布局重算首屏
  _followAllShown = 0;
  applyLibFilter();
}

/* ---------- 搜索过滤 ---------- */
function getLibSearchQuery() {
  const pg = document.querySelector('.page.active');
  const el = pg && pg.querySelector('.lib-search');
  return el ? el.value.trim().toLowerCase() : '';
}

function applyLibFilter() {
  renderPage();
}

/* ---------- 勾选与批量下载(所有列表页统一的底部操作条) ---------- */
const _libChecked = new Set();

function toggleLibCheck(origIdx, checked) {
  if (checked) _libChecked.add(origIdx); else _libChecked.delete(origIdx);
  const cb = document.querySelector(`.page.active input[data-libidx="${origIdx}"]`);
  const item = cb ? cb.closest('.video-item') : null;
  if (item) item.classList.toggle('checked', checked);
  updateLibCount();
}

/* 点击封面图切换勾选(勾选框太小不好点)。播放按钮/链接内部已 stopPropagation,不会误触发。 */
document.addEventListener('click', (e) => {
  const thumb = e.target.closest('.video-item .thumb-wrap');
  if (!thumb) return;
  const cb = thumb.closest('.video-item').querySelector('input.vi-check');
  if (!cb) return;
  cb.checked = !cb.checked;  // 先更新 DOM,让 计数/操作条 看到新状态
  toggleLibCheck(parseInt(cb.dataset.libidx), cb.checked);
});

function libToggleAll(checked) {
  document.querySelectorAll('.page.active .video-item').forEach(item => {
    const cb = item.querySelector('input.vi-check');
    if (!cb) return;
    const idx = parseInt(cb.dataset.libidx);
    cb.checked = checked;
    item.classList.toggle('checked', checked);
    if (checked) _libChecked.add(idx); else _libChecked.delete(idx);
  });
  updateLibCount();
}

function updateLibCount() {
  const n = document.querySelectorAll('.page.active input[data-libidx]:checked').length;
  document.querySelectorAll('.lib-count').forEach(el => el.textContent = n);
  // 下载页勾选期间被打断的列表刷新,清空勾选后补上
  if (_libMode === 'downloads' && n === 0 && _dlDirty) { _dlDirty = false; loadDownloads(); }
  updateActionBar();
}

function updateActionBar() {
  const bar = document.getElementById('lib-action-bar');
  const onLibPage = LIB_PAGES.includes(_libMode)
    && !isUpCardsView()
    && !(_libMode === 'fav' && !_favCurrent)
    && !(_libMode === 'seasons' && !_seasonsCurrent)
    && document.querySelector(`.page.active[data-page="${_libMode}"]`);
  const n = document.querySelectorAll('.page.active input[data-libidx]:checked').length;
  bar.classList.toggle('show', !!onLibPage && n > 0);
  // 按页面切换动作组:下载记录页勾选的是任务/记录 → 删除;其它页 → 批量下载
  const isDl = _libMode === 'downloads';
  const show = (id, on) => { const el = document.getElementById(id); if (el) el.style.display = on ? '' : 'none'; };
  show('ab-quality', !isDl);
  show('ab-download-btn', !isDl);
  show('ab-del-rec', isDl);
  show('ab-del-file', isDl);
}

async function libDownload() {
  const checked = [...document.querySelectorAll('.page.active input[data-libidx]:checked')];
  if (checked.length === 0) { alert('请至少勾选一项'); return; }
  const qSel = document.querySelector('.lib-quality');
  const label = qSel ? qSel.value : '';
  let items = checked.map(cb => {
    const it = _libItems[parseInt(cb.dataset.libidx)];
    return { url: it.url, title: it.title, page: 1,
             thumbnail: it.thumbnail || '', uploader: it.uploader || '',
             uploader_mid: it.uploader_mid || 0, duration: it.duration || 0 };
  });
  const skipped = items.filter(it => isUrlDownloaded(it.url)).length;
  items = items.filter(it => !isUrlDownloaded(it.url));
  if (items.length === 0) { alert('所选项均已下载过'); return; }
  try {
    const r = await (await fetch(API('/api/batch/download'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ items, label })
    })).json();
    if (!r.ok) throw new Error(r.error || '启动失败');
    alert(`已加入 ${r.count} 个下载任务${skipped ? `(跳过 ${skipped} 个已下载)` : ''}`);
    r.task_ids.forEach(subscribeProgress);
    goPage('downloads');
  } catch (e) { alert(e.message); }
}

function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 3600) return Math.floor(diff/60) + '分钟前';
  if (diff < 86400) return Math.floor(diff/3600) + '小时前';
  if (diff < 604800) return Math.floor(diff/86400) + '天前';
  return (d.getMonth()+1) + '/' + d.getDate();
}

function subscribeProgress(taskId, retries) {
  if (_activeSSE[taskId]) return;
  retries = retries || 0;
  const es = new EventSource(API('/api/progress/' + taskId));
  _activeSSE[taskId] = es;
  es.onmessage = (ev) => {
    retries = 0;
    const t = JSON.parse(ev.data);
    updateTaskCard(t);
    if (['done', 'failed', 'cancelled', 'interrupted'].includes(t.status)) {
      es.close(); delete _activeSSE[taskId];
    }
  };
  es.onerror = () => {
    es.close(); delete _activeSSE[taskId];
    if (retries < 3) {
      setTimeout(() => subscribeProgress(taskId, retries + 1), 2000 * (retries + 1));
    }
  };
}

async function cancelTask(taskId) {
  try {
    await fetch(API('/api/cancel/' + taskId), { method: 'POST' });
  } catch (e) { /* SSE 会更新状态 */ }
}

/* ---------- 记录删除(下载记录页勾选后的底部操作条) ---------- */
/* 勾选后直接按按钮语义删除,不再弹窗二次选择 */
async function delSelected(deleteFile) {
  const checked = [...document.querySelectorAll('.page.active input[data-libidx]:checked')];
  const ids = checked.map(cb => (_libItems[parseInt(cb.dataset.libidx)] || {}).task_id).filter(Boolean);
  if (ids.length === 0) { alert('请先勾选要删除的任务'); return; }
  try {
    const r = await (await fetch(API('/api/task/delete'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ids, delete_file: deleteFile })
    })).json();
    if (!r.ok) throw new Error(r.error || '删除失败');
    _libChecked.clear();
    await loadDownloads();
  } catch (e) { alert(e.message); }
}

/* 清除所有已完成/失败记录(只删记录,不动文件) */
async function clearAllRecords() {
  try {
    const r = await (await fetch(API('/api/tasks/clear'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ delete_file: false })
    })).json();
    if (!r.ok) throw new Error(r.error || '清除失败');
    if (!r.count) { alert('没有可清除的记录'); return; }
    alert(`已清除 ${r.count} 条记录`);
    _libChecked.clear();
    await loadDownloads();
  } catch (e) { alert(e.message); }
}

/* 清除失败/失效记录:失败任务 + 文件已不在本地的完成任务(只删记录) */
async function clearInvalidRecords() {
  try {
    const r = await (await fetch(API('/api/tasks/clean-invalid'), { method: 'POST' })).json();
    if (!r.ok) throw new Error(r.error || '清理失败');
    if (!r.removed) { alert('没有失败/失效的记录'); return; }
    alert(`已删除 ${r.removed} 条记录\n(下载失败 ${r.failed} 条,文件已失效 ${r.invalid} 条)`);
    _libChecked.clear();
    await loadDownloads();
  } catch (e) { alert(e.message); }
}

async function revealTask(taskId) {
  if (!taskId) return;
  try {
    const r = await (await fetch(API('/api/task/' + taskId + '/reveal'), { method: 'POST' })).json();
    if (!r.ok) alert('打开失败: ' + (r.error || ''));
    else if (!r.found) alert('没找到该视频的文件(可能已被删除或移动),已打开下载目录。');
  } catch (e) { alert('打开失败: ' + e.message); }
}

/* ---------- 工具 ---------- */
function formatDuration(sec) {
  sec = Math.floor(sec || 0);
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m >= 60) { const h = Math.floor(m/60); return h + ':' + String(m%60).padStart(2,'0') + ':' + String(s).padStart(2,'0'); }
  return m + ':' + String(s).padStart(2, '0');
}
function escapeHtml(s) {
  return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

/* ---------- 主题 ---------- */
function applyTheme(theme) {
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

function toggleTheme() {
  const cur = localStorage.getItem('bd_theme') || 'auto';
  const next = cur === 'light' ? 'dark' : cur === 'dark' ? 'auto' : 'light';
  localStorage.setItem('bd_theme', next);
  applyTheme(next);
}

function loadConfigIntoUI() {
  fetch(API('/api/config')).then(r => r.json()).then(r => {
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

/* ---------- 设置:改动即生效,无保存按钮 ---------- */
function flashSaved() {
  const el = document.getElementById('save-flash');
  if (!el) return;
  el.classList.add('show');
  clearTimeout(flashSaved._t);
  flashSaved._t = setTimeout(() => el.classList.remove('show'), 1400);
}

async function saveConfigField(patch) {
  try {
    const r = await (await fetch(API('/api/config'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(patch)
    })).json();
    if (!r.ok) throw new Error(r.error || '保存失败');
    if ('download_dir' in patch) DL_DIR = r.download_dir;
    flashSaved();
    return r;
  } catch (e) { alert('保存失败: ' + e.message); }
}

function saveChk(name, el) {
  const patch = {};
  patch[name] = el.checked;
  saveConfigField(patch);
}

function saveDir() {
  const v = document.getElementById('config-dir-input').value.trim();
  if (v) saveConfigField({ download_dir: v });
}

function saveTheme(v) {
  localStorage.setItem('bd_theme', v);
  applyTheme(v);
  saveConfigField({ theme: v });
}

function resetConfigDir() {
  document.getElementById('config-dir-input').value = '~/Downloads/bilibili';
  saveConfigField({ download_dir: '~/Downloads/bilibili' });
}

async function loadConfig() {
  try {
    const r = await (await fetch(API('/api/config'))).json();
    if (r.ok) DL_DIR = r.download_dir;
  } catch (e) {}
}

/* ---------- 统计 ---------- */
function formatBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + units[i];
}

async function loadStats() {
  try {
    const r = await (await fetch(API('/api/stats'))).json();
    if (!r.ok) return;
    const el = document.getElementById('dl-stats-line');
    if (el) el.textContent = `总下载 ${r.total_count} 条 · ${formatBytes(r.total_bytes)} · 本月 ${r.month_count} · 今日 ${r.day_count}`;
  } catch (e) {}
}

/* ---------- 已下载判定(列表角标 + 批量跳过) ---------- */
let _doneBvids = new Set();

function bvFromUrl(url) {
  const m = /BV[0-9A-Za-z]{10}/.exec(url || '');
  return m ? m[0] : null;
}

function isUrlDownloaded(url) {
  const bv = bvFromUrl(url);
  return !!bv && _doneBvids.has(bv);
}

(async function init() {
  await loadConfig();
  const stored = localStorage.getItem('bd_theme');
  const cfg = await fetch(API('/api/config')).then(r => r.json()).catch(() => null);
  const theme = stored || (cfg && cfg.theme) || 'auto';
  applyTheme(theme);
  updateLibViewBtn();
  syncFeedSeg();
  await refreshLogin();
  goPage('downloads');
  // 窗口宽度变化 → 每页条数自适应重算
  let _resizeT = null;
  window.addEventListener('resize', () => {
    clearTimeout(_resizeT);
    _resizeT = setTimeout(() => { if (LIB_PAGES.includes(_libMode)) renderPage(); }, 200);
  });
  // 内容区独立滚动:滚过 500 显示回顶按钮;接近底部触发无限流追加
  const _scroller = document.querySelector('.content');
  _scroller.addEventListener('scroll', () => {
    document.getElementById('back-top').classList.toggle('show', _scroller.scrollTop > 500);
    if (_scroller.scrollTop + _scroller.clientHeight < _scroller.scrollHeight - 600) return;
    maybeLoadMore();
  }, { passive: true });
  // 后台自动下载入队靠轮询刷新角标,与 SSE 互为兜底
  setInterval(() => { pollTasks(); }, 30000);
})();
