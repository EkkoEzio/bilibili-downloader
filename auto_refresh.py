"""订阅追更:定时串行检查 auto=True 的 UP,新增投稿记入「待确认」,用户点下载才入队。

风控与合规边界(2026-08-15 法律评估结论):
- 自动任务属于 B 站用户协议"自动程序"条款场景,是本工具里风险最高的一块,
  因此间隔刻意保守:UP 之间 >=5 秒,轮询间隔下限 10 分钟
  (config.AUTO_REFRESH_MINUTES,UI 不可调破)。
- 检查与下载分离:后台只检查+记录,下载由用户确认触发,自动行为更轻、更可控。
- 只下载自己账号有权观看的免费内容;限流返回空时不动 last_seen(不丢数据)。
- 下载内容仅个人存档,不分发。
"""
import logging
import threading
import time
from typing import Optional

import bilibili_auth as auth
import config
import downloader
import up_list

log = logging.getLogger("bd.auto_refresh")

UP_INTERVAL = 5.0  # UP 之间的请求间隔(秒);检查与下载分离后检查更轻,间隔放宽到 5 秒更稳

# 增量拉取参数:B 站 arc/search 不支持按时间过滤,只能按 pubdate 倒序分页。
# 依据「上次拉取时间」自适应页大小:刚拉过(24h 内)只拉小页,降低单次响应量;
# 一页全是新视频(爆发更新)时翻页兜底,保证不漏。
FULL_PS = 15
RECENT_PS = 5
RECENT_WINDOW = 24 * 3600
MAX_PAGES = 3

_lock = threading.Lock()  # 防止上一轮没跑完又叠一轮


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
        page = auth.fetch_up_videos(mid, ps=ps, pn=pn)
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


def refresh_once() -> int:
    """跑一轮追更检查:返回本轮新发现的待确认视频数。串行 + 间隔,防账号级风控。

    只记录不下载:新视频进「待确认」列表,等用户在页面点「全部下载」才入队
    (检查与下载分离——自动检查更轻更不易触发风控,也避免未经确认的自动下载)。
    """
    with _lock:
        ups = [u for u in up_list.list_ups() if u.get("auto")]
        if not ups:
            return 0
        found = 0
        for i, up in enumerate(ups):
            try:
                mid = int(up["mid"])
                last = up_list.get_last_seen(mid)
                new, newest = fetch_new(mid, last)
                if not newest:
                    log.warning("追更:%s 无数据(可能限流),跳过且不推进 last_seen",
                                up.get("uname") or mid)
                else:
                    # 存量记录没存到名称的,用视频作者名回填
                    if not up.get("uname") and newest.get("uploader"):
                        up_list.set_uname(mid, newest["uploader"])
                    up_list.add_history(new)  # 历史记「发现的新增」(含已下载过的)
                    to_dl = [v for v in new if not downloader.is_downloaded(v["url"])]
                    added = up_list.add_pending([dict(v, uploader_mid=mid) for v in to_dl])
                    if added:
                        found += added
                        log.info("追更待确认 %s:%d 条", up.get("uname") or mid, added)
                    # 处理成功才推进已见;异常/限流留给下一轮重试
                    up_list.mark_seen(mid, newest["bvid"], newest.get("view_at", 0),
                                      newest.get("title", ""), fetched_at=int(time.time()))
            except Exception:
                log.exception("追更处理 UP %s 异常", up.get("mid"))
            # 无论本轮结果如何,UP 之间都保持间隔(防风控的关键)
            if i < len(ups) - 1:
                time.sleep(UP_INTERVAL)
        return found


def start_loop() -> threading.Thread:
    """启动守护轮询线程;间隔运行时读 config.AUTO_REFRESH_MINUTES(实时可调)。"""
    def loop():
        while True:
            time.sleep(max(60, config.AUTO_REFRESH_MINUTES * 60))
            try:
                n = refresh_once()
                if n:
                    log.info("追更本轮入队 %d 条", n)
            except Exception:
                log.exception("追更轮询异常")

    th = threading.Thread(target=loop, daemon=True, name="bd-auto-refresh")
    th.start()
    return th
