"""自动下载:定时串行检查 UP 清单,新投稿直接入队下载。

清单语义(2026-08-18 改版):加入清单 = 自动下载该 UP 的新投稿,
任务直接建在下载队列里;每个 UP 可配默认清晰度(up_list.quality)。

风控与合规边界(2026-08-15 法律评估结论,自动下载后依然遵守):
- 自动任务属于 B 站用户协议"自动程序"条款场景,是本工具里风险最高的一块,
  因此间隔刻意保守:UP 之间 >=5 秒,轮询间隔下限 10 分钟
  (config.AUTO_REFRESH_MINUTES,UI 不可调破)。
- 只下载自己账号有权观看的免费内容;限流返回空时不动 last_seen(不丢数据)。
- 下载内容仅个人存档,不分发。
"""
import logging
import threading
import time
from typing import Optional

import bilibili_api as bapi
import config
import downloader
import up_list
from extras import page_from_url

log = logging.getLogger("bd.auto_refresh")

UP_INTERVAL = 5.0  # UP 之间的请求间隔(秒),防账号级风控

# 增量拉取参数:B 站 arc/search 不支持按时间过滤,只能按 pubdate 倒序分页。
# 依据「上次拉取时间」自适应页大小:刚拉过(24h 内)只拉小页,降低单次响应量;
# 一页全是新视频(爆发更新)时翻页兜底,保证不漏。
FULL_PS = 15
RECENT_PS = 5
RECENT_WINDOW = 24 * 3600
MAX_PAGES = 3

# 自动任务失败后不依赖时间游标重新发现，而从持久化任务史退避重试。
AUTO_RETRY_BASE = 10 * 60
AUTO_RETRY_MAX = 24 * 3600
# 放弃阈值:达到后不再自动重试(用户仍可手动下载)。退避到第 8 次约 21 小时,
# 再往上对删稿/充电专属/地区限制这类永久失败只是无限消耗任务史容量,
# 累计到 MAX_TOTAL_TASKS 后连手动下载都会被容量检查挡住。
AUTO_RETRY_MAX_ATTEMPTS = 8
# 与 up_list 持久化边界共享同一来源；达到上限必须显式失败，不能裁掉旧页后
# 继续推进游标，否则会造成永久漏稿。
MAX_SCAN_ITEMS = up_list.MAX_SCAN_ITEMS


class ScanCapacityError(RuntimeError):
    """单个 UP 的续扫累计项超过安全边界。"""

    def __init__(self, current: int) -> None:
        self.current = int(current)
        self.limit = int(MAX_SCAN_ITEMS)
        super().__init__(
            f"续扫状态含 {self.current} 项，超过安全上限 {self.limit}；"
            "请重置该 UP 的已见记录后分轮重新扫描"
        )


# 保证「同一时刻只有一轮检查」。非阻塞获取:拿不到就跳过,不排队。
# 阻塞获取(`with _lock:`)只保证互斥,不保证去重 —— 后来者会卡在锁上,等前一轮
# 释放后完整补跑一轮(每个 UP 一次 arc/search、UP 之间 sleep 5 秒)。连点 N 次
# 「立即检查新视频」就是串行跑 N 轮完整检查,正是 docs/COMPLIANCE.md 承诺要
# 避免的账号级风控场景。需求 2.9 要的是「跳过」,不是「排队后补跑」。
_lock = threading.Lock()


class _RefreshReservation:
    """一次性业务轮次预约；可由 HTTP 线程安全转交给 worker。"""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._claimed = False
        self._released = False

    def claim(self) -> None:
        """声明由当前 worker 消费；重复消费属于编程错误。"""
        with self._state_lock:
            if self._released:
                raise RuntimeError("追更预约已释放")
            if self._claimed:
                raise RuntimeError("追更预约已被消费")
            self._claimed = True

    def release(self) -> bool:
        """幂等释放业务锁；线程启动失败时可由预约方回收。"""
        with self._state_lock:
            if self._released:
                return False
            self._released = True
        _lock.release()
        return True


def try_reserve_refresh() -> Optional[_RefreshReservation]:
    """原子预约一轮追更；忙时返回 ``None``，绝不排队。"""
    if not _lock.acquire(blocking=False):
        return None
    try:
        return _RefreshReservation()
    except BaseException:
        _lock.release()
        raise


# 生命周期状态与业务互斥锁分离：读取状态永不等待一轮慢速上游检查。
_status_lock = threading.Lock()
_scheduler_lock = threading.Lock()
_scheduler_thread: Optional[threading.Thread] = None
_cycle_metrics = threading.local()
_status = {
    "last_trigger": None,
    "last_started_at": None,
    "last_finished_at": None,
    "last_success_at": None,
    "last_error": None,
    "last_enqueued": 0,
    "last_partial_errors": 0,
    "next_run_at": None,
}


def estimate_ps(fetched_at, now=None) -> int:
    """自适应页大小:24h 内拉过 → RECENT_PS;从未拉过/隔久了 → FULL_PS。"""
    now = now if now is not None else time.time()
    if not fetched_at:
        return FULL_PS
    return RECENT_PS if now - fetched_at <= RECENT_WINDOW else FULL_PS


def _as_ts(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _merge_page(collected: list[dict], page: list[dict], *, prepend: bool = False) -> None:
    """按 BVID 去重合并页面；重取首页时保持当前首页顺序在最前。"""
    source = [*page, *collected] if prepend else [*collected, *page]
    merged = []
    seen = set()
    for raw in source:
        if not isinstance(raw, dict):
            continue
        bvid = str(raw.get("bvid") or "")
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        merged.append(dict(raw))
    collected[:] = merged


def _page_finishes_scan(page: list[dict], ps: int, seen_ts: int) -> bool:
    """短页是真实末尾；只有严格早于旧秒边界才算完整跨过同秒带。"""
    if len(page) < ps:
        return True
    return bool(seen_ts and _as_ts(page[-1].get("view_at")) < seen_ts)


def fetch_new(mid: int, last: Optional[dict] = None):
    """增量拉取单个 UP，返回 ``(新增列表, 本轮头部或 None)``。

    公开二元返回保持兼容；头部字典的私有 ``_scan_complete/_scan_state`` 供
    refresh_once 区分“完整覆盖旧游标”和“只耗尽本轮页预算”。续扫固定重取
    首页；deep 前沿继续寻找旧游标/末页，head 前沿则在首页漂移后重新证明
    ``[1, deep 接缝)`` 的连续覆盖。deep 提前见到终止证据时只持久化
    ``terminal_pn``，必须等低页接缝闭合后才提交游标。每轮仍不超过 MAX_PAGES。
    """
    last = last or {}
    seen_ts = _as_ts(last.get("ts"))
    seen_bvid = str(last.get("bvid") or "")
    seen_same = {str(v) for v in (last.get("same_ts_bvids") or []) if v}
    if seen_bvid:
        seen_same.add(seen_bvid)

    raw_scan = last.get("scan")
    scan = raw_scan if isinstance(raw_scan, dict) else None
    if scan and (_as_ts(scan.get("base_ts")) != seen_ts
                 or str(scan.get("base_bvid") or "") != seen_bvid):
        # 游标被重置/人工改动后，旧 continuation 已失效。
        scan = None

    if scan:
        try:
            ps = max(1, min(50, int(scan.get("ps") or FULL_PS)))
        except (TypeError, ValueError):
            ps = FULL_PS
        raw_items = scan.get("items") or []
        if not isinstance(raw_items, list):
            raise ScanCapacityError(MAX_SCAN_ITEMS + 1)
        if len(raw_items) > MAX_SCAN_ITEMS:
            raise ScanCapacityError(len(raw_items))
        collected = [dict(v) for v in raw_items
                     if isinstance(v, dict) and v.get("bvid")]
    else:
        ps = estimate_ps(last.get("fetched_at"))
        collected = []

    def _merge_scanned(page: list[dict], *, prepend: bool = False) -> None:
        _merge_page(collected, page, prepend=prepend)
        if len(collected) > MAX_SCAN_ITEMS:
            raise ScanCapacityError(len(collected))

    def _pn(value, default: int) -> int:
        try:
            return max(2, int(value or default))
        except (TypeError, ValueError):
            return default

    def _optional_pn(value) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return max(2, int(value))
        except (TypeError, ValueError):
            return None

    complete = False
    pages_used = 0
    # deep_next_pn 是旧游标方向的单调前沿；next_pn 保留给旧数据兼容。
    deep_next_pn = _pn(
        (scan or {}).get("deep_next_pn") or (scan or {}).get("next_pn"), 2
    ) if scan else 1

    if scan and "head_repair_until_pn" in scan:
        head_next_pn = _pn(scan.get("head_next_pn"), 2)
        head_repair_until_pn = _pn(
            scan.get("head_repair_until_pn"), deep_next_pn)
        terminal_pn = _optional_pn(scan.get("terminal_pn"))
    elif scan:
        # 旧 schema 无法证明曾修复过哪些低页：从第 2 页保守重证，
        # 并把旧 deep 前沿冻结为接缝目标，不能沿用可能已跳过的 head_next_pn。
        head_next_pn = 2
        head_repair_until_pn = deep_next_pn
        terminal_pn = None
    else:
        head_next_pn = 2
        head_repair_until_pn = 2
        terminal_pn = None

    def _repair_closed() -> bool:
        return head_next_pn >= head_repair_until_pn

    observed_head = None
    head_changed = False

    if scan:
        first = bapi.fetch_up_videos(mid, ps=ps, pn=1)
        pages_used = 1
        if not first:
            complete = True
        else:
            observed_head = first[0] if isinstance(first[0], dict) else None
            old_head = (str(scan.get("head_bvid") or ""),
                        _as_ts(scan.get("head_ts")))
            new_head = (str((observed_head or {}).get("bvid") or ""),
                        _as_ts((observed_head or {}).get("view_at")))
            head_changed = new_head != old_head
            if head_changed:
                # 冻结漂移发生前的 deep 接缝。后续 deep 可以继续推进，但只有
                # head 从第 2 页连续追到该接缝，才可使用终止证据提交游标。
                head_next_pn = 2
                head_repair_until_pn = deep_next_pn
                terminal_pn = None
            _merge_scanned(first, prepend=True)
            if _page_finishes_scan(first, ps, seen_ts):
                complete = True
            elif terminal_pn is not None and _repair_closed():
                # 首页锚点稳定，上一轮保存的终止证据仍属于同一 offset。
                complete = True

        if not complete:
            remaining = MAX_PAGES - pages_used
            plan: list[list] = []

            def _schedule(pn: int, role: str) -> None:
                for item in plan:
                    if item[0] == pn:
                        item[1].add(role)
                        return
                if len(plan) < remaining:
                    plan.append([pn, {role}])

            if not _repair_closed():
                if terminal_pn is not None:
                    # deep 已找到末页/旧游标，只需把剩余预算用于闭合低页空洞。
                    candidate = head_next_pn
                    while (len(plan) < remaining
                           and candidate < head_repair_until_pn):
                        _schedule(candidate, "head")
                        candidate += 1
                else:
                    # 一个槽修复低页，一个槽推进旧 continuation；同页时合并角色，
                    # 把空出的槽继续给 deep，保证两个前沿都有实际进展。
                    _schedule(head_next_pn, "head")
                    _schedule(deep_next_pn, "deep")
                    candidate = deep_next_pn + 1
                    while len(plan) < remaining:
                        _schedule(candidate, "deep")
                        candidate += 1
            else:
                candidate = deep_next_pn
                while len(plan) < remaining:
                    _schedule(candidate, "deep")
                    candidate += 1

            for requested_pn, roles in plan:
                page = bapi.fetch_up_videos(mid, ps=ps, pn=requested_pn)
                pages_used += 1
                if "head" in roles:
                    head_next_pn = max(head_next_pn, requested_pn + 1)
                if "deep" in roles:
                    deep_next_pn = max(deep_next_pn, requested_pn + 1)

                finishes = not page
                if page:
                    _merge_scanned(page)
                    finishes = _page_finishes_scan(page, ps, seen_ts)
                if finishes:
                    terminal_pn = requested_pn
                    if "head" in roles:
                        # 终止页本身位于连续 head 前沿；其后的旧接缝无需再证明。
                        head_repair_until_pn = min(
                            head_repair_until_pn, head_next_pn)

                if terminal_pn is not None and _repair_closed():
                    complete = True
                    break
    else:
        # 首轮仍按 1,2,3 顺序扫描；耗尽预算后从下一页建立 deep continuation。
        while not complete and pages_used < MAX_PAGES:
            page = bapi.fetch_up_videos(mid, ps=ps, pn=deep_next_pn)
            pages_used += 1
            requested_pn = deep_next_pn
            deep_next_pn = requested_pn + 1
            if not page:
                complete = True
                break
            if observed_head is None:
                observed_head = page[0] if isinstance(page[0], dict) else None
            _merge_scanned(page)
            if _page_finishes_scan(page, ps, seen_ts):
                complete = True

        if not complete:
            # 本轮从第 1 页连续读到 deep 前沿，初始接缝已经有完整证明。
            head_next_pn = deep_next_pn
            head_repair_until_pn = deep_next_pn

    newest = dict(collected[0]) if collected else None
    new = []
    for video in collected:
        bvid = str(video.get("bvid") or "")
        ts = _as_ts(video.get("view_at"))
        if ts > seen_ts or (ts == seen_ts and bvid not in seen_same):
            new.append(video)

    if newest is not None:
        newest["_scan_complete"] = complete
        if not complete:
            head = observed_head or collected[0]
            newest["_scan_state"] = {
                "base_bvid": seen_bvid,
                "base_ts": seen_ts,
                "ps": ps,
                # 旧版本只认识 next_pn；让它继续表示深页前沿。
                "next_pn": deep_next_pn,
                "deep_next_pn": deep_next_pn,
                "head_next_pn": head_next_pn,
                "head_repair_until_pn": head_repair_until_pn,
                "terminal_pn": terminal_pn,
                "head_bvid": str(head.get("bvid") or ""),
                "head_ts": _as_ts(head.get("view_at")),
                "items": collected,
                "updated_at": int(time.time()),
            }
    return new, newest


def _retry_items(mid: int, now: Optional[int] = None,
                 tasks_snapshot: Optional[list[dict]] = None) -> list[dict]:
    """从任务史恢复该 UP 最新一次失败/中断的自动任务，带指数退避。

    调用方可传一轮共享的任务快照，避免按 UP 数重复完整扫描任务史。
    """
    now = int(time.time()) if now is None else int(now)
    tasks = downloader.list_tasks() if tasks_snapshot is None else tasks_snapshot
    latest: dict[tuple[str, int], dict] = {}
    for task in tasks:
        if task.get("source") != "auto_refresh":
            continue
        try:
            task_mid = int(task.get("uploader_mid") or 0)
            page = int(task.get("page") or 1)
        except (TypeError, ValueError):
            continue
        if task_mid != int(mid):
            continue
        url = str(task.get("url") or "")
        if not url:
            continue
        key = (url, page)
        old = latest.get(key)
        if old is None or _as_ts(task.get("created_at")) >= _as_ts(old.get("created_at")):
            latest[key] = task

    retries = []
    for task in latest.values():
        if task.get("status") not in ("failed", "interrupted"):
            continue
        attempt = _as_ts(task.get("auto_attempt"))
        if attempt >= AUTO_RETRY_MAX_ATTEMPTS:
            continue
        delay = min(AUTO_RETRY_MAX, AUTO_RETRY_BASE * (2 ** min(attempt, 7)))
        failed_at = _as_ts(task.get("finished_at") or task.get("created_at"))
        if failed_at and now - failed_at < delay:
            continue
        url = str(task.get("url") or "")
        if downloader.is_downloaded(url):
            continue
        retries.append({
            "bvid": task.get("bvid") or downloader.bvid_from_url(url) or "",
            "title": task.get("title") or "(解析中)",
            "url": url,
            "page": int(task.get("page") or 1),
            "thumbnail": task.get("thumbnail") or "",
            "uploader": task.get("uploader") or "",
            "uploader_mid": int(mid),
            "duration": task.get("duration") or 0,
            "source": "auto_refresh",
            "auto_attempt": attempt + 1,
        })
    return retries


def _commit_complete_cursor(mid: int, newest: dict, new: list[dict]) -> None:
    """提交头部秒边界，并把该秒本轮发现的所有 BVID 合并进复合游标。"""
    fetched_at = int(time.time())
    head_ts = _as_ts(newest.get("view_at"))
    head_bvid = str(newest.get("bvid") or "")
    up_list.mark_seen(mid, head_bvid, head_ts, newest.get("title", ""),
                      fetched_at=fetched_at)
    committed = {head_bvid}
    for video in new:
        bvid = str(video.get("bvid") or "")
        if not bvid or bvid in committed or _as_ts(video.get("view_at")) != head_ts:
            continue
        committed.add(bvid)
        up_list.mark_seen(mid, bvid, head_ts, video.get("title", ""),
                          fetched_at=fetched_at)


def is_running() -> bool:
    """是否已有一轮检查在跑。路由据此如实回报「本次未启动新一轮」(需求 2.10)。"""
    return _lock.locked()


def _update_status(**changes) -> None:
    with _status_lock:
        _status.update(changes)


def get_status() -> dict:
    """返回不含账号或投稿内容的只读生命周期快照。"""
    with _status_lock:
        snapshot = dict(_status)
    with _scheduler_lock:
        scheduler = _scheduler_thread
    snapshot.update({
        "running": is_running(),
        "scheduler_running": bool(scheduler and scheduler.is_alive()),
    })
    return snapshot


def run_refresh_with_status(
        trigger: str = "manual", *,
        _reservation: Optional[_RefreshReservation] = None) -> int:
    """预约并执行一轮检查，在同一预约内提交完整生命周期状态。

    调用仍经过模块级零参数 ``refresh_once()``，保留测试与下游 monkeypatch
    契约。忙时直接返回 0，且不改写上一轮任何 ``last_*`` 状态。
    """
    reservation = _reservation
    if reservation is None:
        reservation = try_reserve_refresh()
        if reservation is None:
            log.info("自动下载:已有一轮检查在运行,本次状态包装器跳过")
            return 0
    if not isinstance(reservation, _RefreshReservation):
        raise TypeError("无效的追更预约")
    reservation.claim()

    had_marker = hasattr(_cycle_metrics, "reservation_held")
    previous_marker = getattr(_cycle_metrics, "reservation_held", False)
    _cycle_metrics.reservation_held = True
    try:
        started_at = int(time.time())
        _cycle_metrics.partial_errors = 0
        _update_status(last_trigger=str(trigger or "manual"),
                       last_started_at=started_at, last_error=None)
        try:
            enqueued = refresh_once()
        except Exception as exc:
            _update_status(last_finished_at=int(time.time()),
                           last_error=str(exc)[:500],
                           last_enqueued=0,
                           last_partial_errors=int(
                               getattr(_cycle_metrics, "partial_errors", 0) or 0))
            raise
        finished_at = int(time.time())
        _update_status(last_finished_at=finished_at,
                       last_success_at=finished_at,
                       last_error=None,
                       last_enqueued=int(enqueued or 0),
                       last_partial_errors=int(
                           getattr(_cycle_metrics, "partial_errors", 0) or 0))
        return enqueued
    finally:
        if had_marker:
            _cycle_metrics.reservation_held = previous_marker
        else:
            try:
                del _cycle_metrics.reservation_held
            except AttributeError:
                pass
        # 成功/错误终态已经写完后才允许下一轮预约，避免生命周期状态交错。
        reservation.release()


def refresh_once() -> int:
    """跑一轮检查:清单里每个 UP 的新投稿(未下载过的)直接入队下载。

    返回本轮入队任务数(未启动新一轮时为 0)。串行 + 间隔,防账号级风控。

    锁是非阻塞获取:已有一轮在跑时**直接丢弃本次触发**,而不是排队等锁 ——
    阻塞获取只做到「不并发」,做不到「不重复」,后来者会在前一轮结束后完整补跑
    一轮真实请求;非阻塞获取才是需求 2.9 要的「跳过」。

    `finally: _lock.release()` 不可省:手写 acquire 没有 `with` 自带的异常安全,
    漏掉会让锁永远握在一个已返回的调用手里 —— is_running() 恒 True,之后每次
    检查(手动点击 + 后台轮询)都被误判成「已有一轮在跑」而跳过,自动下载彻底
    停摆且不报任何错。
    """
    _cycle_metrics.partial_errors = 0
    reservation_held = bool(getattr(_cycle_metrics, "reservation_held", False))
    acquired_here = False
    if reservation_held:
        if not _lock.locked():
            raise RuntimeError("追更预约标记存在但业务锁未持有")
    elif not _lock.acquire(blocking=False):
        log.info("自动下载:已有一轮检查在运行,本次跳过(不排队、不发请求)")
        return 0
    else:
        acquired_here = True
    try:
        ups = up_list.list_ups()
        collections = up_list.list_collections()
        if not ups and not collections:
            return 0
        # 一轮只复制/扫描一次任务史；每个 UP 仍独立过滤自己的失败任务。
        tasks_snapshot = downloader.list_tasks()
        # 游标也一轮只读一次盘:本轮内每个 UP 的写入都只动自己的条目,
        # 快照值与「处理到它时现读」等价,不必按 UP 数重复解析整份清单。
        last_seen_map = up_list.get_last_seen_all()
        enqueued = 0
        for i, up in enumerate(ups):
            try:
                mid = int(up["mid"])
                last = last_seen_map.get(str(mid))
                new, newest = fetch_new(mid, last)
                if not newest:
                    log.warning("自动下载:%s 无投稿数据，保留 last_seen",
                                up.get("uname") or mid)
                elif not up.get("uname") and newest.get("uploader"):
                    # 存量记录没存到名称的,用视频作者名回填
                    up_list.set_uname(mid, newest["uploader"])

                label = (up.get("quality") or "").strip()
                fresh = [
                    dict(v, uploader_mid=mid, source="auto_refresh", auto_attempt=0)
                    for v in new
                    if v.get("url") and not downloader.is_downloaded(v["url"])
                ]
                # 任务记录本身充当 durable outbox：即使时间游标已推进，异步下载
                # 后来失败/重启中断，下一轮仍会从任务史恢复，而非永久漏稿。
                candidates = _retry_items(mid, tasks_snapshot=tasks_snapshot)
                occupied = {
                    (str(v.get("url") or ""), int(v.get("page") or 1))
                    for v in candidates
                }
                for video in fresh:
                    key = (str(video.get("url") or ""), int(video.get("page") or 1))
                    if key not in occupied:
                        occupied.add(key)
                        candidates.append(video)

                if candidates:
                    tids = downloader.start_batch(candidates, label=label,
                                                  max_concurrent=1)
                    if tids:
                        enqueued += len(tids)
                        log.info("自动下载 %s:%d 条入队(清晰度 %s)",
                                 up.get("uname") or mid, len(tids), label or "自动")

                if newest:
                    if newest.get("_scan_complete", True):
                        # 只有确认越过旧秒边界/到真实末页才推进 committed cursor。
                        _commit_complete_cursor(mid, newest, new)
                    else:
                        up_list.save_scan_state(mid, newest["_scan_state"])
                        log.info("自动下载 %s:本轮达到 %d 页预算，已保存续扫点 pn=%s",
                                 up.get("uname") or mid, MAX_PAGES,
                                 newest["_scan_state"].get("next_pn"))
            except Exception:
                _cycle_metrics.partial_errors = int(
                    getattr(_cycle_metrics, "partial_errors", 0) or 0) + 1
                log.exception("自动下载处理 UP %s 异常", up.get("mid"))
            # 无论本轮结果如何,UP 之间都保持间隔(防风控的关键)
            if i < len(ups) - 1:
                time.sleep(UP_INTERVAL)

        # 合集 / 收藏夹订阅:首次以全部现有条目为基线,之后只下新增的
        # UP 与第一个合集之间也要保持间隔(否则最后一个 UP 后立刻打下一个请求)
        if ups and collections:
            time.sleep(UP_INTERVAL)
        for i, coll in enumerate(collections):
            try:
                enqueued += _refresh_collection(coll, tasks_snapshot=tasks_snapshot)
            except Exception:
                _cycle_metrics.partial_errors = int(
                    getattr(_cycle_metrics, "partial_errors", 0) or 0) + 1
                log.exception("自动下载处理合集 %s:%s 异常",
                              coll.get("kind"), coll.get("id"))
            if i < len(collections) - 1:
                time.sleep(UP_INTERVAL)
        return enqueued
    finally:
        # 直接调用 refresh_once 时自行放锁；状态包装器持有的预约要延续到
        # last_finished/last_error 提交完成，由 reservation.release() 统一释放。
        if acquired_here:
            _lock.release()


def _fetch_collection_items(kind: str, cid: int) -> list[dict]:
    """按订阅类型拉取合集/收藏夹当前条目(归一化视频卡字段)。"""
    if kind == "season":
        return bapi.fetch_season_content(cid)
    if kind == "fav":
        return bapi.fetch_fav_content(cid)
    raise ValueError(f"未知订阅类型:{kind}")


def _collection_retry_items(items: list[dict], now: Optional[int] = None,
                            tasks_snapshot: Optional[list[dict]] = None
                            ) -> list[dict]:
    """从任务史恢复该合集条目里失败/中断的自动任务，带指数退避。

    合集没有时间游标可用，已见集合一推进就不再回头：下载失败若只靠「下一轮
    再看」就永远漏稿（该条已在 seen 里）。所以失败必须由任务史兜底，与 UP
    订阅的 ``_retry_items`` 同一职责。匹配键是 bvid 而不是 uploader_mid——
    合集条目的投稿人各不相同，用 UP mid 根本找不到这些任务。
    """
    now = int(time.time()) if now is None else int(now)
    bvids = {str(it.get("bvid") or "") for it in items} - {""}
    if not bvids:
        return []
    tasks = downloader.list_tasks() if tasks_snapshot is None else tasks_snapshot
    latest: dict[tuple[str, int], dict] = {}
    for task in tasks:
        if task.get("source") != "auto_refresh":
            continue
        if str(task.get("bvid") or "") not in bvids:
            continue
        url = str(task.get("url") or "")
        if not url:
            continue
        try:
            page = int(task.get("page") or 1)
        except (TypeError, ValueError):
            continue
        key = (url, page)
        old = latest.get(key)
        if old is None or _as_ts(task.get("created_at")) >= _as_ts(old.get("created_at")):
            latest[key] = task

    retries = []
    for task in latest.values():
        if task.get("status") not in ("failed", "interrupted"):
            continue
        attempt = _as_ts(task.get("auto_attempt"))
        if attempt >= AUTO_RETRY_MAX_ATTEMPTS:
            continue
        delay = min(AUTO_RETRY_MAX, AUTO_RETRY_BASE * (2 ** min(attempt, 7)))
        failed_at = _as_ts(task.get("finished_at") or task.get("created_at"))
        if failed_at and now - failed_at < delay:
            continue
        url = str(task.get("url") or "")
        if downloader.is_downloaded(url):
            continue
        retries.append({
            "bvid": task.get("bvid") or "",
            "title": task.get("title") or "(解析中)",
            "url": url,
            "page": int(task.get("page") or 1),
            "thumbnail": task.get("thumbnail") or "",
            "uploader": task.get("uploader") or "",
            "uploader_mid": task.get("uploader_mid") or 0,
            "duration": task.get("duration") or 0,
            "source": "auto_refresh",
            "auto_attempt": attempt + 1,
        })
    return retries


def _refresh_collection(coll: dict,
                        tasks_snapshot: Optional[list[dict]] = None) -> int:
    """处理一个合集/收藏夹订阅,返回本轮入队任务数。

    语义与 UP 订阅一致:首次添加时把当时全部条目记入基线,之后只下新出现的。
    与 UP 不同,合集没有时间游标可用,只能靠 bvid 集合去重,所以:
    - 条目数超过上限时明确失败,不截断后假装完成(否则会永久漏稿);
    - 已见集合按「新在前」截断到 COLLECTION_SEEN_MAX,旧条目被挤出后若仍在
      合集里会重新入队,因此上限取得比常见合集规模大得多;
    - 失败/中断的下载由 ``_collection_retry_items`` 从任务史恢复,已下载过的
      新条目不再重复入队(与 UP 订阅同一套判定)。
    """
    kind = str(coll.get("kind") or "")
    cid = int(coll.get("id") or 0)
    if not cid:
        return 0
    items = _fetch_collection_items(kind, cid)
    if len(items) > up_list.COLLECTION_SEEN_MAX:
        raise RuntimeError(
            f"{kind}:{cid} 条目数 {len(items)} 超过去重上限 "
            f"{up_list.COLLECTION_SEEN_MAX}；请先在 B 站把该合集条目减到上限以内")

    seen = {str(v) for v in (coll.get("seen_bvids") or []) if v}
    if not seen:
        # 无基线(理论上不会发生):以当前全部条目为基线,不下任何东西。
        up_list.commit_collection_seen(kind, cid,
                                       [it.get("bvid") for it in items])
        log.info("自动下载 %s:%s 建立基线 %d 条", kind, cid, len(items))
        return 0

    new_items = [it for it in items
                 if it.get("bvid") and it.get("url")
                 and str(it["bvid"]) not in seen]

    label = str(coll.get("quality") or "").strip()
    candidates = [
        # 合集条目通常不带 ?p=,但仍按 URL 解析分 P,避免多 P 视频下错分集
        {"url": str(it["url"]), "title": it.get("title", ""),
         "page": max(1, int(page_from_url(it["url"]))),
         "thumbnail": it.get("thumbnail", ""),
         "uploader": it.get("uploader", ""),
         "uploader_mid": it.get("uploader_mid") or 0,
         "duration": it.get("duration") or 0,
         "bvid": it.get("bvid", ""),
         "source": "auto_refresh"}
        # 已经下载过的条目不再重复下载;它们仍算「已见」,见下面的提交。
        for it in new_items if not downloader.is_downloaded(it["url"])
    ]
    # 失败/中断的任务从任务史恢复:已见集合不再回头,不靠这里兜底就永久漏稿。
    # 注意必须在「本轮没有新增」时也执行——没有新增不等于没有失败要重试。
    pending = _collection_retry_items(items, tasks_snapshot=tasks_snapshot)
    occupied = {(str(v.get("url") or ""), int(v.get("page") or 1))
                for v in pending}
    for video in candidates:
        key = (str(video.get("url") or ""), int(video.get("page") or 1))
        if key not in occupied:
            occupied.add(key)
            pending.append(video)

    tids = (downloader.start_batch(pending, label=label, max_concurrent=1)
            if pending else [])
    if tids:
        log.info("自动下载 %s:%s 新入库 %d 条入队(清晰度 %s)",
                 kind, cid, len(tids), label or "自动")
    # 提交「本轮新出现的全部条目」而不是只提交入队的那批:已下载的也要标已见,
    # 否则每一轮都要为它们重新查一次是否已下载。无新增时这次提交只更新
    # checked_at,表示「本轮检查过」。
    up_list.commit_collection_seen(kind, cid,
                                   [it.get("bvid") for it in new_items])
    return len(tids)


def start_loop() -> threading.Thread:
    """幂等启动守护轮询线程；死亡线程可在后续调用中重建。"""
    global _scheduler_thread

    def loop():
        while True:
            interval = max(60, config.AUTO_REFRESH_MINUTES * 60)
            _update_status(next_run_at=int(time.time() + interval))
            time.sleep(interval)
            _update_status(next_run_at=None)
            try:
                n = run_refresh_with_status("scheduled")
                if n:
                    log.info("自动下载本轮入队 %d 条", n)
            except Exception:
                log.exception("自动下载轮询异常")

    with _scheduler_lock:
        current = _scheduler_thread
        if current is not None and current.is_alive():
            return current
        th = threading.Thread(target=loop, daemon=True, name="bd-auto-refresh")
        _scheduler_thread = th
        try:
            th.start()
        except Exception:
            if _scheduler_thread is th:
                _scheduler_thread = None
            raise
        return th
