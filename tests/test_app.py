"""路由层行为测试:/api/up-updates(A3)与 /api/stats(C3)。"""
import inspect
import time

import pytest


@pytest.fixture()
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _patch_up_env(monkeypatch, ups, fetch_by_mid, last_seen=None):
    """给 /api/up-updates 打桩:UP 清单、fetch_up_videos、last_seen 读写。"""
    marked = []
    sleeps = []

    def fake_fetch(mid, ps=10):
        return fetch_by_mid.get(int(mid), [])

    monkeypatch.setattr("app.up_list.list_ups", lambda: ups)
    monkeypatch.setattr("app.auth.fetch_up_videos", fake_fetch)
    monkeypatch.setattr("app.up_list.get_last_seen",
                        lambda mid: last_seen.get(int(mid)) if last_seen else None)
    monkeypatch.setattr("app.up_list.mark_seen",
                        lambda mid, bvid, ts, title="": marked.append((int(mid), bvid, ts)))
    monkeypatch.setattr("app.time.sleep", lambda s: sleeps.append(s))
    return marked, sleeps


def _vid(bvid, ts, title="视频"):
    return {"bvid": bvid, "title": title, "uploader": "UP", "thumbnail": "",
            "view_at": ts, "url": f"https://www.bilibili.com/video/{bvid}"}


def test_up_updates_fetches_each_up_once(client, monkeypatch):
    """mark_seen 必须复用第一次抓取的结果,不得为每个 UP 再发一次 ps=1 请求。"""
    ups = [{"mid": 1}, {"mid": 2}]
    fetch_calls = []

    def fake_fetch(mid, ps=10):
        fetch_calls.append((int(mid), ps))
        return [_vid(f"BV{mid}", 100)]

    marked, _ = _patch_up_env(monkeypatch, ups, {})
    monkeypatch.setattr("app.auth.fetch_up_videos", fake_fetch)

    r = client.get("/api/up-updates")
    assert r.get_json()["ok"]
    # 每个 UP 只请求一次(ps=20),不再有 ps=1 的二次抓取
    assert fetch_calls == [(1, 20), (2, 20)]


def test_up_updates_serial_with_delay(client, monkeypatch):
    """arc/search 并发会触发 B 站账号级 412,必须串行 + UP 间隔。"""
    ups = [{"mid": 1}, {"mid": 2}, {"mid": 3}]
    marked, sleeps = _patch_up_env(monkeypatch, ups,
                                   {1: [_vid("BV1", 1)], 2: [], 3: [_vid("BV3", 3)]})
    import app as app_module
    src = inspect.getsource(app_module.up_updates)
    assert "ThreadPoolExecutor" not in src, "不得用线程池并发打 arc/search"
    r = client.get("/api/up-updates")
    assert r.get_json()["ok"]
    assert len(sleeps) == len(ups) - 1, "UP 之间应有防风控间隔"


def test_up_updates_rate_limited_up_keeps_last_seen(client, monkeypatch):
    """UP 被限流返回空时,不得推进 last_seen——否则用户没看到的视频永远不再算新增。"""
    ups = [{"mid": 1}, {"mid": 2}]
    # mid=2 整页(ps=20)被限流为空,但若代码再发 ps=1 拿到数据就会误推进
    marked, _ = _patch_up_env(monkeypatch, ups, {1: [_vid("BVnew1", 200)]})

    def fake_fetch(mid, ps=10):
        mid = int(mid)
        if mid == 2:
            return [_vid("BVnew2", 999)] if ps == 1 else []
        return [_vid("BVnew1", 200)]

    monkeypatch.setattr("app.auth.fetch_up_videos", fake_fetch)

    r = client.get("/api/up-updates")
    body = r.get_json()
    assert body["ok"]
    bvids = [it["bvid"] for it in body["items"]]
    assert bvids == ["BVnew1"], "被限流的 UP 不应产出条目"
    assert marked == [(1, "BVnew1", 200)], "只有真正拿到数据的 UP 才推进 last_seen"


def test_up_updates_filters_seen_videos(client, monkeypatch):
    """增量模式:只返回比 last_seen 更新的视频,并推进最新已见。"""
    ups = [{"mid": 1}]
    marked, _ = _patch_up_env(
        monkeypatch, ups,
        {1: [_vid("BVnew", 200, "新视频"), _vid("BVold", 100, "旧视频")]},  # arc/search 按最新在前
        last_seen={1: {"bvid": "BVold", "ts": 100, "title": "旧视频"}})

    r = client.get("/api/up-updates")
    body = r.get_json()
    assert [it["bvid"] for it in body["items"]] == ["BVnew"]
    assert marked == [(1, "BVnew", 200)]


def test_up_updates_include_seen_returns_all(client, monkeypatch):
    """include_seen=true:已见的也返回,但仍推进最新已见。"""
    ups = [{"mid": 1}]
    marked, _ = _patch_up_env(
        monkeypatch, ups,
        {1: [_vid("BVnew", 200, "新视频"), _vid("BVold", 100, "旧视频")]},  # arc/search 按最新在前
        last_seen={1: {"bvid": "BVold", "ts": 100, "title": "旧视频"}})

    r = client.get("/api/up-updates?include_seen=true")
    body = r.get_json()
    assert [it["bvid"] for it in body["items"]] == ["BVnew", "BVold"]
    assert marked == [(1, "BVnew", 200)]


# ---------- /api/stats ----------

def test_stats_month_is_calendar_month(client, monkeypatch, tmp_path):
    """「本月」按自然月算,而不是近 30 天:上月末完成的任务不算本月。"""
    import datetime
    first = datetime.date.today().replace(day=1)
    month_start = int(time.mktime(first.timetuple()))
    monkeypatch.setattr("downloader.list_tasks", lambda: [
        {"status": "done", "downloaded_bytes": 100, "finished_at": int(time.time()) - 3600},  # 今天
        {"status": "done", "downloaded_bytes": 100, "finished_at": month_start - 43200},      # 上月末
    ])
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", tmp_path)
    r = client.get("/api/stats").get_json()
    assert r["ok"]
    assert r["total_count"] == 2
    assert r["day_count"] == 1
    assert r["month_count"] == 1, "上月末完成的任务不属于本月(自然月口径)"


def test_app_annotations_resolve():
    """模块注解应完整可解析(曾缺 `from typing import Optional`)。"""
    import typing
    import app as app_module
    typing.get_type_hints(app_module._parse_mid_from_input)


# ---------- 前端结构(沿用仓库惯例:对模板做内容断言) ----------

def _tpl():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text(encoding="utf-8")


def test_frontend_history_load_more():
    """观看历史支持「加载更早」:走服务端分页逐页追加,而不是一次拉完。"""
    html = _tpl()
    assert "loadMoreHistory" in html, "缺少加载更早入口"
    assert "/api/history?page=" in html, "历史应按 page 参数分页请求"


def test_frontend_modal_uses_theme_vars():
    """删除确认弹窗背景走主题变量,深色模式下文字才可读。"""
    html = _tpl()
    modal_start = html.find('id="del-modal"')
    modal = html[modal_start:modal_start + 400]
    assert "var(--bg-card)" in modal, "弹窗应使用 var(--bg-card) 而非硬编码白色"
