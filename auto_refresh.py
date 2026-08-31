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

# 保证「同一时刻只有一轮检查」。非阻塞获取:拿不到就跳过,不排队。
# 阻塞获取(`with _lock:`)只保证互斥,不保证去重 —— 后来者会卡在锁上,等前一轮
# 释放后完整补跑一轮(每个 UP 一次 arc/search、UP 之间 sleep 5 秒)。连点 N 次
# 「立即检查新视频」就是串行跑 N 轮完整检查,正是 docs/COMPLIANCE.md 承诺要
# 避免的账号级风控场景。需求 2.9 要的是「跳过」,不是「排队后补跑」。
_lock = threading.Lock()


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
    refresh_once 区分“完整覆盖旧游标”和“只耗尽本轮页预算”。未完整时保留
    committed cursor，并持久化下一页；续扫先重取首页检测 offset 分页漂移，
    每轮请求仍不超过 MAX_PAGES。
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
        collected = [dict(v) for v in (scan.get("items") or [])
                     if isinstance(v, dict) and v.get("bvid")]
    else:
        ps = estimate_ps(last.get("fetched_at"))
        collected = []

    complete = False
    pages_used = 0
    next_pn = 1

    if scan:
        # offset 分页在新投稿插入后会整体右移。先重取首页：头变则从第 2 页
        # 重新衔接，头未变才信任持久化 next_pn；已有 items 始终按 BVID 去重。
        first = bapi.fetch_up_videos(mid, ps=ps, pn=1)
        pages_used = 1
        if not first:
            complete = True
        else:
            old_head = (str(scan.get("head_bvid") or ""),
                        _as_ts(scan.get("head_ts")))
            new_head = (str(first[0].get("bvid") or ""),
                        _as_ts(first[0].get("view_at")))
            _merge_page(collected, first, prepend=True)
            if _page_finishes_scan(first, ps, seen_ts):
                complete = True
            elif new_head != old_head:
                next_pn = 2
            else:
                try:
                    next_pn = max(2, int(scan.get("next_pn") or 2))
                except (TypeError, ValueError):
                    next_pn = 2

    while not complete and pages_used < MAX_PAGES:
        page = bapi.fetch_up_videos(mid, ps=ps, pn=next_pn)
        pages_used += 1
        requested_pn = next_pn
        next_pn = requested_pn + 1
        if not page:
            complete = True
            break
        _merge_page(collected, page)
        if _page_finishes_scan(page, ps, seen_ts):
            complete = True

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
            head = collected[0]
            newest["_scan_state"] = {
                "base_bvid": seen_bvid,
                "base_ts": seen_ts,
                "ps": ps,
                "next_pn": next_pn,
                "head_bvid": str(head.get("bvid") or ""),
                "head_ts": _as_ts(head.get("view_at")),
                "items": collected,
                "updated_at": int(time.time()),
            }
    return new, newest


def _retry_items(mid: int, now: Optional[int] = None) -> list[dict]:
    """从任务史恢复该 UP 最新一次失败/中断的自动任务，带指数退避。"""
    now = int(time.time()) if now is None else int(now)
    latest: dict[tuple[str, int], dict] = {}
    for task in downloader.list_tasks():
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
    if not _lock.acquire(blocking=False):
        log.info("自动下载:已有一轮检查在运行,本次跳过(不排队、不发请求)")
        return 0
    try:
        ups = up_list.list_ups()
        if not ups:
            return 0
        enqueued = 0
        for i, up in enumerate(ups):
            try:
                mid = int(up["mid"])
                last = up_list.get_last_seen(mid)
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
                candidates = _retry_items(mid)
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
                log.exception("自动下载处理 UP %s 异常", up.get("mid"))
            # 无论本轮结果如何,UP 之间都保持间隔(防风控的关键)
            if i < len(ups) - 1:
                time.sleep(UP_INTERVAL)
        return enqueued
    finally:
        # 任何出口都要放锁:正常 return、`ups` 为空的提前 return、以及
        # list_ups() 之类锁内异常向上传播的路径。
        _lock.release()


def start_loop() -> threading.Thread:
    """启动守护轮询线程;间隔运行时读 config.AUTO_REFRESH_MINUTES(实时可调)。"""
    def loop():
        while True:
            time.sleep(max(60, config.AUTO_REFRESH_MINUTES * 60))
            try:
                n = refresh_once()
                if n:
                    log.info("自动下载本轮入队 %d 条", n)
            except Exception:
                log.exception("自动下载轮询异常")

    th = threading.Thread(target=loop, daemon=True, name="bd-auto-refresh")
    th.start()
    return th
