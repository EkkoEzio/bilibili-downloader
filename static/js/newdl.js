/* 新建下载抽屉：单视频/批量自动判断、多 P 选择清晰度、批量勾选下载。 */
import { postJSON, isAbortError } from './net.js';
import { escapeHtml, escapeAttr, formatDuration, pageFromUrl,
         safeHttpUrl, nonNegativeInt } from './util.js';
import { goPage, isUrlDownloaded, itemBusinessId } from './lib.js';
import { closeDrawer } from './drawer.js';
import { announce, setBusy, withPending } from './confirm.js';
import { subscribeProgress } from './pages/downloads.js?v=20261110g';
import { QUALITY_OPTIONS } from './quality.js';

let _singleUrl = '';
let _singleInfo = null;
let _batchItems = [];
let _batchItemById = new Map();
let _parseGeneration = 0;
let _parseController = null;
let _partGeneration = 0;
let _partController = null;

function cancelPartRequest() {
  _partGeneration++;
  if (_partController) _partController.abort();
  _partController = null;
}

function beginParseRequest() {
  _parseGeneration++;
  if (_parseController) _parseController.abort();
  cancelPartRequest();
  const controller = new AbortController();
  _parseController = controller;
  return { generation: _parseGeneration, controller };
}

function parseRequestIsCurrent(generation, controller) {
  return generation === _parseGeneration
    && controller === _parseController
    && !controller.signal.aborted;
}

function beginPartRequest() {
  cancelPartRequest();
  const controller = new AbortController();
  _partController = controller;
  return { generation: _partGeneration, controller };
}

function partRequestIsCurrent(generation, controller, select, url, page, box) {
  return generation === _partGeneration
    && controller === _partController
    && !controller.signal.aborted
    && document.getElementById('part-select') === select
    && document.getElementById('part-formats') === box
    && (select.dataset.url || _singleUrl) === url
    && select.value === page;
}

function replaceBatchItems(items) {
  const nextItems = [];
  const nextMap = new Map();
  const positions = new Map();
  for (const item of Array.isArray(items) ? items : []) {
    const id = itemBusinessId(item);
    if (positions.has(id)) nextItems[positions.get(id)] = item;
    else { positions.set(id, nextItems.length); nextItems.push(item); }
    nextMap.set(id, item);
  }
  _batchItems = nextItems;
  _batchItemById = nextMap;
}

export async function smartParse() {
  const text = document.getElementById('dl-input').value.trim();
  if (!text) {
    announce('请先粘贴 B 站链接', true);
    alert('请粘贴链接');
    return;
  }
  const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
  const isList = lines.length > 1 || /favlist|medialist|watchlater|space\.bilibili/i.test(lines[0]);
  if (isList) await batchParse(lines);
  else await parseVideo(lines[0]);
}

async function parseVideo(url) {
  const { generation, controller } = beginParseRequest();
  const btn = document.getElementById('parse-btn');
  const box = document.getElementById('parse-result');
  setBusy(btn, true, '解析中…');
  box.innerHTML = '<div class="empty"><span class="spinner"></span>解析中...</div>';
  document.getElementById('batch-result').innerHTML = '';
  _singleUrl = '';
  _singleInfo = null;
  replaceBatchItems([]);
  try {
    const r = await postJSON('/api/parse', { url }, { signal: controller.signal });
    if (!parseRequestIsCurrent(generation, controller)) return;
    if (!r.ok) throw new Error(r.error || '解析失败');
    _singleUrl = url;
    _singleInfo = r.info;
    renderParseResult(r.info, url, r.logged_in);
  } catch (error) {
    if (isAbortError(error) || !parseRequestIsCurrent(generation, controller)) return;
    const message = error.message || '请求失败';
    box.innerHTML = '<div class="empty">' + escapeHtml(message) + '</div>';
    announce(message, true);
  } finally {
    if (parseRequestIsCurrent(generation, controller)) setBusy(btn, false);
  }
}

function renderParseResult(info, url, loggedIn) {
  const box = document.getElementById('parse-result');
  let warnHtml = '';
  if (!loggedIn) {
    warnHtml = '<div class="warn">未登录，可能只能下载公开画质。如需 1080P+/4K 大会员画质，请先登录。</div>';
  }
  if (info.is_multi_part && info.parts && info.parts.length > 0) {
    const partOptions = info.parts.map(p => {
      const page = nonNegativeInt(p.page) || 1;
      return `<option value="${page}">P${page} · ${escapeHtml(p.title)} (${formatDuration(p.duration)})</option>`;
    }).join('');
    box.innerHTML = `
      ${warnHtml}
      <div class="video-card">
        <div class="video-info">
          <div class="vt">${escapeHtml(info.title)} <span style="color:var(--accent);font-size:12.5px;">（共 ${info.parts.length} P）</span></div>
          <div class="vm">UP主：${escapeHtml(info.uploader || '-')}</div>
          <div class="btn-row"><select id="part-select" aria-label="选择分 P"></select></div>
          <div id="part-formats" class="mt-10"><div class="hint"><span class="spinner"></span>加载清晰度...</div></div>
        </div>
      </div>`;
    const sel = document.getElementById('part-select');
    sel.innerHTML = partOptions;
    sel.dataset.url = url;
    sel.addEventListener('change', loadPartFormats);
    loadPartFormats();
    return;
  }

  cancelPartRequest();
  const thumbnail = safeHttpUrl(info.thumbnail, { httpsOnly: true });
  box.innerHTML = `
    ${warnHtml}
    <div class="video-card">
      <img src="${escapeAttr(thumbnail)}" alt="" onerror="this.style.visibility='hidden'">
      <div class="video-info">
        <div class="vt">${escapeHtml(info.title)}</div>
        <div class="vm">UP主：${escapeHtml(info.uploader || '-')} ${info.duration ? '· 时长：' + formatDuration(info.duration) : ''}</div>
        ${qualitySelectHtml(info.formats, info.best_label)}
        <div class="btn-row mt-10">
          <button type="button" class="primary" id="single-download-btn" onclick="startDownload(this)">开始下载</button>
        </div>
      </div>
    </div>`;
}

async function loadPartFormats() {
  const select = document.getElementById('part-select');
  if (!select) return;
  const url = select.dataset.url || _singleUrl;
  const page = select.value;
  const box = document.getElementById('part-formats');
  const { generation, controller } = beginPartRequest();
  box.innerHTML = '<div class="hint"><span class="spinner"></span>加载清晰度...</div>';
  try {
    const r = await postJSON('/api/parse-part', { url, page: parseInt(page) }, {
      signal: controller.signal,
    });
    if (!partRequestIsCurrent(generation, controller, select, url, page, box)) return;
    if (!r.ok) throw new Error(r.error || '加载失败');
    _singleInfo = r.info;
    box.innerHTML = `
      ${qualitySelectHtml(r.info.formats, r.info.best_label)}
      <div class="btn-row mt-10"><button type="button" class="primary" id="single-download-btn" onclick="startDownload(this)">开始下载</button></div>`;
  } catch (error) {
    if (isAbortError(error) || !partRequestIsCurrent(generation, controller, select, url, page, box)) return;
    const message = error.message || '请求失败';
    box.innerHTML = '<div class="hint">' + escapeHtml(message) + '</div>';
    announce(message, true);
  }
}

function qualitySelectHtml(formats, bestLabel) {
  const list = Array.isArray(formats) ? formats : [];
  const videoOnly = list.filter(f => f.kind === 'video' || f.kind === 'av');
  let options = videoOnly.map(f => {
    const label = String(f.label ?? '');
    const selected = f.label === bestLabel ? 'selected' : '';
    const size = Number(f.size_mb);
    const sizeTxt = Number.isFinite(size) && size > 0 ? ` (${size} MB)` : '';
    return `<option value="${escapeAttr(label)}" ${selected}>${escapeHtml(label)}${sizeTxt}</option>`;
  }).join('');
  const audio = list.find(f => f.kind === 'audio');
  if (audio) {
    const size = Number(audio.size_mb);
    const sizeTxt = Number.isFinite(size) && size > 0 ? ` (${size} MB)` : '';
    options += `<option value="音频">仅音频${sizeTxt}</option>`;
  }
  if (!options) return '<div class="hint">无可用清晰度</div>';
  return `<div class="btn-row mt-8"><select id="quality-select" aria-label="下载清晰度">${options}</select></div>`;
}

export async function startDownload(button = null) {
  const url = _singleUrl;
  const label = document.getElementById('quality-select')?.value || '';
  const partSel = document.getElementById('part-select');
  const page = partSel ? parseInt(partSel.value) : 1;
  if (!url || !label) return;
  const submitButton = button || document.getElementById('single-download-btn');
  try {
    await withPending(submitButton, '提交中…', async () => {
      const r = await postJSON('/api/download', {
        url, label, page,
        uploader_mid: (_singleInfo && _singleInfo.uploader_mid) || 0,
      });
      if (!r.ok) {
        const message = r.task_id
          ? `${r.error || '启动下载失败'}，请在「下载记录」查看该任务`
          : (r.error || '启动下载失败');
        throw new Error(message);
      }
      subscribeProgress(r.task_id);
      closeDrawer();
      goPage('downloads');
    });
    announce('下载任务已加入队列');
  } catch (error) {
    let message = error.message || '启动下载失败';
    // 重复任务走 HTTP 409,net.js 抛 HttpError,payload 里带 task_id:
    // 这里补上指向下载记录的引导(200+ok:false 形态已不存在,别只看 r.task_id)。
    const dupTaskId = error && error.payload && error.payload.task_id;
    if (dupTaskId && error.code === 'duplicate_task') {
      message += '，请在「下载记录」查看该任务';
    }
    announce(message, true);
    alert(message);
  }
}

async function batchParse(lines) {
  const { generation, controller } = beginParseRequest();
  const box = document.getElementById('batch-result');
  const btn = document.getElementById('parse-btn');
  setBusy(btn, true, '解析中…');
  document.getElementById('parse-result').innerHTML = '';
  box.innerHTML = '<div class="empty"><span class="spinner"></span>解析中...（收藏夹/合集可能需要一点时间）</div>';
  _singleUrl = '';
  _singleInfo = null;
  replaceBatchItems([]);
  const isSingleList = lines.length === 1;
  try {
    const r = isSingleList
      ? await postJSON('/api/batch/parse', { url: lines[0] }, { signal: controller.signal })
      : await postJSON('/api/batch/parse', { urls: lines }, { signal: controller.signal });
    if (!parseRequestIsCurrent(generation, controller)) return;
    if (!r.ok) throw new Error(r.error || '解析失败');
    replaceBatchItems(r.items || []);
    renderBatchList(r.title || (isSingleList ? '收藏夹/合集' : '多链接'));
  } catch (error) {
    if (isAbortError(error) || !parseRequestIsCurrent(generation, controller)) return;
    const message = error.message || '请求失败';
    box.innerHTML = '<div class="empty">' + escapeHtml(message) + '</div>';
    announce(message, true);
  } finally {
    if (parseRequestIsCurrent(generation, controller)) setBusy(btn, false);
  }
}

function renderBatchList(title) {
  const box = document.getElementById('batch-result');
  if (_batchItems.length === 0) {
    box.innerHTML = '<div class="empty">未解析出任何视频</div>';
    return;
  }
  const itemRows = _batchItems.map((it) => {
    const id = itemBusinessId(it);
    const itemTitle = it.title || '未命名视频';
    return `
    <label class="batch-check-row">
      <input type="checkbox" data-bid="${escapeAttr(id)}" aria-label="选择 ${escapeAttr(itemTitle)}" checked>
      <span class="bc-title">${escapeHtml(itemTitle)}</span>
      ${it.duration ? `<span class="bc-meta">${formatDuration(it.duration)}</span>` : ''}
      ${it.error ? `<span class="bc-meta" style="color:var(--red);">${escapeHtml(it.error)}</span>` : ''}
    </label>`;
  }).join('');
  box.innerHTML = `
    <div class="batch-head">「${escapeHtml(title)}」共 ${_batchItems.length} 项</div>
    <div class="toolbar tight">
      <button type="button" class="quiet" onclick="batchToggleAll(true)">全选</button>
      <button type="button" class="quiet" onclick="batchToggleAll(false)">全不选</button>
      <span class="tb-note">已选 <b id="batch-count">0</b> 项</span>
    </div>
    <div class="batch-scroll">
      ${itemRows}
    </div>
    <div class="btn-row mt-10">
      <select id="batch-quality" aria-label="批量下载清晰度">${QUALITY_OPTIONS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
      <button type="button" class="primary" id="batch-download-btn" onclick="batchDownload(this)">批量下载</button>
    </div>
    <div class="hint">批量下载按设置的并发数同时进行，其余排队；已下载过的项会自动跳过。</div>`;
  document.querySelectorAll('input[data-bid]').forEach(cb => cb.addEventListener('change', updateBatchCount));
  updateBatchCount();
}

export function batchToggleAll(checked) {
  document.querySelectorAll('input[data-bid]').forEach(cb => { cb.checked = checked; });
  updateBatchCount();
}

function updateBatchCount() {
  const count = document.querySelectorAll('input[data-bid]:checked').length;
  const el = document.getElementById('batch-count');
  if (el) el.textContent = count;
}

export async function batchDownload(button = null) {
  const checked = [...document.querySelectorAll('input[data-bid]:checked')];
  if (checked.length === 0) {
    announce('请至少勾选一项', true);
    alert('请至少勾选一项');
    return;
  }
  const label = document.getElementById('batch-quality')?.value || '';
  let items = checked.map((cb) => {
    const it = _batchItemById.get(String(cb.dataset.bid || ''));
    if (!it) return null;
    return { url: it.url, title: it.title, page: pageFromUrl(it.url),
             thumbnail: it.thumbnail || '', uploader: it.uploader || '',
             uploader_mid: it.uploader_mid || 0, duration: it.duration || 0 };
  }).filter(Boolean);
  const skipped = items.filter(it => isUrlDownloaded(it.url)).length;
  items = items.filter(it => !isUrlDownloaded(it.url));
  if (items.length === 0) {
    announce('所选项均已下载过');
    alert('所选项均已下载过');
    return;
  }
  const submitButton = button || document.getElementById('batch-download-btn');
  try {
    const r = await withPending(submitButton, '提交中…', async () => {
      const result = await postJSON('/api/batch/download', { items, label });
      if (!result.ok) throw new Error(result.error || '启动失败');
      return result;
    });
    const message = `已加入 ${r.count} 个下载任务${skipped ? `（跳过 ${skipped} 个已下载）` : ''}`;
    announce(message);
    alert(message);
    r.task_ids.forEach(subscribeProgress);
  } catch (error) {
    const message = error.message || '启动失败';
    announce(message, true);
    alert(message);
  }
}

/* 抽屉关闭时立即释放解析请求；代际仍会阻止已进入微任务队列的旧响应提交。 */
document.addEventListener('drawer:closed', () => {
  _parseGeneration++;
  if (_parseController) _parseController.abort();
  _parseController = null;
  cancelPartRequest();
});
