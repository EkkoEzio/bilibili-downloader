/* 新建下载抽屉:单视频/批量自动判断、多 P 选择清晰度、批量勾选下载。 */
import { postJSON } from './net.js';
import { escapeHtml, formatDuration, pageFromUrl } from './util.js';
import { goPage, isUrlDownloaded } from './lib.js';
import { closeDrawer } from './drawer.js';
import { subscribeProgress } from './pages/downloads.js';
import { QUALITY_OPTIONS } from './quality.js';

let _singleUrl = '';
let _singleInfo = null;  // 最近一次解析结果(带 uploader_mid,下载时回传)
let _batchItems = [];

export async function smartParse() {
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
    const r = await postJSON('/api/parse', { url });
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
    // 分 P 下拉的 URL 通过 data-url 传递 + 事件监听取值,
    // 不把用户可控的 URL 拼进内联 JS(属性值经 HTML 解码后可逸出 JS 字符串)
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
            <select id="part-select"></select>
          </div>
          <div id="part-formats" style="margin-top:10px;"><div class="hint"><span class="spinner"></span>加载清晰度...</div></div>
        </div>
      </div>`;
    const sel = document.getElementById('part-select');
    sel.innerHTML = partOptions;
    sel.dataset.url = url;
    sel.addEventListener('change', loadPartFormats);
    loadPartFormats();
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

async function loadPartFormats() {
  const sel = document.getElementById('part-select');
  if (!sel) return;
  const url = sel.dataset.url || _singleUrl;
  const page = sel.value;
  const fbox = document.getElementById('part-formats');
  fbox.innerHTML = '<div class="hint"><span class="spinner"></span>加载清晰度...</div>';
  try {
    const r = await postJSON('/api/parse-part', { url, page: parseInt(page) });
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
export async function startDownload() {
  const url = _singleUrl;
  const label = document.getElementById('quality-select')?.value || '';
  const partSel = document.getElementById('part-select');
  const page = partSel ? parseInt(partSel.value) : 1;
  if (!url || !label) return;
  try {
    const r = await postJSON('/api/download', {
      url, label, page,
      uploader_mid: (_singleInfo && _singleInfo.uploader_mid) || 0
    });
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
async function batchParse(lines) {
  const box = document.getElementById('batch-result');
  const btn = document.getElementById('parse-btn');
  btn.disabled = true;
  document.getElementById('parse-result').innerHTML = '';
  box.innerHTML = '<div class="empty"><span class="spinner"></span>解析中...(收藏夹/合集可能需要一点时间)</div>';
  const isSingleList = lines.length === 1;
  try {
    const r = isSingleList
      ? await postJSON('/api/batch/parse', { url: lines[0] })
      : await postJSON('/api/batch/parse', { urls: lines });
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
      <select id="batch-quality">${QUALITY_OPTIONS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
      <button class="primary" onclick="batchDownload()">批量下载</button>
    </div>
    <div class="hint">批量下载按设置的并发数同时进行,其余排队;已下载过的项会自动跳过。</div>`;
  document.querySelectorAll('input[data-bidx]').forEach(cb => cb.addEventListener('change', updateBatchCount));
  updateBatchCount();
}

export function batchToggleAll(checked) {
  document.querySelectorAll('input[data-bidx]').forEach(cb => cb.checked = checked);
  updateBatchCount();
}

function updateBatchCount() {
  const n = document.querySelectorAll('input[data-bidx]:checked').length;
  const el = document.getElementById('batch-count');
  if (el) el.textContent = n;
}

export async function batchDownload() {
  const checked = [...document.querySelectorAll('input[data-bidx]:checked')];
  if (checked.length === 0) { alert('请至少勾选一项'); return; }
  const label = document.getElementById('batch-quality').value;
  let items = checked.map(cb => {
    const idx = parseInt(cb.dataset.bidx);
    const it = _batchItems[idx];
    /* 分 P 走 util.pageFromUrl(键规则的唯一来源),不在这里手写第二种解析:
       /api/batch/parse 的条目只有 URL(没有 page 字段),查询侧只能从 URL 解析。
       曾经的手写版把分 P 当「问号紧跟 p=」的子串来匹配,与唯一来源的
       [?&]p= 正则结论不同,且两种都不报错:
         - p 不在首位时(如 ...?spm_id_from=333.999&p=2)子串匹配判假 → 算成 P1,
           下错集数。resolve_playlist 的单视频分支把用户粘贴的原始链接原样透传,
           而 B 站地址栏/分享链接普遍带 spm_id_from、vd_source,所以这是现实形态;
         - 分 P 值非法时 parseInt 得 NaN → 提交的 page 序列化成 null。
       注:守卫测试 test_frontend_page_parsing_only_in_util 按整行扫字面量,
       连注释也不放过,所以这里只描述、不复刻旧写法。 */
    return { url: it.url, title: it.title, page: pageFromUrl(it.url),
             thumbnail: it.thumbnail || '', uploader: it.uploader || '',
             uploader_mid: it.uploader_mid || 0, duration: it.duration || 0 };
  });
  const skipped = items.filter(it => isUrlDownloaded(it.url)).length;
  items = items.filter(it => !isUrlDownloaded(it.url));
  if (items.length === 0) { alert('所选项均已下载过'); return; }
  try {
    const r = await postJSON('/api/batch/download', { items, label });
    if (!r.ok) throw new Error(r.error || '启动失败');
    alert(`已加入 ${r.count} 个下载任务${skipped ? `(跳过 ${skipped} 个已下载)` : ''}`);
    r.task_ids.forEach(subscribeProgress);
  } catch (e) { alert(e.message); }
}

