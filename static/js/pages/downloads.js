/* 下载记录页：稳定任务 ID 状态 + 单一聚合 SSE + 断线兜底轮询。 */
import { registerPage, state, libDom, renderPage, setItemExtras, LIB_PAGES,
         goPage, replaceItems, selectedVisibleItems } from '../lib.js';
import { getJSON, postJSON, postEmpty, isAbortError } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, formatBytes, bvFromUrl, dlKey, pageFromUrl,
         safeHttpUrl, safeDecimalId } from '../util.js';
import { announce, confirmAction, withPending } from '../confirm.js';

const TASK_ACTIVE = ['queued', 'downloading', 'merging', 'cancelling', 'retrying'];
const TASK_STATUS_CN = {
  queued: '排队中', downloading: '下载中', merging: '合并中',
  cancelling: '取消中', retrying: '重试中', failed: '失败',
  cancelled: '已取消', interrupted: '已中断',
};
const TASK_TERMINAL = ['done', 'failed', 'cancelled', 'interrupted'];
const TASK_KNOWN = new Set([...TASK_ACTIVE, ...TASK_TERMINAL]);
const TASK_STREAM_STALE_MS = 25000;
const TASK_POLL_INTERVAL_MS = 30000;

function normalizedStatus(value) {
  const status = String(value ?? '');
  return TASK_KNOWN.has(status) ? status : 'unknown';
}

function inlineStringArg(value) {
  return escapeAttr(JSON.stringify(String(value ?? '')));
}

let _doneKeys = new Set();
/* 进行中的任务键:让卡片在下载过程中就能显示「下载中」,而不是等下载完才有标记。 */
let _activeKeys = new Set();
let _loadGeneration = 0;
let _loadController = null;
let _dirty = false;

/* 聚合事件流状态：任务 Map 是任务快照的唯一真相；共享 lib.state 仅在下载页
   映射为可展示条目，切到其它内容页时绝不写入。 */
const _taskById = new Map();
let _taskRevision = 0;
let _taskStream = null;
let _taskStreamGeneration = 0;
let _taskStreamHealthy = false;
let _taskLastEventAt = 0;
let _taskReconnectTimer = null;
let _taskReconnectFailures = 0;
let _taskHealthTimer = null;
let _fallbackPollTimer = null;
let _pollController = null;

function doneKeySet(tasks) {
  return new Set(tasks.filter(t => t.status === 'done')
    .map(t => { const bv = bvFromUrl(t.url); return bv ? dlKey(bv, t.page || 1) : null; })
    .filter(Boolean));
}

/* 进行中任务的键集合:与 doneKeySet 同粒度((bv, 分P)),给卡片上的「下载中」角标用。
   只有集合本身变化才需要重绘卡片,进度/速度变化不走这里。 */
function activeKeySet(tasks) {
  return new Set(tasks.filter(t => TASK_ACTIVE.includes(t.status))
    .map(t => { const bv = bvFromUrl(t.url); return bv ? dlKey(bv, t.page || 1) : null; })
    .filter(Boolean));
}

function sameSet(a, b) {
  return a.size === b.size && [...a].every(value => b.has(value));
}

function isDownloaded(url) {
  const bv = bvFromUrl(url);
  return !!bv && _doneKeys.has(dlKey(bv, pageFromUrl(url)));
}

/* 卡片要展示的下载状态:''(无) / 'done'(已下载) / 'active'(下载中)。
   与 isDownloaded 同一套键规则,只是多做一步「进行中」判定——
   「已下载」优先:同一视频重复下载时按已完成算,不让角标在两个状态间抖。 */
function downloadState(url) {
  const bv = bvFromUrl(url);
  if (!bv) return '';
  const key = dlKey(bv, pageFromUrl(url));
  if (_doneKeys.has(key)) return 'done';
  return _activeKeys.has(key) ? 'active' : '';
}

function hasVisibleChecks() {
  /* 必须同时看 state.checked:换页回下载页时列表里残留上一轮的旧卡片,
     checkbox 的 checked 属性还在(goPage 只清状态、不清目标页 DOM),
     只按 DOM 判断会把幽灵勾选当成「用户正在选择」,整体重绘被永久搁置,
     卡片勾选态/操作条/计数三态从此不同步(2026-11-11 实测复现)。 */
  return state.checked.size > 0
    && document.querySelectorAll('.page.active input[data-libid]:checked').length > 0;
}

function updateBadge(tasks) {
  const active = tasks.filter(t => TASK_ACTIVE.includes(t.status)).length;
  const badge = document.getElementById('dl-badge');
  if (badge) {
    badge.style.display = active ? '' : 'none';
    badge.textContent = active > 99 ? '99+' : active;
  }
}

function applyTaskBadges(tasks) {
  const nextDone = doneKeySet(tasks);
  const nextActive = activeKeySet(tasks);
  // 两个集合任一变化都要重绘:只认 done 的话,「下载中」角标要等下一次整页刷新
  // 才出现或消失(开始下载时卡片上什么都看不到)。
  const changed = !sameSet(_doneKeys, nextDone) || !sameSet(_activeKeys, nextActive);
  _doneKeys = nextDone;
  _activeKeys = nextActive;
  updateBadge(tasks);
  return changed;
}

function taskToItem(task) {
  const status = normalizedStatus(task.status);
  return {
    task_id: String(task.task_id ?? ''), status,
    url: task.url, title: task.title || '(解析中)', label: task.label || '',
    page: task.page || 1,
    uploader: task.uploader || '', uploader_mid: safeDecimalId(task.uploader_mid) || 0,
    thumbnail: task.thumbnail || '', duration: task.duration || 0,
    view_at: task.finished_at || task.created_at || 0,
    progress: task.progress || 0, speed: task.speed || 0, eta: task.eta || 0,
    error: task.error || '',
    rename_conflict: task.rename_conflict || null,
    rename_error: task.rename_error || '',
    rename_skipped: task.rename_skipped || '',
    _sort: (TASK_ACTIVE.includes(status) ? 0 : 1) * 1e10
      - (task.finished_at || task.created_at || 0),
  };
}

function orderedTaskItems(tasks) {
  return tasks.map(taskToItem).sort((a, b) => a._sort - b._sort);
}

function taskNeedsCardRebuild(previous, next) {
  if (!previous) return true;
  const fields = [
    'status', 'title', 'url', 'label', 'page', 'uploader', 'uploader_mid',
    'thumbnail', 'duration', 'error', 'finished_at', 'created_at',
    'rename_conflict', 'rename_error', 'rename_skipped',
  ];
  return fields.some(field => JSON.stringify(previous[field] ?? null)
    !== JSON.stringify(next[field] ?? null));
}

/* 将任务快照投影到 UI。progress/speed/eta 变化只更新卡片；新增、删除、状态
   或排序字段变化才整体重绘。有可见勾选时延后重绘，但 Map 已按稳定 ID 更新，
   因此批量操作仍精确指向原任务。 */
function commitTaskCollection(tasks, options = {}) {
  const badgesChanged = applyTaskBadges(tasks);
  if (state.mode !== 'downloads') {
    if (badgesChanged && LIB_PAGES.includes(state.mode) && !hasVisibleChecks()) renderPage();
    return;
  }

  replaceItems(orderedTaskItems(tasks));
  const forceRender = options.forceRender === true;
  if (forceRender && hasVisibleChecks()) {
    _dirty = true;
  } else if (forceRender) {
    _dirty = false;
    renderPage();
  } else {
    for (const task of options.changedTasks || []) updateTaskCard(task);
  }
  if (forceRender || (options.changedTasks || []).some(t => TASK_TERMINAL.includes(t.status))) {
    loadStats();
  }
}

function replaceTaskMap(tasks) {
  _taskById.clear();
  for (const task of Array.isArray(tasks) ? tasks : []) {
    const taskId = String(task && task.task_id || '');
    if (taskId) _taskById.set(taskId, task);
  }
}

function taskSnapshot() { return [..._taskById.values()]; }

function cancelDownloadLoad() {
  _loadGeneration++;
  if (_loadController) _loadController.abort();
  _loadController = null;
}

function downloadLoadIsCurrent(generation, controller) {
  return generation === _loadGeneration
    && controller === _loadController
    && !controller.signal.aborted;
}

export async function loadDownloads(prefetchedTasks) {
  cancelDownloadLoad();
  const generation = _loadGeneration;
  const controller = new AbortController();
  _loadController = controller;
  if (state.mode === 'downloads') {
    const d = libDom();
    if (d.meta) d.meta.textContent = '';
  }
  try {
    let tasks = prefetchedTasks;
    if (!Array.isArray(tasks)) {
      const response = await getJSON('/api/tasks', { signal: controller.signal });
      if (response.ok === false) throw new Error(response.error || '加载失败');
      tasks = Array.isArray(response.tasks) ? response.tasks : [];
    }
    if (!downloadLoadIsCurrent(generation, controller)) return;
    replaceTaskMap(tasks);
    commitTaskCollection(tasks, { forceRender: state.mode === 'downloads' });
  } catch (error) {
    if (isAbortError(error) || !downloadLoadIsCurrent(generation, controller)
        || state.mode !== 'downloads') return;
    const d = libDom();
    d.list.innerHTML = `<div class="empty">${escapeHtml(error.message || '加载失败')}<br><button class="ghost" onclick="loadDownloads()">${icon('refresh')} 重试</button></div>`;
  } finally {
    if (downloadLoadIsCurrent(generation, controller)) _loadController = null;
  }
}

/* ---------- 单一聚合 SSE ---------- */
function streamIsRecent() {
  return _taskStreamHealthy && Date.now() - _taskLastEventAt < TASK_STREAM_STALE_MS;
}

function clearFallbackTimer() {
  if (_fallbackPollTimer) clearTimeout(_fallbackPollTimer);
  _fallbackPollTimer = null;
}

function markTaskStreamHealthy(generation) {
  if (generation !== _taskStreamGeneration) return false;
  _taskStreamHealthy = true;
  _taskLastEventAt = Date.now();
  _taskReconnectFailures = 0;
  clearFallbackTimer();
  if (_pollController) _pollController.abort();
  return true;
}

function parseEventData(event) {
  try {
    const payload = JSON.parse(event.data);
    return payload && typeof payload === 'object' ? payload : null;
  } catch (error) {
    return null;
  }
}

function failTaskStream(generation) {
  if (generation !== _taskStreamGeneration) return;
  if (_taskStream) _taskStream.close();
  _taskStream = null;
  _taskStreamHealthy = false;
  _taskStreamGeneration++;
  scheduleFallbackPoll(0);
  if (_taskReconnectTimer) clearTimeout(_taskReconnectTimer);
  _taskReconnectFailures++;
  const delay = Math.min(30000, 1000 * (2 ** Math.min(_taskReconnectFailures, 5)));
  _taskReconnectTimer = setTimeout(() => {
    _taskReconnectTimer = null;
    ensureTaskStream();
  }, delay);
}

function handleTaskSnapshot(event, generation) {
  const payload = parseEventData(event);
  const revision = Number(payload && payload.revision);
  if (!payload || !Array.isArray(payload.tasks) || !Number.isInteger(revision) || revision < 1) {
    failTaskStream(generation);
    return;
  }
  if (!markTaskStreamHealthy(generation)) return;
  cancelDownloadLoad();
  _taskRevision = revision;
  replaceTaskMap(payload.tasks);
  commitTaskCollection(taskSnapshot(), { forceRender: state.mode === 'downloads' });
}

function handleTaskPatch(event, generation) {
  const payload = parseEventData(event);
  const revision = Number(payload && payload.revision);
  if (!payload || !Array.isArray(payload.upserts) || !Array.isArray(payload.removed)
      || !Number.isInteger(revision)) {
    failTaskStream(generation);
    return;
  }
  if (revision <= _taskRevision) {
    markTaskStreamHealthy(generation);
    return;
  }
  if (revision !== _taskRevision + 1) {
    failTaskStream(generation);
    return;
  }

  let rebuild = payload.removed.length > 0;
  for (const task of payload.upserts) {
    const taskId = String(task && task.task_id || '');
    if (!taskId) continue;
    const previous = _taskById.get(taskId);
    if (taskNeedsCardRebuild(previous, task)) rebuild = true;
    _taskById.set(taskId, task);
  }
  for (const rawId of payload.removed) _taskById.delete(String(rawId));
  _taskRevision = revision;
  if (!markTaskStreamHealthy(generation)) return;
  commitTaskCollection(taskSnapshot(), {
    forceRender: rebuild && state.mode === 'downloads',
    changedTasks: payload.upserts,
  });
}

function handleTaskHeartbeat(event, generation) {
  const payload = parseEventData(event);
  const revision = Number(payload && payload.revision);
  if (!Number.isInteger(revision) || revision !== _taskRevision) {
    failTaskStream(generation);
    return;
  }
  markTaskStreamHealthy(generation);
}

export function ensureTaskStream() {
  if (typeof EventSource !== 'function') {
    scheduleFallbackPoll(0);
    return null;
  }
  if (_taskStream && _taskStream.readyState !== 2) return _taskStream;
  if (_taskReconnectTimer) clearTimeout(_taskReconnectTimer);
  _taskReconnectTimer = null;

  const generation = ++_taskStreamGeneration;
  _taskRevision = 0;
  _taskStreamHealthy = false;
  const es = new EventSource('/api/tasks/events');
  _taskStream = es;
  es.addEventListener('snapshot', event => handleTaskSnapshot(event, generation));
  es.addEventListener('patch', event => handleTaskPatch(event, generation));
  es.addEventListener('heartbeat', event => handleTaskHeartbeat(event, generation));
  es.addEventListener('stream-error', () => failTaskStream(generation));
  es.onerror = () => failTaskStream(generation);
  return es;
}

export function startTaskUpdates() {
  ensureTaskStream();
  if (!_taskHealthTimer) {
    _taskHealthTimer = setInterval(() => {
      if (_taskStreamHealthy && !streamIsRecent()) failTaskStream(_taskStreamGeneration);
      else if (!_taskStream) scheduleFallbackPoll(0);
    }, 5000);
  }
}

/* 旧调用点仍可按 task_id 调用，但不再创建每任务 EventSource。 */
export function subscribeProgress(_taskId) {
  ensureTaskStream();
}

function scheduleFallbackPoll(delay = TASK_POLL_INTERVAL_MS) {
  if (streamIsRecent() || _fallbackPollTimer) return;
  _fallbackPollTimer = setTimeout(async () => {
    _fallbackPollTimer = null;
    await pollTasks();
    if (!streamIsRecent()) scheduleFallbackPoll(TASK_POLL_INTERVAL_MS);
  }, delay);
}

/* 仅在聚合流未健康或已超时时兜底；健康 SSE 期间调用会直接返回。 */
export async function pollTasks(force = false) {
  if (!force && streamIsRecent()) return;
  if (_pollController) return;
  const controller = new AbortController();
  _pollController = controller;
  try {
    const response = await getJSON('/api/tasks', { signal: controller.signal });
    if (response.ok === false) throw new Error(response.error || '加载失败');
    if (controller.signal.aborted || streamIsRecent()) return;
    const tasks = Array.isArray(response.tasks) ? response.tasks : [];
    replaceTaskMap(tasks);
    commitTaskCollection(tasks, { forceRender: state.mode === 'downloads' });
  } catch (error) {
    if (!isAbortError(error)) {
      // 保持现有快照；下一轮 fallback 会重试。
    }
  } finally {
    if (_pollController === controller) _pollController = null;
  }
}

/* ---------- 进度卡就地更新 ---------- */
function updateTaskCard(task) {
  if (state.mode !== 'downloads') return;
  const taskId = String(task.task_id ?? '');
  const card = document.getElementById('vt-' + taskId);
  if (!card) return;
  const rawProgress = Number(task.progress);
  const pct = Math.min(100, Math.max(0, Number.isFinite(rawProgress) ? rawProgress : 0));
  const progress = card.querySelector('.dp-bar');
  if (progress) progress.setAttribute('aria-valuenow', String(pct));
  const bar = card.querySelector('.dp-bar i');
  if (bar) bar.style.width = pct + '%';
  const set = (selector, value) => {
    const el = card.querySelector(selector);
    if (el && value != null) el.textContent = value;
  };
  set('.dp-pct', pct + '%');
  // 断流/卡顿时 speed/eta 会归零:必须显式清写,否则卡片一直挂着
  // 上一次的正值速度和「剩余 Ns」,看起来像还在下载。
  const speed = Number(task.speed);
  if (Number.isFinite(speed) && speed > 0) {
    set('.dp-spd', speed > 1024 ? (speed / 1024).toFixed(1) + ' MB/s' : speed.toFixed(0) + ' KB/s');
  } else {
    set('.dp-spd', '0 KB/s');
  }
  const eta = Number(task.eta);
  if (Number.isFinite(eta) && eta > 0) set('.dp-eta', '剩余 ' + Math.ceil(eta) + 's');
  else set('.dp-eta', '');
  const status = normalizedStatus(task.status);
  const tag = card.querySelector('.status-tag');
  if (tag) {
    tag.className = 'status-tag ' + status;
    tag.textContent = TASK_STATUS_CN[status] || status;
  }
}

export async function cancelTask(taskId, button = null) {
  try {
    await withPending(button, '取消中…', async () => {
      const response = await postEmpty('/api/cancel/' + encodeURIComponent(String(taskId ?? '')));
      if (response.ok === false) throw new Error(response.error || '取消失败');
    });
    announce('已发送取消请求');
  } catch (error) {
    announce(error.message || '取消失败', true);
    alert(error.message || '取消失败');
  }
}

/* ---------- 任务卡渲染 ---------- */
function taskWarnHtml(it) {
  const taskArg = inlineStringArg(it.task_id);
  if (it.rename_conflict) {
    return `<div class="dp-err dp-err-actions">名称冲突：已有同名文件
      <button class="ghost" onclick="resolveTaskConflict(${taskArg},'rename_with_index',this)">加 (1) 改名</button>
      <button class="ghost" onclick="resolveTaskConflict(${taskArg},'open_existing',this)">打开现有文件</button>
      <button class="ghost" onclick="resolveTaskConflict(${taskArg},'cancel',this)">忽略</button></div>`;
  }
  if (it.rename_error) {
    return `<div class="dp-err" title="${escapeAttr(it.rename_error)}">自动改名失败：${escapeHtml(String(it.rename_error).slice(0, 60))}</div>`;
  }
  if (it.rename_skipped) return '<div class="dp-err">已有同名同大小的文件，未覆盖改名</div>';
  return '';
}

function taskCardHtml(it, isGrid, checkHtml, checkedCls) {
  const status = normalizedStatus(it.status);
  const active = TASK_ACTIVE.includes(status);
  const rawProgress = Number(it.progress);
  const pct = Math.min(100, Math.max(0, Number.isFinite(rawProgress) ? rawProgress : 0));
  const rawSpeed = Number(it.speed);
  const speed = Number.isFinite(rawSpeed) && rawSpeed > 0 ? rawSpeed : 0;
  const rawEta = Number(it.eta);
  const eta = Number.isFinite(rawEta) && rawEta > 0 ? Math.ceil(rawEta) : 0;
  const spd = speed > 1024 ? (speed / 1024).toFixed(1) + ' MB/s' : speed.toFixed(0) + ' KB/s';
  const bar = active
    ? `<div class="dp-bar" role="progressbar" aria-label="${escapeAttr(it.title || '任务')}下载进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>`
    : '';
  const err = ['failed', 'interrupted'].includes(status) && it.error
    ? `<div class="dp-err" title="${escapeAttr(it.error)}">${escapeHtml(String(it.error).slice(0, 80))}</div>` : '';
  const live = status === 'downloading'
    ? `<span class="dp-pct">${pct}%</span><span class="dp-spd">${spd}</span>`
      + (eta ? `<span class="dp-eta">剩余 ${eta}s</span>` : '')
    : (status === 'merging' ? '<span class="dp-eta">正在合并音视频...</span>' : '');
  const metaInner = `<span class="status-tag ${status}">${TASK_STATUS_CN[status] || status}</span>${live}`
    + (it.label ? `<span class="q-label">[${escapeHtml(it.label)}]</span>` : '');
  const taskArg = inlineStringArg(it.task_id);
  const cancelBtn = active
    ? `<button class="danger" onclick="cancelTask(${taskArg},this)">取消</button>` : '';
  const targetUrl = safeHttpUrl(it.url);
  const titleHtml = targetUrl
    ? `<a href="${escapeAttr(targetUrl)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>`
    : escapeHtml(it.title);
  const thumbnail = safeHttpUrl(it.thumbnail, { httpsOnly: true });
  const thumb = `<div class="thumb-wrap"><img src="${escapeAttr(thumbnail)}" alt="" onerror="this.style.visibility='hidden'" loading="lazy"></div>`;
  const warn = taskWarnHtml(it);
  const domId = escapeAttr(String(it.task_id ?? ''));
  if (!isGrid) {
    return `
    <div class="video-item lv${checkedCls}" id="vt-${domId}">
      ${checkHtml}${thumb}
      <div class="lv-body">
        <div class="vi-title">${titleHtml}</div>
        ${bar}
        <div class="dp-meta" aria-live="polite">${metaInner}${cancelBtn}</div>
        ${err}${warn}
      </div>
    </div>`;
  }
  return `
  <div class="video-item${checkedCls}" id="vt-${domId}">
    ${checkHtml}${thumb}
    <div class="vi-title">${titleHtml}</div>
    ${bar}
    <div class="dp-meta">${metaInner}${cancelBtn}</div>
    ${err}${warn}
  </div>`;
}

/* ---------- 记录与文件操作 ---------- */
function deleteResultMessage(response, deleteFile) {
  const removed = Number(response.count) || 0;
  const retained = Number(response.retained_count) || 0;
  const lines = [`已删除 ${removed} 条记录`];
  if (retained) lines.push(`${retained} 条记录仍保留（任务可能仍在运行或已变化）`);
  if (deleteFile) {
    const deleted = Number(response.file_deleted_count) || 0;
    const skipped = Number(response.file_skipped_count) || 0;
    const failed = Number(response.file_failed_count) || 0;
    lines.push(`本地文件：已删除 ${deleted} 个，未找到或安全跳过 ${skipped} 个，失败 ${failed} 个`);
  }
  if (response.partial || Number(response.file_failed_count)) {
    lines.push('操作仅部分完成，请按上述结果检查保留项。');
  }
  return lines.join('\n');
}

export async function delSelected(deleteFile) {
  // 在打开模态确认前冻结稳定任务 ID；后续刷新或排序不能改变本次操作对象。
  const ids = selectedVisibleItems().map(item => item.task_id).filter(Boolean);
  if (ids.length === 0) { alert('请先勾选要删除的任务'); return; }
  const accepted = await confirmAction({
    title: deleteFile ? '删除记录和本地文件？' : '删除所选记录？',
    message: deleteFile
      ? `将删除 ${ids.length} 条任务记录及其可确认归属的本地文件。`
      : `将删除 ${ids.length} 条任务记录；本地文件会保留。`,
    detail: deleteFile ? '本地文件删除后无法恢复。路径归属不明确的文件会被安全跳过。' : '',
    confirmLabel: deleteFile ? '删除记录和文件' : '删除记录',
  });
  if (!accepted) return;
  const buttons = [document.getElementById('ab-del-rec'), document.getElementById('ab-del-file')];
  try {
    let response;
    await withPending(buttons, '删除中…', async () => {
      response = await postJSON('/api/task/delete', { ids, delete_file: deleteFile });
      if (!response.ok) throw new Error(response.error || '删除失败');
    });
    const message = deleteResultMessage(response, deleteFile);
    state.checked.clear();
    await loadDownloads();
    announce(message, !!response.partial);
    alert(message);
  } catch (error) {
    announce(error.message || '删除失败', true);
    alert(error.message || '删除失败');
  }
}

export async function clearAllRecords() {
  const accepted = await confirmAction({
    title: '清除全部已结束记录？',
    message: '将清除已完成、失败、已取消和已中断的任务记录。正在运行的任务与所有本地文件都会保留。',
    confirmLabel: '清除已结束记录',
  });
  if (!accepted) return;
  const button = document.getElementById('clear-terminal-btn');
  try {
    let response;
    await withPending(button, '清除中…', async () => {
      response = await postJSON('/api/tasks/clear', { delete_file: false });
      if (!response.ok) throw new Error(response.error || '清除失败');
    });
    const message = response.count
      ? `已清除 ${response.count} 条已结束记录，本地文件均已保留`
      : '没有可清除的已结束记录';
    state.checked.clear();
    await loadDownloads();
    announce(message);
    alert(message);
  } catch (error) {
    announce(error.message || '清除失败', true);
    alert(error.message || '清除失败');
  }
}

export async function clearInvalidRecords() {
  const accepted = await confirmAction({
    title: '清除失败和失效记录？',
    message: '将删除失败任务，以及已完成但本地文件已确认不存在的记录。不会删除现有本地文件。',
    confirmLabel: '确认清理',
  });
  if (!accepted) return;
  const button = document.getElementById('clean-invalid-btn');
  try {
    let response;
    await withPending(button, '清理中…', async () => {
      response = await postEmpty('/api/tasks/clean-invalid');
      if (!response.ok) throw new Error(response.error || '清理失败');
    });
    const message = response.removed
      ? `已删除 ${response.removed} 条记录\n（下载失败 ${response.failed} 条，文件已失效 ${response.invalid} 条）`
      : '没有失败或失效的记录';
    state.checked.clear();
    await loadDownloads();
    announce(message);
    alert(message);
  } catch (error) {
    announce(error.message || '清理失败', true);
    alert(error.message || '清理失败');
  }
}

export async function revealTask(taskId) {
  if (!taskId) return;
  try {
    taskId = encodeURIComponent(String(taskId));
    const response = await postEmpty('/api/task/' + taskId + '/reveal');
    if (!response.ok) alert('打开失败：' + (response.error || ''));
    else if (!response.found) alert('没找到该视频的文件（可能已被删除或移动），已打开下载目录。');
  } catch (error) { alert('打开失败：' + error.message); }
}

export async function resolveTaskConflict(taskId, action, button = null) {
  try {
    await withPending(button, '处理中…', async () => {
      const encodedTaskId = encodeURIComponent(String(taskId ?? ''));
      const allowedActions = ['rename_with_index', 'cancel', 'open_existing'];
      if (!allowedActions.includes(action)) throw new Error('非法处理动作');
      const response = await postJSON('/api/task/' + encodedTaskId + '/resolve-conflict', { action });
      if (!response.ok) throw new Error(response.error || '处理失败');
      await loadDownloads();
    });
    announce('名称冲突处理完成');
  } catch (error) {
    announce(error.message || '处理失败', true);
    alert(error.message || '处理失败');
  }
}

async function loadStats() {
  try {
    const response = await getJSON('/api/stats');
    if (!response.ok) return;
    const el = document.getElementById('dl-stats-line');
    if (el) el.textContent = `总下载 ${response.total_count} 条 · ${formatBytes(response.total_bytes)} · 本月 ${response.month_count} · 今日 ${response.day_count}`;
  } catch (error) { /* 统计失败不影响任务列表 */ }
}

/* ---------- 所有内容页共用的批量下载 ---------- */
export async function libDownload() {
  const selected = selectedVisibleItems();
  if (selected.length === 0) { alert('请至少勾选一项'); return; }
  const qualitySelect = document.querySelector('.lib-quality');
  const label = qualitySelect ? qualitySelect.value : '';
  let items = selected.map((it) => ({
    url: it.url, title: it.title, page: it.page || pageFromUrl(it.url),
    thumbnail: it.thumbnail || '', uploader: it.uploader || '',
    uploader_mid: it.uploader_mid || 0, duration: it.duration || 0,
  }));
  const skipped = items.filter(it => isDownloaded(it.url)).length;
  items = items.filter(it => !isDownloaded(it.url));
  if (items.length === 0) { alert('所选项均已下载过'); return; }
  try {
    const response = await postJSON('/api/batch/download', { items, label });
    if (!response.ok) throw new Error(response.error || '启动失败');
    alert(`已加入 ${response.count} 个下载任务${skipped ? `（跳过 ${skipped} 个已下载）` : ''}`);
    response.task_ids.forEach(subscribeProgress);
    goPage('downloads');
  } catch (error) { alert(error.message); }
}

setItemExtras({
  isProgressCard: (it) => state.mode === 'downloads' && !!it.status && it.status !== 'done',
  progressCard: taskCardHtml,
  warnHtml: taskWarnHtml,
  isDownloaded,
  downloadState,
  onChecksEmpty: () => {
    if (state.mode === 'downloads' && _dirty) {
      _dirty = false;
      commitTaskCollection(taskSnapshot(), { forceRender: true });
    }
  },
});

registerPage('downloads', {
  load: loadDownloads,
  retry: loadDownloads,
  deactivate: cancelDownloadLoad,
  actionBar: 'delete',
  emptyRetry: false,
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '还没有下载记录，点右上角「新建下载」开始',
  /* 统计口径唯一由 metaText 提供,不再旁路直写 meta 行:旧 updateDownloadsMeta
     先写「已下载 N 条」,renderDefaultList 随后覆盖成「共 N 条」,SSE 增量路径
     又写回——同一位置两种文案来回跳(2026-11-11 复现)。 */
  metaText: (q, pairs) => {
    if (q) return `共 ${state.items.length} 条 · 匹配 ${pairs.length}`;
    const done = state.items.filter(item => item.status === 'done').length;
    const active = state.items.filter(item => TASK_ACTIVE.includes(item.status)).length;
    return `已下载 ${done} 条` + (active ? ` · 进行中 ${active}` : '');
  },
});
