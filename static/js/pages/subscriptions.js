/* 关注 UP(自动下载订阅):站内关注 × 本地自动下载清单的合并管理页。

   与「关注动态」分开的原因:动态流是浏览行为,本页是订阅管理行为
   (加 UP / 开自动下载 / 选清晰度 / 取关回关),混在一起时两个用途互相干扰。
   合集/收藏夹的自动下载开关在各自页面(追更合集、我的收藏)。 */
import { registerPage, state, libDom, cachedLoad, getLibSearchQuery,
         upCardChunk, dismissFresh, refreshLib, maybeFillScreen, replaceItems,
         cancelCachedLoads, renderFilterQuery } from '../lib.js';
import { postJSON, postEmpty } from '../net.js';
import { emptyStateHtml, loadingStateHtml } from '../lib.js';
import { icon } from '../icons.js';
import { escapeHtml, escapeAttr, safeDecimalId, safeHttpUrl, nonNegativeInt,
         formatDay } from '../util.js';
import { loadAutoRefreshStatus } from '../settings.js';
import { announce, toast, withPending } from '../confirm.js';
import { syncFollowState } from '../follow.js';
import { registerSearchTarget, isSearchMode } from '../search.js';

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
      d.list.innerHTML = loadingStateHtml('加载关注与自动下载清单…');
    },
    showError: (msg) => {
      libDom().list.innerHTML = emptyStateHtml(escapeHtml(msg || '加载失败'), { retry: 'loadSubscriptions()' });
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

/* ---------- UP 签名悬停气泡 ---------- */
/* 签名按 1 行截断;原生 title 提示慢且常被忽略,改成即时气泡:渲染后
   markTruncatedSigns() 只给真被截断的签名写 data-full,悬停时把全文放进
   挂在 body 上的固定定位气泡(卡片 overflow:hidden 会裁掉卡内气泡)。 */
function markTruncatedSigns() {
  document.querySelectorAll('.uc-sign').forEach((el) => {
    const clipped = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
    if (clipped) el.dataset.full = el.textContent; else delete el.dataset.full;
  });
}

let _signTipEl = null;
let _signTipFor = null;
function signTipEl() {
  if (!_signTipEl) {
    _signTipEl = document.createElement('div');
    _signTipEl.className = 'sign-tip';
    document.body.appendChild(_signTipEl);
  }
  return _signTipEl;
}

document.addEventListener('mouseover', (event) => {
  const sign = event.target.closest('.uc-sign[data-full]');
  if (!sign || _signTipFor === sign) return;
  _signTipFor = sign;
  const tip = signTipEl();
  tip.textContent = sign.dataset.full;
  tip.classList.add('show');
  const rect = sign.getBoundingClientRect();
  const maxWidth = Math.min(420, window.innerWidth - rect.left - 16);
  tip.style.left = Math.max(8, rect.left) + 'px';
  tip.style.maxWidth = Math.max(200, maxWidth) + 'px';
  const tipH = tip.offsetHeight;
  const fitsBelow = rect.bottom + 6 + tipH <= window.innerHeight - 8;
  tip.style.top = (fitsBelow ? rect.bottom + 6 : Math.max(8, rect.top - tipH - 6)) + 'px';
});

document.addEventListener('mouseout', (event) => {
  const sign = event.target.closest('.uc-sign[data-full]');
  if (!sign || _signTipFor !== sign) return;
  const to = event.relatedTarget;
  if (to && to.closest && to.closest('.uc-sign') === sign) return;
  signTipEl().classList.remove('show');
  _signTipFor = null;
});

/* 自动下载档位:纯分辨率(""=最高)+ 仅音频。2026-11-17 用户定稿:去掉 60 帧独立
   档,只选分辨率(最高/4K/2K/1080/720);同分辨率默认最高帧率(144→60→30),
   没有所选分辨率时自动降档到所选以下最高——两者都由后端
   quality.resolve_quality_label 在挑流时落实。仅音频保留(挑流走音频专用路径)。
   档位集合与 up_list.QUALITY_CHOICES 一致(测试绑定两侧)。
   「不下载」文案为「不自动下载」(2026-11-17 用户改名)。 */
const UP_QUALITY_SEGMENTS = [
  ["__off", "不自动下载"], ["", "最高"], ["2160P", "4K"], ["2K", "2K"], ["1080", "1080"], ["720", "720"], ["音频", "仅音频"],
];
/* 存量旧档位(60 帧独立档)归一到新分辨率档,与后端 _norm_quality 同一套映射 */
const _LEGACY_QUALITY = { "1080P60": "1080", "1080P": "1080", "720P60": "720", "720P": "720" };
function _normQuality(q) {
  q = String(q || "");
  return UP_QUALITY_SEGMENTS.some(([v]) => v === q) ? q : (_LEGACY_QUALITY[q] ?? "");
}

function upAutoControlHtml(u) {
  const mid = u.mid;
  const label = u.uname || ('UP ' + mid);
  // 「不下载 + 各分辨率」平铺成同一排分段胶囊,点哪个切哪个,当前档高亮。
  // 「不下载」固定第一项;「最高画质」已更名「最高」。
  const segments = UP_QUALITY_SEGMENTS.map(([value, text]) => {
    const on = value === '__off' ? !u.auto : (u.auto && _normQuality(u.quality) === value);
    const title = value === '__off'
      ? (u.auto ? '停止自动下载' : '当前不自动下载')
      : (value === '' ? '新投稿自动下载' : `${text}新投稿自动下载`);
    return `<button type="button" class="uc-auto-pill${on ? ' on' : ''}${value === '__off' ? ' off' : ''}"`
      + ` data-mid="${escapeAttr(mid)}" data-value="${escapeAttr(value)}" title="${escapeAttr(title)}"`
      + ` aria-pressed="${on ? 'true' : 'false'}" onclick="upAutoPick(this)">${escapeHtml(text)}</button>`;
  }).join('');
  return `<div class="uc-auto-row" role="group" aria-label="${escapeAttr(label)}的自动下载档位">${segments}</div>`;
}

function upCardHtml(u) {
  const mid = u.mid;
  const name = escapeHtml(u.uname || ('UP ' + mid));
  const nameHtml = `<a href="https://space.bilibili.com/${mid}" target="_blank" rel="noopener">${name}</a>`;
  // 关注胶囊挪到 UP 名后面,与视频卡元信息行的关注胶囊同一枚样式(类名用 uc-follow:
  // follow.js 以 .vi-follow 全局接管视频卡的关注状态,UP 卡有自己的换组/幽灵卡
  // 语义走 upToggleFollow,不能被它扫到,所以复用样式、类名分开)。
  const followChip = u.followed
    ? `<button class="uc-follow on" type="button" data-mid="${escapeAttr(mid)}" aria-pressed="true" title="取消关注该 UP" onclick="upToggleFollow(this.dataset.mid, true, this)">已关注</button>`
    : `<button class="uc-follow" type="button" data-mid="${escapeAttr(mid)}" aria-pressed="false" title="关注该 UP" onclick="upToggleFollow(this.dataset.mid, false, this)">关注</button>`;
  const sign = String(u.sign || '').trim();
  // 时间行:站内关注给「关注于」,手动加入给「加入于」;没扫过投稿就明说,
  // 不显示一个空白让用户以为是加载失败。
  const whenParts = [];
  if (u.followedAt) {
    whenParts.push(`${u.followed ? '关注于' : '加入于'} ${formatDay(u.followedAt)}`);
  }
  whenParts.push(u.lastPubAt ? `最近投稿 ${formatDay(u.lastPubAt)}` : '尚未检查投稿');
  return `
  <div class="up-card">
    <div class="uc-thumb">
      <div class="uc-cover">
        <img class="uc-face" src="${escapeAttr(u.face)}" alt="" onerror="this.style.visibility='hidden'">
      </div>
      <div class="uc-actions">${upAutoControlHtml(u)}</div>
    </div>
    <div class="uc-text">
      <div class="uc-name" title="${escapeAttr(u.uname || '')}">${nameHtml}${followChip}</div>
      <div class="uc-sign">${sign ? escapeHtml(sign) : '暂无个性签名'}</div>
      <div class="uc-meta">${whenParts.map(part => `<span>${escapeHtml(part)}</span>`).join('')}</div>
    </div>
  </div>`;
}

function renderUps() {
  const d = libDom();
  // 搜索态用有效过滤词:站内搜到的 UP 由 B 站匹配返回,名字未必包含
  // 搜索原文,再用原文 includes 会把结果筛空(与首页同源问题)。
  const q = renderFilterQuery();
  let ups = _mergedUps;
  if (q) ups = _mergedUps.filter(u =>
    (u.uname || '').toLowerCase().includes(q) || String(u.mid).includes(q));
  d.list.className = 'lib-list';
  if (ups.length === 0) {
    d.list.innerHTML = emptyStateHtml(
      q || isSearchMode('subs') ? '没有匹配结果（试试其他关键词）'
        : '还没有关注的 UP。在上方粘贴 UP 主页链接添加自动下载，或到 B 站关注后刷新。');
  } else {
    if (!_mergedShown) _mergedShown = upCardChunk();
    const shown = q ? ups : ups.slice(0, _mergedShown);
    d.list.innerHTML = `<div class="${state.view === 'grid' ? 'up-grid' : 'up-list'}">`
      + shown.map(upCardHtml).join('') + '</div>';
  }
  markTruncatedSigns();
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

export async function upAutoPick(btn) {
  const mid = safeDecimalId(btn.dataset.mid);
  const value = String(btn.dataset.value || '');
  if (!mid) { renderUps(); return; }
  const up = _mergedUps.find(u => u.mid === mid);
  const wasAuto = !!(up && up.auto);
  const turnOff = value === '__off';
  // 点的就是当前档:不重复请求
  if (turnOff === !wasAuto) { return; }
  if (!turnOff && wasAuto && (up.quality || '') === value) { return; }
  btn.disabled = true;
  try {
    if (turnOff) {
      const r = await postJSON('/api/up-list/remove', { mid: parseInt(mid, 10) });
      if (!r.ok) throw new Error(r.error || '移除失败');
      // 位置不跳、卡片不立刻消失(2026-11-17 用户要求):切换即时生效但不重排;
      // 未关注的 UP 停掉自动下载后,本地清单已移除,刷新后卡片自然消失。
      patchUp(mid, { auto: false, quality: '' });
      keepVisible(mid);
      announce('已停止自动下载');
    } else if (!wasAuto) {
      const r = await postJSON('/api/up-list/add', { input: mid });
      if (!r.ok) throw new Error(r.error || '添加失败');
      patchUp(mid, { auto: true, quality: value });
      keepVisible(mid);
      if (value) {
        const q = await postJSON('/api/up-list/set-quality',
                                 { mid: parseInt(mid, 10), quality: value });
        if (!q.ok) {
          // 后端拒绝了该档位(归一成最高画质),本地不能继续显示用户选的档:
          // 否则提示说「当前为最高画质」,分段却停在用户选的那档。
          patchUp(mid, { quality: '' });
          announce('已开启自动下载，但清晰度设置失败（当前为最高）', true);
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
    toast(error.message || '操作失败');
  } finally {
    btn.disabled = false;
    renderUps();
  }
}

export async function upToggleFollow(mid, followedNow, button = null) {
  const normalizedMid = safeDecimalId(mid);
  if (!normalizedMid) { toast('UP ID 无效'); return; }
  const action = followedNow ? 'unfollow' : 'follow';
  // 不弹确认:取关后卡片立即翻转为「关注」,反悔成本为零。
  try {
    await withPending(button, '处理中…', async () => {
      const r = await postJSON('/api/relation/modify', { mid: normalizedMid, action });
      if (!r.ok) throw new Error(r.error || '操作失败');
    });
    patchUp(normalizedMid, { followed: !followedNow });
    renderUps();
    // 视频卡上的关注胶囊与这里共用一份状态,改完要同步过去
    syncFollowState(normalizedMid, !followedNow);
    announce(followedNow ? '已取消关注' : '已关注');
  } catch (error) {
    announce(error.message || '操作失败', true);
    toast(error.message || '操作失败');
  }
}

export async function upAdd() {
  const input = document.getElementById('up-add-input');
  const button = document.getElementById('up-add-btn');
  const value = input.value.trim();
  if (!value) { toast('请粘贴 UP 主页链接或 mid'); return; }
  try {
    await withPending(button, '添加中…', async () => {
      const r = await postJSON('/api/up-list/add', { input: value });
      if (!r.ok) throw new Error(r.error || '添加失败');
      input.value = '';
      announce(`已加入自动下载清单：${r.up.uname || ('UP ' + r.up.mid)}`);
      // 强制刷新:本地清单变化后磁盘缓存里的列表已过期,不绕缓存看不到新卡片
      if (state.mode === 'subs') loadSubscriptions(true);
    });
  } catch (error) { toast(error.message); }
}

export async function upCheckNow(btn) {
  try {
    await withPending(btn, '检查中…', async () => {
      const r = await postEmpty('/api/up-list/check');
      const message = r && r.started === false
        ? '已有检查在进行中，本次未启动新一轮。稍后队列会自动带回新任务。'
        : '已在后台开始检查，新视频会自动进入下载队列。';
      announce(message);
      toast(message);
    });
    window.setTimeout(loadAutoRefreshStatus, 300);
  } catch (error) {
    announce(error.message || '检查启动失败', true);
    toast(error.message);
  }
}

export function refreshSubs() {
  return loadSubscriptions(true);
}

function deactivateSubs() {
  cancelCachedLoads('subs');
}

/* 搜索栏:回车搜全站 UP 主,点卡片上的「关注」即可直接关注(走同一套写接口)。 */
registerSearchTarget('subs', {
  kind: 'user',
  apply: (users, q) => {
    _mergedUps = users.map(u => ({ ...u, auto: false, local: false }));
    _mergedTotal = users.length;
    _truncated = false;
    _mergedShown = users.length;
    renderUps();
    const d = libDom();
    if (d.meta) d.meta.textContent = `搜索「${q}」· 命中 ${users.length} 个 UP 主`;
  },
  exit: () => loadSubscriptions(true),
});

registerPage('subs', {
  load: loadSubscriptions,
  retry: loadSubscriptions,
  render: () => { renderUps(); maybeFillScreen(); },
  infinite: 'own',
  advanceChunk: advanceUps,
  chunk: () => upCardChunk(),
  actionBarVisible: () => false,
  emptyHint: (q) => q ? '没有匹配结果（试试其他关键词）' : '还没有订阅任何 UP',
  // 搜索态切视图时结果集是全量命中,直接整集展示;常态才走分块推进。
  onViewToggle: () => { _mergedShown = isSearchMode('subs') ? _mergedUps.length : 0; },
  deactivate: deactivateSubs,
});
