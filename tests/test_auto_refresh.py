"""auto_refresh.py:订阅追更(串行防风控 + 增量拉取 + 跳过已下载 + 限流不推进)。"""
import time

import auto_refresh


def _vid(bvid, ts):
    return {"bvid": bvid, "title": f"视频{bvid}", "uploader": "UP", "thumbnail": "",
            "view_at": ts, "url": f"https://www.bilibili.com/video/{bvid}"}


def _patch_env(monkeypatch, ups, fetch_by_mid, last_seen=None):
    """打桩:UP 清单 / arc/search / last_seen 读写 / 历史 / 批量下载 / sleep。"""
    batches, marked, sleeps, history = [], [], [], []

    def fake_list_ups():
        return ups

    def fake_fetch(mid, ps=10, pn=1):
        return fetch_by_mid.get(int(mid), [])

    def fake_start_batch(items, label, max_concurrent=None):
        batches.append(([v["bvid"] for v in items], max_concurrent))
        return ["t"] * len(items)

    monkeypatch.setattr(auto_refresh.up_list, "list_ups", fake_list_ups)
    monkeypatch.setattr(auto_refresh.auth, "fetch_up_videos", fake_fetch)
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen",
                        lambda mid: last_seen.get(int(mid)) if last_seen else None)
    monkeypatch.setattr(auto_refresh.up_list, "mark_seen",
                        lambda mid, bvid, ts, title="", fetched_at=None:
                            marked.append((int(mid), bvid, ts, fetched_at)))
    monkeypatch.setattr(auto_refresh.up_list, "add_history",
                        lambda items: history.extend(v["bvid"] for v in items))
    monkeypatch.setattr(auto_refresh.downloader, "start_batch", fake_start_batch)
    monkeypatch.setattr(auto_refresh.downloader, "is_downloaded", lambda url: False)
    monkeypatch.setattr(auto_refresh.time, "sleep", lambda s: sleeps.append(s))
    return batches, marked, sleeps, history


def test_only_auto_ups_processed(monkeypatch):
    ups = [{"mid": 1, "auto": True}, {"mid": 2, "auto": False}]
    batches, marked, _, _ = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BVnew1", 200)], 2: [_vid("BVnew2", 200)]})
    n = auto_refresh.refresh_once()
    assert n == 1
    assert batches == [(["BVnew1"], 1)], "只下载 auto=True 的 UP,且串行(并发=1)"
    assert len(marked) == 1 and marked[0][:3] == (1, "BVnew1", 200)
    assert marked[0][3], "mark_seen 应带拉取时间 fetched_at"


def test_skips_seen_and_downloaded(monkeypatch):
    ups = [{"mid": 1, "auto": True}]
    batches, marked, _, history = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BVnew", 300),        # 新视频
             _vid("BVseen", 100),       # 已见(ts 不大于 last_seen)
             _vid("BVdl", 250)]},       # 新但之前已下载过
        last_seen={1: {"bvid": "BVseen", "ts": 100, "title": ""}})
    # BVdl 标记为已下载
    monkeypatch.setattr(
        auto_refresh.downloader, "is_downloaded",
        lambda url: "BVdl" in url)
    n = auto_refresh.refresh_once()
    assert n == 1
    assert batches == [(["BVnew"], 1)]
    assert marked[0][:3] == (1, "BVnew", 300), "mark_seen 用最新一条"
    # 历史记录的是「发现的新增」(含已下载过的 BVdl),不只是本次下载的
    assert history == ["BVnew", "BVdl"]


def test_rate_limited_up_skipped_and_not_marked(monkeypatch):
    ups = [{"mid": 1, "auto": True}]
    batches, marked, _, _ = _patch_env(monkeypatch, ups, {1: []})
    assert auto_refresh.refresh_once() == 0
    assert batches == []
    assert marked == [], "限流返回空不得推进 last_seen"


def test_serial_interval_between_ups(monkeypatch):
    ups = [{"mid": 1, "auto": True}, {"mid": 2, "auto": True}, {"mid": 3, "auto": True}]
    batches, _, sleeps, _ = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BV1", 1)], 2: [], 3: [_vid("BV3", 3)]})
    auto_refresh.refresh_once()
    expected = [auto_refresh.UP_INTERVAL] * (len(ups) - 1)
    assert sleeps == expected, "UP 之间应有防风控间隔"
    assert auto_refresh.UP_INTERVAL >= 3.0, "合规要求:自动任务请求间隔不低于 3 秒"


def test_first_run_downloads_everything(monkeypatch):
    """从未 mark_seen 过的 UP:全部视为新增(与手动刷新语义一致)。"""
    ups = [{"mid": 1, "auto": True}]
    batches, _, _, _ = _patch_env(monkeypatch, ups, {1: [_vid("BVa", 1), _vid("BVb", 2)]})
    n = auto_refresh.refresh_once()
    assert n == 2
    assert batches == [(["BVa", "BVb"], 1)]


# ---------- 增量拉取:自适应页大小 + 翻页兜底(2026-08-16) ----------

def test_estimate_ps_tiers():
    """刚拉过(24h 内)用小页,超过 24h 或从未拉过用整页。"""
    now = time.time()
    assert auto_refresh.estimate_ps(None, now) == auto_refresh.FULL_PS
    assert auto_refresh.estimate_ps(now - 3600, now) == auto_refresh.RECENT_PS
    assert auto_refresh.estimate_ps(now - 25 * 3600, now) == auto_refresh.FULL_PS
    assert auto_refresh.RECENT_PS < auto_refresh.FULL_PS


def _patch_fetch_calls(monkeypatch, pages_by_mid):
    """按 (mid, pn) 返回预置页,并记录每次请求的 ps/pn。"""
    calls = []

    def fake_fetch(mid, ps=10, pn=1):
        calls.append((int(mid), ps, pn))
        return pages_by_mid.get((int(mid), pn), [])

    monkeypatch.setattr(auto_refresh.auth, "fetch_up_videos", fake_fetch)
    return calls


def test_fetch_new_small_ps_when_recently_fetched(monkeypatch):
    """上次拉取在一小时内 → 只请求小页(5 条),不再整页 20 条。"""
    calls = _patch_fetch_calls(monkeypatch, {(1, 1): [_vid("BVnew", 300)]})
    last = {"bvid": "BVold", "ts": 100, "fetched_at": time.time() - 3600}
    new, newest = auto_refresh.fetch_new(1, last)
    assert calls[0] == (1, auto_refresh.RECENT_PS, 1)
    assert [v["bvid"] for v in new] == ["BVnew"]
    assert newest["bvid"] == "BVnew"


def test_fetch_new_full_ps_when_stale_or_never(monkeypatch):
    calls = _patch_fetch_calls(monkeypatch, {(1, 1): [_vid("BVnew", 300)]})
    # 从未拉过
    auto_refresh.fetch_new(1, None)
    assert calls[0][1] == auto_refresh.FULL_PS
    calls.clear()
    # 三天前拉过
    auto_refresh.fetch_new(1, {"bvid": "BVold", "ts": 100,
                               "fetched_at": time.time() - 3 * 86400})
    assert calls[0][1] == auto_refresh.FULL_PS


def test_fetch_new_paginates_on_burst(monkeypatch):
    """小页整页都是新视频(爆发更新)→ 翻下一页兜底,不漏中间的。"""
    burst = [_vid(f"BVb{i}", 500 + i) for i in range(5)]          # 第1页:5条全新
    older = [_vid("BVold", 100), _vid("BVolder", 50)]             # 第2页:含已见
    calls = _patch_fetch_calls(monkeypatch, {(1, 1): burst, (1, 2): older})
    last = {"bvid": "BVold", "ts": 100, "fetched_at": time.time() - 600}
    new, newest = auto_refresh.fetch_new(1, last)
    assert [(pn) for _, _, pn in calls] == [1, 2], "第1页整页新 → 应翻第2页"
    assert [v["bvid"] for v in new] == [f"BVb{i}" for i in range(5)], "已见的过滤掉"
    assert newest["bvid"] == "BVb0"


def test_fetch_new_stops_when_page_covered(monkeypatch):
    """页尾出现已见视频 → 已覆盖到上次位置,不再翻页。"""
    page = [_vid("BVnew", 300), _vid("BVold", 100)]
    calls = _patch_fetch_calls(monkeypatch, {(1, 1): page, (1, 2): []})
    last = {"bvid": "BVold", "ts": 100, "fetched_at": time.time() - 600}
    new, _ = auto_refresh.fetch_new(1, last)
    assert len(calls) == 1, "无需第2页"
    assert [v["bvid"] for v in new] == ["BVnew"]


def test_fetch_new_rate_limited(monkeypatch):
    calls = _patch_fetch_calls(monkeypatch, {})
    new, newest = auto_refresh.fetch_new(1, {"bvid": "x", "ts": 1, "fetched_at": time.time()})
    assert new == [] and newest is None
