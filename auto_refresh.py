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


def fetch_new(mid: int, last: Optional[dict] = None):
    """增量拉取单个 UP。返回 (新增视频列表, 本轮最新一条或 None)。

    last 是 up_list.get_last_seen() 的结果(None = 全量)。
    翻页终止条件:页不满 / 页尾出现已见 / 到 MAX_PAGES。
    """
    ps = estimate_ps(last.get("fetched_at") if last else None)
    seen_ts = (last or {}).get("ts", 0)
    seen_bvid = (last or {}).get("bvid", "")
    collected = []
    got_bvids = set()
    for pn in range(1, MAX_PAGES + 1):
        page = bapi.fetch_up_videos(mid, ps=ps, pn=pn)
        if not page:
            break
        for v in page:
            if v.get("bvid") not in got_bvids:
                got_bvids.add(v.get("bvid"))
                collected.append(v)
        if len(page) < ps or page[-1].get("view_at", 0) <= seen_ts:
            break
    newest = collected[0] if collected else None
    new = [v for v in collected
           if v.get("view_at", 0) > seen_ts and v.get("bvid") != seen_bvid]
    return new, newest


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
                    log.warning("自动下载:%s 无数据(可能限流),跳过且不推进 last_seen",
                                up.get("uname") or mid)
                else:
                    # 存量记录没存到名称的,用视频作者名回填
                    if not up.get("uname") and newest.get("uploader"):
                        up_list.set_uname(mid, newest["uploader"])
                    to_dl = [v for v in new if not downloader.is_downloaded(v["url"])]
                    if to_dl:
                        label = (up.get("quality") or "").strip()
                        items = [dict(v, uploader_mid=mid) for v in to_dl]
                        tids = downloader.start_batch(items, label=label,
                                                      max_concurrent=1)
                        if tids:
                            enqueued += len(tids)
                            log.info("自动下载 %s:%d 条入队(清晰度 %s)",
                                     up.get("uname") or mid, len(tids), label or "自动")
                    # 处理成功才推进已见;异常/限流留给下一轮重试
                    up_list.mark_seen(mid, newest["bvid"], newest.get("view_at", 0),
                                      newest.get("title", ""), fetched_at=int(time.time()))
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
