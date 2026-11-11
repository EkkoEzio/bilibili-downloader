/* 关注 UP(自动下载订阅):站内关注 × 本地自动下载清单的合并管理页。

   与「关注动态」分开的原因:动态流是浏览行为,本页是订阅管理行为
   (加 UP / 开自动下载 / 选清晰度 / 取关回关),混在一起时两个用途互相干扰。
   合集/收藏夹的自动下载开关在各自页面(追更合集、我的收藏)。 */
import { registerPage, state, libDom, cachedLoad, getLibSearchQuery,
         upCardChunk, dismissFresh, refreshLib, maybeFillScreen, replaceItems,
         cancelCachedLoads } from '../lib.js';
import { postJSON, postEmpty } from '../net.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, safeDecimalId, safeHttpUrl, nonNegativeInt,
         formatDay } from '../util.js';
import { QUALITY_OPTIONS, QUALITY_CARD_LABELS } from '../quality.js';
import { loadAutoRefreshStatus } from '../settings.js';
import { announce, withPending } from '../confirm.js';

function qualityOptionHtml(value, current) {
  const label = QUALITY_CARD_LABELS.get(value) ?? String(value);
  const selected = value === current ? ' selected' : '';
  return `<option value="${escapeAttr(value)}"${selected}>${escapeHtml(label)}</option>`;
}

let _mergedUps = [];
let _mergedTotal = 0;
let _mergedShown = 0;
/* 后端页数预算(10 页 × 50)耗尽仍有关注未拉到时置位,列表文案如实说明。 */
let _truncated = false;

function mergeUps(followItems, localUps) {
  const localByMid = new Map();
  for (const up of localUps) {
    const mid = safeDecimalId(up.mid);
    if (mid) localByMid.set(mid, up);
  }
  const followedMids = new Set();
  const merged = [];
  for (const it of Array.isArray(followItems) ? followItems : []) {
    const mid = safeDecimalId(it.mid);
    if (!mid || followedMids.has(mid)) continue;
    followedMids.add(mid);
    const local = localByMid.get(mid);
    merged.push({
      mid, uname: it.uname || '',
      face: safeHttpUrl(it.face, { httpsOnly: true }) || '',
      sign: String(it.sign || ''),
      followed: true, auto: !!local,
      quality: (local && local.quality) || '',
      // 站内关注用接口给的 mtime;只有手动加入、没有 mtime 时才回落到 added_at
      followedAt: nonNegativeInt(it.mtime) || (local ? nonNegativeInt(local.added_at) : 0),
      lastPubAt: local ? nonNegativeInt(local.last_pub_at) : 0,
    });
  }
  for (const [mid, up] of localByMid) {
    if (followedMids.has(mid)) continue;
    merged.push({
      mid, uname: up.uname || '',
      face: safeHttpUrl(up.face, { httpsOnly: true }) || '',
      sign: '',
      followed: false, auto: true, quality: up.quality || '',
      followedAt: nonNegativeInt(up.added_at),
      lastPubAt: nonNegativeInt(up.last_pub_at),
    });
  }
  // 排序规则(与追更合集一致):自动下载的整批在前,不下载的在后;
  // 同一组内按关注时间倒序,时间相同再按 mid 兜底,保证顺序稳定。
  return sortUps(merged);
}

/* 自动下载优先 → 组内按关注时间倒序。切换开关后也要重排,卡片才会立刻归位。
   mergeUps 里传的是还没赋给 _mergedUps 的新数组,所以显式接收目标。 */
function sortUps(list = _mergedUps) {
  list.sort((a, b) => (Number(!a.auto) - Number(!b.auto))
    || (b.followedAt - a.followedAt)
    || (Number(b.mid) - Number(a.mid)));
  return list;
}

let _forceOnce = false;

export async function loadSubscriptions(force = false) {
  loadAutoRefreshStatus();
  const forced = !!force || _forceOnce;
  _forceOnce = false;
  await cachedLoad({
    page: 'subs',
    api: forced ? '/api/followings?force=1' : '/api/followings',
    cacheKey: 'followings',
    // 用户主动刷新时直接应用新数据,不要只弹「获取到新内容」提示条
    fast: forced,
    // sig() 只认数组(非数组一律返回 "0",会让新旧数据永远判定相同、页面卡住);
    // 这里把关注与本地清单合成一个带业务标识的数组供比对。
    extract: (b) => [
      ...(b.items || []).map(it => ({ mid: `f:${it.mid}` })),
      ...(b.local_ups || []).map(up => ({ mid: `a:${up.mid}:${up.quality || ''}` })),
    ],
    stillValid: () => state.mode === 'subs',
    showLoading: () => {
      const d = libDom();
      d.meta.textContent = '';
      d.list.innerHTML = '<div class="empty"><span class="spinner"></span>加载关注与自动下载清单...</div>';
    },
    showError: (msg) => {
      libDom().list.innerHTML = `<div class="empty">${escapeHtml(msg || '加载失败')}<br><button class="ghost" onclick="loadSubscriptions()">${icon('refresh')} 重试</button></div>`;
    },
    apply: (b) => {
      _mergedUps = mergeUps(b.items || [], b.local_ups || []);
      _mergedTotal = Number(b.total) || 0;
      _truncated = !!b.truncated;
      _mergedShown = 0;
      renderUps();
      maybeFillScreen();
    },
  });
}

function patchUp(mid, patch) {
  const up = _mergedUps.find(u => u.mid === mid);
  if (up) Object.assign(up, patch);
}

/* 重排后保证被操作的那张卡仍在上屏切片内。
   _mergedShown 是「已展开多少张」,卡片改开关后会换组(自动下载组在前),
   位置可能从切片内掉到切片外——用户刚点完就看不见自己刚改的那张。 */
function keepVisible(mid) {
  const idx = _mergedUps.findIndex(u => u.mid === mid);
  if (idx >= 0 && _mergedShown < idx + 1) _mergedShown = idx + 1;
}

function upAutoControlHtml(u) {
  const mid = u.mid;
  const label = u.uname || ('UP ' + mid);
  // 两态都把「不下载」作为显式选项:用户能直接看到/选择"不自动下载"这个状态,
  // 不会把占位文字「自动下载」误读成"正在下载"。
  // 「不下载」固定在第一项,与追更合集/收藏夹卡片上的开关保持同一排序。
  if (u.auto) {
    const options = QUALITY_OPTIONS
      .map(([value]) => qualityOptionHtml(value, u.quality || '')).join('');
    return `<select class="uc-auto on" data-mid="${escapeAttr(mid)}"
      aria-label="${escapeAttr(label)}的自动下载清晰度"
      title="新投稿自动下载中;改选换清晰度,选「不下载」即停止"
      onchange="upAutoChanged(this)">
      <option value="__off">不下载</option>${options}</select>`;
  }
  const plainOptions = QUALITY_OPTIONS
    .map(([value]) => qualityOptionHtml(value, null)).join('');
  return `<select class="uc-auto" data-mid="${escapeAttr(mid)}"
    aria-label="${escapeAttr(label)}的自动下载清晰度"
    title="当前不自动下载;选清晰度即开启"
    onchange="upAutoChanged(this)">
    <option value="__off" selected>不下载</option>
    ${plainOptions}</select>`;
}

function upCardHtml(u) {
  const mid = u.mid;
  const name = escapeHtml(u.uname || ('UP ' + mid));
  const nameHtml = `<a href="https://space.bilibili.com/${mid}" target="_blank" rel="noopener">${name}</a>`;
  const followBtn = u.followed
    ? `<button class="ghost" onclick="upToggleFollow(this.dataset.mid, true, this)" data-mid="${escapeAttr(mid)}" title="取消关注该 UP">已关注</button>`
    : `<button class="primary" onclick="upToggleFollow(this.dataset.mid, false, this)" data-mid="${escapeAttr(mid)}" title="关注该 UP">关注</button>`;
  const sign = String(u.sign || '').trim();
  // 时间行:站内关注给「关注于」,手动加入给「加入于」;没扫过投稿就明说,
  // 不显示一个空白让用户以为是加载失败。
  const whenParts = [];
  if (u.followedAt) {
    whenParts.push(`${u.followed ? '关注于' : '加入于'} ${formatDay(u.followedAt)}`);
  }
  whenParts.push(u.lastPubAt ? `最近投稿 ${formatDay(u.lastPubAt)}` : '尚未检查投稿');
  const badge = u.followed
    ? '<span class="uc-badge on">已关注</span>'
    : '<span class="uc-badge">未关注</span>';
  return `
  <div class="up-card">
    <div class="uc-thumb">
      <div class="uc-cover">
        <img class="uc-face" src="${escapeAttr(u.face)}" alt="" onerror="this.style.visibility='hidden'">
        ${badge}
      </div>
      <div class="uc-actions">${followBtn}${upAutoControlHtml(u)}</div>
    </div>
    <div class="uc-text">
      <div class="uc-name" title="${escapeAttr(u.uname || '')}">${nameHtml}</div>
      <div class="uc-sign" title="${escapeAttr(sign)}">${sign ? escapeHtml(sign) : '暂无个性签名'}</div>
      <div class="uc-meta">${whenParts.map(part => `<span>${escapeHtml(part)}</span>`).join('')}</div>
    </div>
  </div>`;
}

function renderUps() {
  const d = libDom();
  const q = getLibSearchQuery();
  let ups = _mergedUps;
  if (q) ups = _mergedUps.filter(u =>
    (u.uname || '').toLowerCase().includes(q) || String(u.mid).includes(q));
  d.list.className = 'lib-list';
  if (ups.length === 0) {
    d.list.innerHTML = `<div class="empty">${q ? '没有匹配结果（试试其他关键词）'
      : '还没有关注的 UP。在上方粘贴 UP 主页链接添加自动下载，或到 B 站关注后刷新。'}</div>`;
  } else {
    if (!_mergedShown) _mergedShown = upCardChunk();
    const shown = q ? ups : ups.slice(0, _mergedShown);
    d.list.innerHTML = `<div class="${state.view === 'grid' ? 'up-grid' : 'up-list'}">`
      + shown.map(upCardHtml).join('') + '</div>';
  }
  if (d.meta) {
    const autoCount = _mergedUps.filter(u => u.auto).length;
    const totalText = `站内关注 ${_mergedTotal} 个 UP${_truncated ? '（关注太多，仅加载了前 500 个）' : ''}`;
    d.meta.textContent = q
      ? `${totalText} · 匹配 ${ups.length}`
      : `${totalText} · 自动下载 ${autoCount} 个`;
  }
}

function advanceUps() {
  if (getLibSearchQuery()) return false;
  if (_mergedShown >= _mergedUps.length) return false;
  _mergedShown += upCardChunk();
  renderUps();
  return true;
}

/* ---------- 卡片动作 ---------- */

export async function upAutoChanged(sel) {
  const mid = safeDecimalId(sel.dataset.mid);
  const value = String(sel.value || '');
  if (!mid || value === '__on') { renderUps(); return; }
  const up = _mergedUps.find(u => u.mid === mid);
  const wasAuto = !!(up && up.auto);
  if (value === '__off' && !wasAuto) { renderUps(); return; }  // 本来就不下载
  sel.disabled = true;
  try {
    if (value === '__off') {
      const r = await postJSON('/api/up-list/remove', { mid: parseInt(mid, 10) });
      if (!r.ok) throw new Error(r.error || '移除失败');
      if (up && !up.followed) {
        // 这条记录只存在于本地自动下载清单:停掉后既没被站内关注、也不再受管,
        // 留在列表里就是一张「未关注 / 不下载」的幽灵卡,刷新前一直点得动。
        _mergedUps = _mergedUps.filter(u => u.mid !== mid);
      } else {
        patchUp(mid, { auto: false, quality: '' });
        sortUps();  // 关掉自动下载后应立刻落到「不下载」那一组
        keepVisible(mid);
      }
      announce('已停止自动下载');
    } else if (!wasAuto) {
      const r = await postJSON('/api/up-list/add', { input: mid });
      if (!r.ok) throw new Error(r.error || '添加失败');
      // 订阅已经成功:即使设置清晰度失败也要如实反映"已开启",否则界面显示
      // 「不下载」而实际已订阅,状态与后端不一致。
      patchUp(mid, { auto: true, quality: value });
      sortUps();  // 开启自动下载后应立刻升到「自动下载」那一组
      keepVisible(mid);
      if (value) {
        const q = await postJSON('/api/up-list/set-quality',
                                 { mid: parseInt(mid, 10), quality: value });
        if (!q.ok) {
          // 后端拒绝了该档位(归一成最高画质),本地不能继续显示用户选的值:
          // 否则提示说「当前为最高画质」,下拉却停在用户选的那档。
          patchUp(mid, { quality: '' });
          announce('已开启自动下载，但清晰度设置失败（当前为最高画质）', true);
          renderUps();
          return;
        }
      }
      announce('已开启自动下载');
    } else {
      const r = await postJSON('/api/up-list/set-quality',
                               { mid: parseInt(mid, 10), quality: value });
      if (!r.ok) throw new Error(r.error || '设置失败');
      patchUp(mid, { quality: value });
      announce('已更新自动下载清晰度');
    }
  } catch (error) {
    announce(error.message || '操作失败', true);
    alert(error.message || '操作失败');
  } finally {
    sel.disabled = false;
    renderUps();
  }
}

export async function upToggleFollow(mid, followedNow, button = null) {
  const normalizedMid = safeDecimalId(mid);
  if (!normalizedMid) { alert('UP ID 无效'); return; }
  const action = followedNow ? 'unfollow' : 'follow';
  // 不弹确认:取关后卡片立即翻转为「关注」,反悔成本为零。
  try {
    await withPending(button, '处理中…', async () => {
      const r = await postJSON('/api/relation/modify', { mid: normalizedMid, action });
      if (!r.ok) throw new Error(r.error || '操作失败');
    });
    patchUp(normalizedMid, { followed: !followedNow });
    renderUps();
    announce(followedNow ? '已取消关注' : '已关注');
  } catch (error) {
    announce(error.message || '操作失败', true);
    alert(error.message || '操作失败');
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
      // 强制刷新:本地清单变化后磁盘缓存里的列表已过期,不绕缓存看不到新卡片
      if (state.mode === 'subs') loadSubscriptions(true);
    });
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

export function refreshSubs() {
  return loadSubscriptions(true);
}

function deactivateSubs() {
  cancelCachedLoads('subs');
}

registerPage('subs', {
  load: loadSubscriptions,
  retry: loadSubscriptions,
  render: () => { renderUps(); maybeFillScreen(); },
  infinite: 'own',
  advanceChunk: advanceUps,
  chunk: () => upCardChunk(),
  actionBarVisible: () => false,
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '还没有订阅任何 UP',
  onViewToggle: () => { _mergedShown = 0; },
  deactivate: deactivateSubs,
});
