"""auto_refresh.py:订阅追更(串行防风控 + 跳过已下载 + 限流不推进 last_seen)。"""
import auto_refresh


def _vid(bvid, ts):
    return {"bvid": bvid, "title": f"视频{bvid}", "uploader": "UP", "thumbnail": "",
            "view_at": ts, "url": f"https://www.bilibili.com/video/{bvid}"}


def _patch_env(monkeypatch, ups, fetch_by_mid, last_seen=None):
    """打桩:UP 清单 / arc/search / last_seen 读写 / 批量下载 / sleep。"""
    batches, marked, sleeps = [], [], []

    def fake_list_ups():
        return ups

    def fake_fetch(mid, ps=10):
        return fetch_by_mid.get(int(mid), [])

    def fake_start_batch(items, label, max_concurrent=None):
        batches.append(([v["bvid"] for v in items], max_concurrent))
        return ["t"] * len(items)

    monkeypatch.setattr(auto_refresh.up_list, "list_ups", fake_list_ups)
    monkeypatch.setattr(auto_refresh.auth, "fetch_up_videos", fake_fetch)
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen",
                        lambda mid: last_seen.get(int(mid)) if last_seen else None)
    monkeypatch.setattr(auto_refresh.up_list, "mark_seen",
                        lambda mid, bvid, ts, title="": marked.append((int(mid), bvid, ts)))
    monkeypatch.setattr(auto_refresh.downloader, "start_batch", fake_start_batch)
    monkeypatch.setattr(auto_refresh.downloader, "is_downloaded", lambda url: False)
    monkeypatch.setattr(auto_refresh.time, "sleep", lambda s: sleeps.append(s))
    return batches, marked, sleeps


def test_only_auto_ups_processed(monkeypatch):
    ups = [{"mid": 1, "auto": True}, {"mid": 2, "auto": False}]
    batches, marked, _ = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BVnew1", 200)], 2: [_vid("BVnew2", 200)]})
    n = auto_refresh.refresh_once()
    assert n == 1
    assert batches == [(["BVnew1"], 1)], "只下载 auto=True 的 UP,且串行(并发=1)"
    assert marked == [(1, "BVnew1", 200)], "未开追更的 UP 不动 last_seen"


def test_skips_seen_and_downloaded(monkeypatch):
    ups = [{"mid": 1, "auto": True}]
    batches, marked, _ = _patch_env(
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
    assert marked == [(1, "BVnew", 300)], "mark_seen 用最新一条"


def test_rate_limited_up_skipped_and_not_marked(monkeypatch):
    ups = [{"mid": 1, "auto": True}]
    batches, marked, _ = _patch_env(monkeypatch, ups, {1: []})
    assert auto_refresh.refresh_once() == 0
    assert batches == []
    assert marked == [], "限流返回空不得推进 last_seen"


def test_serial_interval_between_ups(monkeypatch):
    ups = [{"mid": 1, "auto": True}, {"mid": 2, "auto": True}, {"mid": 3, "auto": True}]
    batches, _, sleeps = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BV1", 1)], 2: [], 3: [_vid("BV3", 3)]})
    auto_refresh.refresh_once()
    expected = [auto_refresh.UP_INTERVAL] * (len(ups) - 1)
    assert sleeps == expected, "UP 之间应有防风控间隔"
    assert auto_refresh.UP_INTERVAL >= 3.0, "合规要求:自动任务请求间隔不低于 3 秒"


def test_first_run_downloads_everything(monkeypatch):
    """从未 mark_seen 过的 UP:全部视为新增(与手动刷新语义一致)。"""
    ups = [{"mid": 1, "auto": True}]
    batches, _, _ = _patch_env(monkeypatch, ups, {1: [_vid("BVa", 1), _vid("BVb", 2)]})
    n = auto_refresh.refresh_once()
    assert n == 2
    assert batches == [(["BVa", "BVb"], 1)]
