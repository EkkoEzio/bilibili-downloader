"""订阅追更:定时串行拉取 auto=True 的 UP,新增投稿自动入队下载。

风控与合规边界(2026-08-15 法律评估结论):
- 自动任务属于 B 站用户协议"自动程序"条款场景,是本工具里风险最高的一块,
  因此间隔刻意保守:UP 之间 >=3 秒(合规清单建议值),轮询间隔下限 10 分钟
  (config.AUTO_REFRESH_MINUTES,UI 不可调破)。
- 只下载自己账号有权观看的免费内容;限流返回空时不动 last_seen。
- 下载内容仅个人存档,不分发。
"""
import logging
import threading
import time

import bilibili_auth as auth
import config
import downloader
import up_list

log = logging.getLogger("bd.auto_refresh")

UP_INTERVAL = 3.0  # UP 之间的请求间隔(秒),自动任务不低于 3 秒

_lock = threading.Lock()  # 防止上一轮没跑完又叠一轮


def refresh_once() -> int:
    """跑一轮追更:返回本轮新入队的视频数。串行 + 间隔,防账号级风控。"""
    with _lock:
        ups = [u for u in up_list.list_ups() if u.get("auto")]
        if not ups:
            return 0
        queued = 0
        for i, up in enumerate(ups):
            try:
                mid = int(up["mid"])
                fetched = auth.fetch_up_videos(mid, ps=20)
                if not fetched:
                    log.warning("追更:%s 无数据(可能限流),跳过且不推进 last_seen",
                                up.get("uname") or mid)
                else:
                    newest = fetched[0]  # arc/search 按 pubdate 倒序
                    last = up_list.get_last_seen(mid)
                    fresh = fetched
                    if last:
                        seen_ts = last.get("ts", 0)
                        seen_bvid = last.get("bvid", "")
                        fresh = [v for v in fresh
                                 if v.get("view_at", 0) > seen_ts and v.get("bvid") != seen_bvid]
                    fresh = [v for v in fresh if not downloader.is_downloaded(v["url"])]
                    if fresh:
                        downloader.start_batch(fresh, label="", max_concurrent=1)
                        queued += len(fresh)
                        log.info("追更入队 %s:%d 条", up.get("uname") or mid, len(fresh))
                    # 处理成功才推进已见;异常/限流留给下一轮重试
                    up_list.mark_seen(mid, newest["bvid"], newest.get("view_at", 0),
                                      newest.get("title", ""))
            except Exception:
                log.exception("追更处理 UP %s 异常", up.get("mid"))
            # 无论本轮结果如何,UP 之间都保持间隔(防风控的关键)
            if i < len(ups) - 1:
                time.sleep(UP_INTERVAL)
        return queued


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
