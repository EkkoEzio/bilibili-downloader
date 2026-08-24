/* 下载记录页:队列已并入(进行中任务在前,已完成在后)。
   任务进度卡 / SSE 就地更新 / 侧边栏角标 / 统计行 / 记录删除 / 冲突处理。 */
import { registerPage, state, libDom, renderPage, setItemExtras, LIB_PAGES, goPage } from '../lib.js';
import { getJSON, postJSON, postEmpty } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, formatBytes, bvFromUrl } from '../util.js';

const TASK_ACTIVE = ['queued', 'downloading', 'merging', 'cancelling', 'retrying'];
const TASK_STATUS_CN = {
  queued: '排队中', downloading: '下载中', merging: '合并中',
  cancelling: '取消中', retrying: '重试中', failed: '失败',
  cancelled: '已取消', interrupted: '已中断',
};

/* 已下载判定(列表角标 + 批量跳过):done 任务的 BV 集合 */
let _doneBvids = new Set();

function isDownloaded(url) {
  const bv = bvFromUrl(url);
  return !!bv && _doneBvids.has(bv);
}

export async function loadDownloads() {
  const d = libDom();
  d.meta.textContent = '';
  try {
    const r = await getJSON('/api/tasks');
    const tasks = r.tasks || [];
    _doneBvids = new Set(tasks.filter(t => t.status === 'done')
      .map(t => bvFromUrl(t.url)).filter(Boolean));
    updateBadge(tasks);
    // 进行中在最前(时间倒序),其余(完成/失败/取消)按完成时间倒序
    state.items = tasks.map(t => {
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
    const doneN = state.items.filter(x => x.status === 'done').length;
    const activeN = state.items.filter(x => TASK_ACTIVE.includes(x.status)).length;
    d.meta.textContent = `已下载 ${doneN} 条` + (activeN ? ` · 进行中 ${activeN}` : '');
    loadStats();
    tasks.filter(t => TASK_ACTIVE.includes(t.status)).forEach(t => subscribeProgress(t.task_id));
  } catch (e) {
    d.list.innerHTML = `<div class="empty">${e.message}<br><button class="ghost sm" onclick="loadDownloads()">${icon('refresh')} 重试</button></div>`;
  }
}

/* 进行中任务数 → 侧边栏「下载记录」角标 */
function updateBadge(tasks) {
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
  if (!card) { maybeReload(); return; }
  const pct = Math.min(100, Math.max(0, t.progress || 0));
  const bar = card.querySelector('.dp-bar i');
  if (bar) bar.style.width = pct + '%';
  const set = (sel, val) => { const el = card.querySelector(sel); if (el && val != null) el.textContent = val; };
  set('.dp-pct', pct + '%');
  if (t.speed) set('.dp-spd', t.speed > 1024 ? (t.speed / 1024).toFixed(1) + ' MB/s' : t.speed.toFixed(0) + ' KB/s');
  if (t.eta) set('.dp-eta', '剩余 ' + t.eta + 's');
  const tag = card.querySelector('.status-tag');
  if (tag) { tag.className = 'status-tag ' + t.status; tag.textContent = TASK_STATUS_CN[t.status] || t.status; }
  if (['done', 'failed', 'cancelled', 'interrupted'].includes(t.status)) maybeReload();
}

/* 勾选期间不整体重拉(列表会重排、勾选索引移位),清空勾选后再拉 */
let _dirty = false;
function maybeReload() {
  if (document.querySelectorAll('.page.active input[data-libidx]:checked').length) {
    _dirty = true;
    return;
  }
  loadDownloads().then(() => {
    if (state.mode !== 'downloads' && LIB_PAGES.includes(state.mode)) renderPage();  // 刷其它页的「已下载」角标
  });
}

/* 30 秒兜底轮询(与 SSE 互为保险):角标 + 下载记录页静默刷新 */
export async function pollTasks() {
  try {
    const tasks = await getJSON('/api/tasks').then(r => r.tasks || []);
    _doneBvids = new Set(tasks.filter(t => t.status === 'done')
      .map(t => bvFromUrl(t.url)).filter(Boolean));
    updateBadge(tasks);
    if (state.mode === 'downloads'
        && !document.querySelectorAll('.page.active input[data-libidx]:checked').length) {
      loadDownloads();
    }
  } catch (e) { /* ignore */ }
}

/* ---------- SSE 订阅 ---------- */
const _activeSSE = {};

export function subscribeProgress(taskId, retries) {
  if (_activeSSE[taskId]) return;
  retries = retries || 0;
  const es = new EventSource('/api/progress/' + taskId);
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

export async function cancelTask(taskId) {
  try {
    await postEmpty('/api/cancel/' + taskId);
  } catch (e) { /* SSE 会更新状态 */ }
}

/* ---------- 任务卡渲染(注入 lib 的列表框架) ---------- */
/* 文件名对齐提示(名称冲突 / 自动改名失败 / 同名同大小跳过) */
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

/* 任务卡:进行中带进度条(供 SSE 就地更新),失败显示原因 */
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
  const warn = taskWarnHtml(it);
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
        ${warn}
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
    ${warn}
  </div>`;
}

/* ---------- 记录删除(勾选后的底部操作条;按按钮语义直接删,不弹窗) ---------- */
export async function delSelected(deleteFile) {
  const checked = [...document.querySelectorAll('.page.active input[data-libidx]:checked')];
  const ids = checked.map(cb => (state.items[parseInt(cb.dataset.libidx)] || {}).task_id).filter(Boolean);
  if (ids.length === 0) { alert('请先勾选要删除的任务'); return; }
  try {
    const r = await postJSON('/api/task/delete', { ids, delete_file: deleteFile });
    if (!r.ok) throw new Error(r.error || '删除失败');
    state.checked.clear();
    await loadDownloads();
  } catch (e) { alert(e.message); }
}

/* 清除所有已完成/失败记录(只删记录,不动文件) */
export async function clearAllRecords() {
  try {
    const r = await postJSON('/api/tasks/clear', { delete_file: false });
    if (!r.ok) throw new Error(r.error || '清除失败');
    if (!r.count) { alert('没有可清除的记录'); return; }
    alert(`已清除 ${r.count} 条记录`);
    state.checked.clear();
    await loadDownloads();
  } catch (e) { alert(e.message); }
}

/* 清除失败/失效记录:失败任务 + 文件已不在本地的完成任务(只删记录) */
export async function clearInvalidRecords() {
  try {
    const r = await postEmpty('/api/tasks/clean-invalid');
    if (!r.ok) throw new Error(r.error || '清理失败');
    if (!r.removed) { alert('没有失败/失效的记录'); return; }
    alert(`已删除 ${r.removed} 条记录\n(下载失败 ${r.failed} 条,文件已失效 ${r.invalid} 条)`);
    state.checked.clear();
    await loadDownloads();
  } catch (e) { alert(e.message); }
}

export async function revealTask(taskId) {
  if (!taskId) return;
  try {
    const r = await postEmpty('/api/task/' + taskId + '/reveal');
    if (!r.ok) alert('打开失败: ' + (r.error || ''));
    else if (!r.found) alert('没找到该视频的文件(可能已被删除或移动),已打开下载目录。');
  } catch (e) { alert('打开失败: ' + e.message); }
}

/* 处理名称冲突(加后缀改名 / 打开现有文件 / 忽略) */
export async function resolveTaskConflict(taskId, action) {
  try {
    const r = await postJSON('/api/task/' + taskId + '/resolve-conflict', { action });
    if (!r.ok) throw new Error(r.error || '处理失败');
    loadDownloads();
  } catch (e) { alert(e.message); }
}

/* ---------- 统计行 ---------- */
async function loadStats() {
  try {
    const r = await getJSON('/api/stats');
    if (!r.ok) return;
    const el = document.getElementById('dl-stats-line');
    if (el) el.textContent = `总下载 ${r.total_count} 条 · ${formatBytes(r.total_bytes)} · 本月 ${r.month_count} · 今日 ${r.day_count}`;
  } catch (e) {}
}

/* ---------- 底部操作条:批量下载勾选项(所有内容页通用) ---------- */
export async function libDownload() {
  const checked = [...document.querySelectorAll('.page.active input[data-libidx]:checked')];
  if (checked.length === 0) { alert('请至少勾选一项'); return; }
  const qSel = document.querySelector('.lib-quality');
  const label = qSel ? qSel.value : '';
  let items = checked.map(cb => {
    const it = state.items[parseInt(cb.dataset.libidx)];
    return { url: it.url, title: it.title, page: 1,
             thumbnail: it.thumbnail || '', uploader: it.uploader || '',
             uploader_mid: it.uploader_mid || 0, duration: it.duration || 0 };
  });
  const skipped = items.filter(it => isDownloaded(it.url)).length;
  items = items.filter(it => !isDownloaded(it.url));
  if (items.length === 0) { alert('所选项均已下载过'); return; }
  try {
    const r = await postJSON('/api/batch/download', { items, label });
    if (!r.ok) throw new Error(r.error || '启动失败');
    alert(`已加入 ${r.count} 个下载任务${skipped ? `(跳过 ${skipped} 个已下载)` : ''}`);
    r.task_ids.forEach(subscribeProgress);
    goPage('downloads');
  } catch (e) { alert(e.message); }
}

/* ---------- 注册:控制器 + 任务卡差异注入 ---------- */
setItemExtras({
  isProgressCard: (it) => state.mode === 'downloads' && !!it.status && it.status !== 'done',
  progressCard: taskCardHtml,
  warnHtml: taskWarnHtml,
  isDownloaded,
  onChecksEmpty: () => {
    if (state.mode === 'downloads' && _dirty) { _dirty = false; loadDownloads(); }
  },
});

registerPage('downloads', {
  load: loadDownloads,
  retry: loadDownloads,
  actionBar: 'delete',
  emptyRetry: false,
  emptyHint: (q) => q ? '没有匹配结果(试试其他关键词)' : '还没有下载记录,点右上角「新建下载」开始',
});
