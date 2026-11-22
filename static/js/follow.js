/* 卡片上的关注状态与关注/取关。

   为什么要批量、为什么要缓存:
   B 站 /x/relation 是逐个 UP 查的接口,一张列表十几张卡连打会把上游打成
   412 风控(实测触发过一次,冷却 10 分钟)。所以:
     · 状态一次性问后端(/api/relation/status?mids=...),后端自己复用
       关注列表的 10 分钟缓存做本地匹配,不额外发请求;
     · 前端按「这一批没查过的 mid」分页取,合并成一次;
     · 结果记在模块里,同一 UP 在所有页面/所有卡片共用一份状态。 */

import { postJSON } from './net.js';
import { toast } from './confirm.js';

const statusByMid = new Map();     // mid -> true / false
const pending = new Set();         // 已在途的 mid,防止重复请求
let flushTimer = null;
let queue = new Set();

function setChipState(chip, followed) {
  chip.dataset.followed = followed ? '1' : '0';
  chip.setAttribute('aria-pressed', followed ? 'true' : 'false');
  chip.textContent = followed ? '已关注' : '关注';
  chip.classList.toggle('on', followed);
}

/* 一次收集 mid → 关注胶囊列表。原先每个 mid 都单独做一次全文档
   querySelectorAll,一张列表 N 个 UP 就是 N 次全页扫描(2026-11-22)。 */
function collectChipsByMid() {
  const map = new Map();
  for (const chip of document.querySelectorAll('.vi-follow[data-mid]')) {
    const mid = String(chip.dataset.mid || '');
    if (!mid) continue;
    let list = map.get(mid);
    if (!list) { list = []; map.set(mid, list); }
    list.push(chip);
  }
  return map;
}

function applyToDom(mid, followed, chipMap = null) {
  const chips = chipMap
    ? (chipMap.get(mid) || [])
    : document.querySelectorAll(`.vi-follow[data-mid="${mid}"]`);
  chips.forEach((chip) => setChipState(chip, followed));
}

async function flush() {
  flushTimer = null;
  const mids = [...queue].filter((mid) => !statusByMid.has(mid) && !pending.has(mid));
  queue = new Set();
  if (!mids.length) return;
  for (const mid of mids) pending.add(mid);

  // 一批最多 200 个(后端也有同样的上限);一张列表页远达不到
  const chunks = [];
  for (let i = 0; i < mids.length; i += 100) chunks.push(mids.slice(i, i + 100));
  for (const chunk of chunks) {
    try {
      const response = await fetch(
        `/api/relation/status?mids=${encodeURIComponent(chunk.join(','))}`,
        { headers: { Accept: 'application/json' } });
      const payload = await response.json();
      const statuses = payload && payload.statuses ? payload.statuses : {};
      const chipMap = collectChipsByMid();
      for (const mid of chunk) {
        const followed = !!statuses[mid];
        statusByMid.set(mid, followed);
        applyToDom(mid, followed, chipMap);
      }
      // 未登录时后端 logged_in=false,按钮保持「关注」,点了会提示
      if (payload && payload.logged_in === false) {
        document.querySelectorAll('.vi-follow').forEach((chip) => {
          chip.dataset.loggedOut = '1';
        });
      }
    } catch (error) {
      /* 查不到就保持未知,不影响卡片其它信息 */
    } finally {
      for (const mid of chunk) pending.delete(mid);
    }
  }
}

/* 渲染完一页后调用:把这一页还没查过的 UP 主攒成一批去问。 */
export function refreshFollowChips() {
  const chipMap = collectChipsByMid();
  if (!chipMap.size) return;
  for (const [mid, chips] of chipMap) {
    if (statusByMid.has(mid)) {
      const followed = statusByMid.get(mid);
      for (const chip of chips) setChipState(chip, followed);
      continue;
    }
    queue.add(mid);
  }
  if (!queue.size || flushTimer) return;
  flushTimer = setTimeout(flush, 60);   // 同一批渲染合并成一次请求
}

async function toggleFollow(chip) {
  const mid = String(chip.dataset.mid || '');
  if (!mid) return;
  if (chip.dataset.loggedOut === '1') {
    toast('请先在「账号」里登录,才能关注 UP 主');
    return;
  }
  const current = chip.dataset.followed === '1';
  const next = !current;
  chip.disabled = true;
  try {
    // requestJSON 在非 2xx 时抛 HttpError,失败走下面的 catch
    await postJSON('/api/relation/modify',
      { mid: Number(mid), action: next ? 'follow' : 'unfollow' });
    statusByMid.set(mid, next);
    applyToDom(mid, next);
    toast(next ? `已关注 ${chip.dataset.name || ''}` : `已取消关注 ${chip.dataset.name || ''}`);
  } catch (error) {
    toast(error.message || '关注操作失败');
  } finally {
    chip.disabled = false;
  }
}

document.addEventListener('click', (event) => {
  const chip = event.target.closest('.vi-follow');
  if (!chip) return;
  event.preventDefault();
  event.stopPropagation();
  toggleFollow(chip);
});

/* 关注 UP 页(或任何别处)改了关注关系后调这里,把卡片上的状态一起对上,
   否则同一 UP 在视频卡上还显示旧的「已关注」。 */
export function syncFollowState(mid, followed) {
  const key = String(mid || '');
  if (!key) return;
  statusByMid.set(key, !!followed);
  applyToDom(key, !!followed);
}
