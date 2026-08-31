/* 下载记录页:队列已并入(进行中任务在前,已完成在后)。
   任务进度卡 / SSE 就地更新 / 侧边栏角标 / 统计行 / 记录删除 / 冲突处理。 */
import { registerPage, state, libDom, renderPage, setItemExtras, LIB_PAGES, goPage } from '../lib.js';
import { getJSON, postJSON, postEmpty } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, formatBytes, bvFromUrl, dlKey, pageFromUrl,
         safeHttpUrl, safeDecimalId } from '../util.js';

const TASK_ACTIVE = ['queued', 'downloading', 'merging', 'cancelling', 'retrying'];
const TASK_STATUS_CN = {
  queued: '排队中', downloading: '下载中', merging: '合并中',
  cancelling: '取消中', retrying: '重试中', failed: '失败',
  cancelled: '已取消', interrupted: '已中断',
};
const TASK_TERMINAL = ['done', 'failed', 'cancelled', 'interrupted'];
const TASK_KNOWN = new Set([...TASK_ACTIVE, ...TASK_TERMINAL]);

function normalizedStatus(value) {
  const status = String(value ?? '');
  return TASK_KNOWN.has(status) ? status : 'unknown';
}

function inlineStringArg(value) {
  return escapeAttr(JSON.stringify(String(value ?? '')));
}

/* 已下载判定(列表角标 + 批量跳过):done 任务的 (BV, 分P) 键集合。
   只按 BV 建集合会让合集下完 P1 后其余分 P 全被当已下载 —— 角标全挂上、
   批量提交时被整批过滤掉并提示「所选项均已下载过」,多 P / 合集主场景直接不可用。
   键规则(dlKey / pageFromUrl)的唯一来源在 ../util.js,这里只 import 不复制。 */
let _doneKeys = new Set();
let _loadSeq = 0;

/* 集合构建的唯一来源(loadDownloads 与 pollTasks 共用,不许各写一份)。
   任务侧的 page 读 t.page —— /api/tasks 的任务对象已含该字段(start_download /
   start_batch 都写、_persist 也不剔除),不能从 t.url 解析:任务表里的 url 是用户
   粘贴的原始链接,常不带 ?p=,解析它会把所有任务都算成 P1,判定照样全错。
   任务 url 取不到 BV(短链 / av 号原样入库)时整条丢掉,不拼出带 null 的键。 */
function doneKeySet(tasks) {
  return new Set(tasks.filter(t => t.status === 'done')
    .map(t => { const bv = bvFromUrl(t.url); return bv ? dlKey(bv, t.page || 1) : null; })
    .filter(Boolean));
}

function sameSet(a, b) {
  return a.size === b.size && [...a].every(value => b.has(value));
}

/* 查询侧的 page 只能从 URL 的 ?p= 解析(入参只有 URL,拿不到任务对象)。
   与任务侧取法不对称是刻意的,写反任一侧都会悄悄退回「只看 BV」的缺陷形态。 */
function isDownloaded(url) {
  const bv = bvFromUrl(url);
  return !!bv && _doneKeys.has(dlKey(bv, pageFromUrl(url)));
}

function applyTaskSnapshot(tasks) {
  const nextDone = doneKeySet(tasks);
  const changed = !sameSet(_doneKeys, nextDone);
  _doneKeys = nextDone;
  updateBadge(tasks);
  tasks.filter(t => TASK_ACTIVE.includes(t.status))
    .forEach(t => subscribeProgress(String(t.task_id ?? '')));
  return changed;
}

export async function loadDownloads(prefetchedTasks) {
  const seq = ++_loadSeq;
  if (state.mode === 'downloads') {
    const d = libDom();
    d.meta.textContent = '';
  }
  try {
    let tasks = prefetchedTasks;
    if (!Array.isArray(tasks)) {
      const r = await getJSON('/api/tasks');
      tasks = Array.isArray(r.tasks) ? r.tasks : [];
    }
    // 请求代次同时解决 downloads → 其它页 → downloads 的 ABA 旧响应覆盖。
    if (seq !== _loadSeq) return;
    const badgesChanged = applyTaskSnapshot(tasks);
    if (state.mode !== 'downloads') {
      // 只重绘当前内容页的已下载角标，绝不把下载任务写进共享 state.items。
      if (badgesChanged && LIB_PAGES.includes(state.mode)
          && !document.querySelectorAll('.page.active input[data-libidx]:checked').length) {
        renderPage();
      }
      return;
    }

    // 进行中在最前(时间倒序),其余(完成/失败/取消)按完成时间倒序
    state.items = tasks.map(t => {
      const status = normalizedStatus(t.status);
      const it = {
        task_id: String(t.task_id ?? ''), status,
        url: t.url, title: t.title || '(解析中)', label: t.label || '',
        /* page 必须进白名单:它是任务的权威分 P 值(/api/tasks 已返回),
           libDownload 重下这条记录时靠它提交正确的分 P。漏掉这一行 it.page 就是
           undefined,提交会静默退回 P1(缺陷 C2)。
           读 t.page 而不是解析 t.url —— 任务表里的 url 是用户粘贴的原始链接,
           常不带 ?p=,解析它会把所有任务都算成 P1,与漏掉字段等价。 */
        page: t.page || 1,
        uploader: t.uploader || '', uploader_mid: safeDecimalId(t.uploader_mid) || 0,
        thumbnail: t.thumbnail || '', duration: t.duration || 0,
        view_at: t.finished_at || t.created_at || 0,
        progress: t.progress || 0, speed: t.speed || 0, eta: t.eta || 0,
        error: t.error || '',
        rename_conflict: t.rename_conflict || null,
        rename_error: t.rename_error || '',
        rename_skipped: t.rename_skipped || '',
        _sort: (TASK_ACTIVE.includes(status) ? 0 : 1) * 1e10 - (t.finished_at || t.created_at || 0),
      };
      return it;
    }).sort((a, b) => a._sort - b._sort);
    if (!document.querySelectorAll('.page.active input[data-libidx]:checked').length) _dirty = false;
    renderPage();
    const doneN = state.items.filter(x => x.status === 'done').length;
    const activeN = state.items.filter(x => TASK_ACTIVE.includes(x.status)).length;
    const d = libDom();
    d.meta.textContent = `已下载 ${doneN} 条` + (activeN ? ` · 进行中 ${activeN}` : '');
    loadStats();
  } catch (e) {
    if (seq !== _loadSeq || state.mode !== 'downloads') return;
    const d = libDom();
    d.list.innerHTML = `<div class="empty">${escapeHtml(e.message || '加载失败')}<br><button class="ghost sm" onclick="loadDownloads()">${icon('refresh')} 重试</button></div>`;
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
  const terminal = TASK_TERMINAL.includes(t.status);
  // 切到其它页后忽略进行中 tick；仅终态刷新任务快照和当前页角标。
  if (state.mode !== 'downloads') {
    if (terminal) maybeReload();
    return;
  }
  const taskId = String(t.task_id ?? '');
  const card = document.getElementById('vt-' + taskId);
  if (!card) { maybeReload(); return; }
  const rawProgress = Number(t.progress);
  const pct = Math.min(100, Math.max(0, Number.isFinite(rawProgress) ? rawProgress : 0));
  const bar = card.querySelector('.dp-bar i');
  if (bar) bar.style.width = pct + '%';
  const set = (sel, val) => { const el = card.querySelector(sel); if (el && val != null) el.textContent = val; };
  set('.dp-pct', pct + '%');
  const speed = Number(t.speed);
  if (Number.isFinite(speed) && speed > 0) {
    set('.dp-spd', speed > 1024 ? (speed / 1024).toFixed(1) + ' MB/s' : speed.toFixed(0) + ' KB/s');
  }
  const eta = Number(t.eta);
  if (Number.isFinite(eta) && eta > 0) set('.dp-eta', '剩余 ' + Math.ceil(eta) + 's');
  const status = normalizedStatus(t.status);
  const tag = card.querySelector('.status-tag');
  if (tag) { tag.className = 'status-tag ' + status; tag.textContent = TASK_STATUS_CN[status] || status; }
  if (terminal) maybeReload();
}

/* 勾选期间不整体重拉(列表会重排、勾选索引移位),清空勾选后再拉 */
let _dirty = false;
function maybeReload() {
  if (state.mode === 'downloads'
      && document.querySelectorAll('.page.active input[data-libidx]:checked').length) {
    _dirty = true;
    return;
  }
  loadDownloads();
}

/* 30 秒兜底轮询(与 SSE 互为保险):角标 + 下载记录页静默刷新 */
export async function pollTasks() {
  try {
    const tasks = await getJSON('/api/tasks')
      .then(r => Array.isArray(r.tasks) ? r.tasks : []);
    const nextDone = doneKeySet(tasks);
    const badgesChanged = !sameSet(_doneKeys, nextDone);
    _doneKeys = nextDone;
    updateBadge(tasks);
    tasks.filter(t => TASK_ACTIVE.includes(t.status))
      .forEach(t => subscribeProgress(String(t.task_id ?? '')));
    if (state.mode === 'downloads'
        && !document.querySelectorAll('.page.active input[data-libidx]:checked').length) {
      loadDownloads(tasks);  // 复用本次快照，不再发第二次 /api/tasks
    } else if (state.mode !== 'downloads' && badgesChanged && LIB_PAGES.includes(state.mode)) {
      renderPage();
    }
  } catch (e) { /* ignore */ }
}

/* ---------- SSE 订阅 ---------- */
const _activeSSE = {};

export function subscribeProgress(taskId, retries) {
  taskId = String(taskId ?? '');
  if (!taskId || _activeSSE[taskId]) return;
  retries = retries || 0;
  const es = new EventSource('/api/progress/' + encodeURIComponent(taskId));
  _activeSSE[taskId] = es;
  es.onmessage = (ev) => {
    retries = 0;
    let t;
    try { t = JSON.parse(ev.data); } catch (e) { return; }
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
    await postEmpty('/api/cancel/' + encodeURIComponent(String(taskId ?? '')));
  } catch (e) { /* SSE 会更新状态 */ }
}

/* ---------- 任务卡渲染(注入 lib 的列表框架) ---------- */
/* 文件名对齐提示(名称冲突 / 自动改名失败 / 同名同大小跳过) */
function taskWarnHtml(it) {
  const taskArg = inlineStringArg(it.task_id);
  if (it.rename_conflict) {
    return `<div class="dp-err">名称冲突:已有同名文件
      <button class="ghost sm" onclick="resolveTaskConflict(${taskArg},'rename_with_index')">加 (1) 改名</button>
      <button class="ghost sm" onclick="resolveTaskConflict(${taskArg},'open_existing')">打开现有文件</button>
      <button class="ghost sm" onclick="resolveTaskConflict(${taskArg},'cancel')">忽略</button></div>`;
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
    ? `<div class="dp-bar"><i style="width:${pct}%"></i></div>` : '';
  const err = ['failed', 'interrupted'].includes(status) && it.error
    ? `<div class="dp-err" title="${escapeAttr(it.error)}">${escapeHtml(String(it.error).slice(0, 80))}</div>` : '';
  const live = status === 'downloading'
    ? `<span class="dp-pct">${pct}%</span><span class="dp-spd">${spd}</span>` +
      (eta ? `<span class="dp-eta">剩余 ${eta}s</span>` : '')
    : (status === 'merging' ? '<span class="dp-eta">正在合并音视频...</span>' : '');
  const metaInner = `<span class="status-tag ${status}">${TASK_STATUS_CN[status] || status}</span>${live}` +
    (it.label ? `<span class="q-label">[${escapeHtml(it.label)}]</span>` : '');
  const taskArg = inlineStringArg(it.task_id);
  const cancelBtn = active
    ? `<button class="danger sm" onclick="cancelTask(${taskArg})">取消</button>` : '';
  const targetUrl = safeHttpUrl(it.url);
  const titleHtml = targetUrl
    ? `<a href="${escapeAttr(targetUrl)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>`
    : escapeHtml(it.title);
  const thumbnail = safeHttpUrl(it.thumbnail, { httpsOnly: true });
  const thumb = `<div class="thumb-wrap"><img src="${escapeAttr(thumbnail)}" onerror="this.style.visibility='hidden'" loading="lazy"></div>`;
  const warn = taskWarnHtml(it);
  const domId = escapeAttr(String(it.task_id ?? ''));
  if (!isGrid) {
    return `
    <div class="video-item lv${checkedCls}" id="vt-${domId}">
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
  <div class="video-item${checkedCls}" id="vt-${domId}">
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
    taskId = encodeURIComponent(String(taskId));
    const r = await postEmpty('/api/task/' + taskId + '/reveal');
    if (!r.ok) alert('打开失败: ' + (r.error || ''));
    else if (!r.found) alert('没找到该视频的文件(可能已被删除或移动),已打开下载目录。');
  } catch (e) { alert('打开失败: ' + e.message); }
}

/* 处理名称冲突(加后缀改名 / 打开现有文件 / 忽略) */
export async function resolveTaskConflict(taskId, action) {
  try {
    const encodedTaskId = encodeURIComponent(String(taskId ?? ''));
    const allowedActions = ['rename_with_index', 'cancel', 'open_existing'];
    if (!allowedActions.includes(action)) throw new Error('非法处理动作');
    const r = await postJSON('/api/task/' + encodedTaskId + '/resolve-conflict', { action });
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
  /* page 的 `||` 兜底同时服务两类条目(libDownload 是六个内容页共用的提交口):
       - 任务记录(下载记录页):state.items 由 loadDownloads 从 /api/tasks 构造,
         带 page 字段 → 走 it.page。这里**不能**改成解析 it.url:任务表里的 url
         是用户粘贴的原始链接,常不带 ?p=,解析它会把 P3 的记录也算成 P1,
         后端就按 P1 拼文件名、按错的 page 判去重(缺陷 C2)。
       - 接口条目(收藏夹 / 稍后再看 / 观看历史 / 关注动态 / 追更合集):
         这些页的条目没有 page 字段 → 退到 pageFromUrl(it.url)。它们的 url 由
         bilibili_api 拼成 .../video/{bvid},从不带 ?p= → 得 1,与修复前逐位相同。
     取 page 的方式沿用 B3 的不对称原则:任务侧读存储字段,查询侧才解析 URL。 */
  let items = checked.map(cb => {
    const it = state.items[parseInt(cb.dataset.libidx)];
    return { url: it.url, title: it.title, page: it.page || pageFromUrl(it.url),
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
