"""路由层行为测试:/api/up-updates(A3)与 /api/stats(C3)。"""
import inspect
import json
import time

import pytest


@pytest.fixture()
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _patch_up_env(monkeypatch, ups, fetch_by_mid, last_seen=None):
    """给 /api/up-updates 打桩:UP 清单、fetch_up_videos、last_seen 读写、历史。"""
    marked = []
    sleeps = []
    history = []

    def fake_fetch(mid, ps=10, pn=1):
        return fetch_by_mid.get(int(mid), [])

    monkeypatch.setattr("app.up_list.list_ups", lambda: ups)
    monkeypatch.setattr("app.auth.fetch_up_videos", fake_fetch)
    monkeypatch.setattr("app.up_list.get_last_seen",
                        lambda mid: last_seen.get(int(mid)) if last_seen else None)
    monkeypatch.setattr("app.up_list.mark_seen",
                        lambda mid, bvid, ts, title="", fetched_at=None:
                            marked.append((int(mid), bvid, ts, fetched_at)))
    monkeypatch.setattr("app.up_list.add_history",
                        lambda items: history.extend(v["bvid"] for v in items))
    monkeypatch.setattr("app.time.sleep", lambda s: sleeps.append(s))
    return marked, sleeps, history


def _vid(bvid, ts, title="视频"):
    return {"bvid": bvid, "title": title, "uploader": "UP", "thumbnail": "",
            "view_at": ts, "url": f"https://www.bilibili.com/video/{bvid}"}


def test_up_updates_fetches_each_up_once(client, monkeypatch):
    """mark_seen 必须复用第一次抓取的结果,不得为每个 UP 再发一次 ps=1 请求。"""
    ups = [{"mid": 1}, {"mid": 2}]
    fetch_calls = []

    def fake_fetch(mid, ps=10, pn=1):
        fetch_calls.append((int(mid), ps, pn))
        return [_vid(f"BV{mid}", 100)]

    marked, _, _ = _patch_up_env(monkeypatch, ups, {})
    monkeypatch.setattr("app.auth.fetch_up_videos", fake_fetch)

    r = client.get("/api/up-updates")
    assert r.get_json()["ok"]
    # 每个 UP 只请求一次(整页 20,无 last_seen 不翻页),没有二次抓取
    assert fetch_calls == [(1, 20, 1), (2, 20, 1)]


def test_up_updates_serial_with_delay(client, monkeypatch):
    """arc/search 并发会触发 B 站账号级 412,必须串行 + UP 间隔。"""
    ups = [{"mid": 1}, {"mid": 2}, {"mid": 3}]
    marked, sleeps, _ = _patch_up_env(monkeypatch, ups,
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
    # mid=2 整页被限流为空
    marked, _, _ = _patch_up_env(monkeypatch, ups, {1: [_vid("BVnew1", 200)]})

    def fake_fetch(mid, ps=10, pn=1):
        mid = int(mid)
        return [] if mid == 2 else [_vid("BVnew1", 200)]

    monkeypatch.setattr("app.auth.fetch_up_videos", fake_fetch)

    r = client.get("/api/up-updates")
    body = r.get_json()
    assert body["ok"]
    bvids = [it["bvid"] for it in body["items"]]
    assert bvids == ["BVnew1"], "被限流的 UP 不应产出条目"
    assert [m[:3] for m in marked] == [(1, "BVnew1", 200)], "只有真正拿到数据的 UP 才推进 last_seen"


def test_up_updates_filters_seen_videos(client, monkeypatch):
    """增量模式:只返回比 last_seen 更新的视频,并推进最新已见。"""
    ups = [{"mid": 1}]
    marked, _, history = _patch_up_env(
        monkeypatch, ups,
        {1: [_vid("BVnew", 200, "新视频"), _vid("BVold", 100, "旧视频")]},  # arc/search 按最新在前
        last_seen={1: {"bvid": "BVold", "ts": 100, "title": "旧视频"}})

    r = client.get("/api/up-updates")
    body = r.get_json()
    assert [it["bvid"] for it in body["items"]] == ["BVnew"]
    assert marked[0][:3] == (1, "BVnew", 200)
    assert history == ["BVnew"], "发现的新增写入本地历史"


def test_up_updates_include_seen_returns_all(client, monkeypatch):
    """include_seen=true:已见的也返回,但不写历史(历史只记增量发现)。"""
    ups = [{"mid": 1}]
    marked, _, history = _patch_up_env(
        monkeypatch, ups,
        {1: [_vid("BVnew", 200, "新视频"), _vid("BVold", 100, "旧视频")]},  # arc/search 按最新在前
        last_seen={1: {"bvid": "BVold", "ts": 100, "title": "旧视频"}})

    r = client.get("/api/up-updates?include_seen=true")
    body = r.get_json()
    assert [it["bvid"] for it in body["items"]] == ["BVnew", "BVold"]
    assert marked[0][:3] == (1, "BVnew", 200)
    assert history == [], "include_seen 不写历史"


# ---------- 增量拉取 + 本地历史(2026-08-16) ----------

def test_up_updates_adaptive_ps_recent_fetch(client, monkeypatch):
    """上次拉取在 24h 内 → 单 UP 只请求小页,不再整页 20 条。"""
    import time as _t
    import auto_refresh
    calls = []

    def fake_fetch(mid, ps=10, pn=1):
        calls.append((int(mid), ps, pn))
        return [_vid("BVnew", 200)]

    monkeypatch.setattr("app.up_list.list_ups", lambda: [{"mid": 1}])
    monkeypatch.setattr("app.auth.fetch_up_videos", fake_fetch)
    monkeypatch.setattr("app.up_list.get_last_seen",
                        lambda mid: {"bvid": "BVold", "ts": 100,
                                     "fetched_at": _t.time() - 3600})
    monkeypatch.setattr("app.up_list.mark_seen", lambda *a, **k: None)
    monkeypatch.setattr("app.up_list.add_history", lambda items: None)
    monkeypatch.setattr("app.time.sleep", lambda s: None)

    r = client.get("/api/up-updates")
    assert r.get_json()["ok"]
    assert calls[0] == (1, auto_refresh.RECENT_PS, 1)


def test_up_updates_appends_history_readable_via_route(client, monkeypatch, tmp_path):
    """刷新发现的新增写入本地历史,「查看历史」离线可读(不再打 B 站接口)。"""
    import up_list as up_list_module
    monkeypatch.setattr(up_list_module, "UP_LIST_FILE", tmp_path / "up_list.json")
    monkeypatch.setattr("app.up_list.list_ups", lambda: [{"mid": 1}])
    monkeypatch.setattr("app.auth.fetch_up_videos",
                        lambda mid, ps=10, pn=1: [_vid("BVh1", 200), _vid("BVold", 50)])
    monkeypatch.setattr("app.up_list.get_last_seen",
                        lambda mid: {"bvid": "BVold", "ts": 100})
    monkeypatch.setattr("app.up_list.mark_seen", lambda *a, **k: None)
    monkeypatch.setattr("app.time.sleep", lambda s: None)

    r = client.get("/api/up-updates")
    assert [it["bvid"] for it in r.get_json()["items"]] == ["BVh1"]

    h = client.get("/api/up-history").get_json()
    assert h["ok"]
    assert [it["bvid"] for it in h["items"]] == ["BVh1"]
    assert h["items"][0]["found_at"] > 0


def test_up_list_includes_fetched_at(client, monkeypatch, tmp_path):
    """/api/up-list 返回 fetched_at,管理面板显示「上次拉取」时间。"""
    import up_list as up_list_module
    monkeypatch.setattr(up_list_module, "UP_LIST_FILE", tmp_path / "up_list.json")
    up_list_module.add_up(1, "UPA", "")
    up_list_module.mark_seen(1, "BVx", 100, "标题", fetched_at=1234)
    r = client.get("/api/up-list").get_json()
    assert r["ups"][0]["fetched_at"] == 1234


# ---------- UP 名称:添加时用用户卡片,拉取时空名自动回填 ----------

def test_up_list_add_uses_user_card(client, monkeypatch, tmp_path):
    """添加 UP 时优先走用户卡片接口拿名称+头像(mid 人记不住,必须显示名称)。"""
    import up_list as up_list_module
    monkeypatch.setattr(up_list_module, "UP_LIST_FILE", tmp_path / "up_list.json")
    monkeypatch.setattr("app.auth.fetch_up_card",
                        lambda mid: {"uname": "远古野", "face": "https://x/face.jpg"})
    monkeypatch.setattr("app.auth.fetch_up_videos",
                        lambda mid, ps=10, pn=1: (_ for _ in ()).throw(AssertionError("不应再走 arc/search")))
    r = client.post("/api/up-list/add", json={"input": "46817116"})
    body = r.get_json()
    assert body["ok"]
    assert body["up"]["uname"] == "远古野"
    assert body["up"]["face"] == "https://x/face.jpg"

    # 存量无名称记录:重新添加同一个 mid 会补上名称
    up_list_module.UP_LIST_FILE.write_text(
        json.dumps({"ups": [{"mid": 46817116, "uname": "", "face": "", "added_at": 1, "auto": False}],
                    "last_seen": {}, "history": []}), encoding="utf-8")
    r2 = client.post("/api/up-list/add", json={"input": "46817116"})
    assert r2.get_json()["up"]["uname"] == "远古野"


def test_up_updates_backfills_missing_uname(client, monkeypatch):
    """存量数据没有 uname:拉取到视频后自动回填 UP 名称。"""
    ups = [{"mid": 1, "uname": "", "face": "", "auto": False}]
    marked, _, _ = _patch_up_env(
        monkeypatch, ups,
        {1: [_vid("BVnew", 200)]})  # _vid 的 uploader 字段为 "UP"
    renamed = []
    monkeypatch.setattr("app.up_list.set_uname",
                        lambda mid, uname: renamed.append((int(mid), uname)) or True)
    r = client.get("/api/up-updates")
    assert r.get_json()["ok"]
    assert renamed == [(1, "UP")], "uname 为空时应回填"


def test_up_updates_keeps_existing_uname(client, monkeypatch):
    """已有名称的 UP 不重复回填。"""
    ups = [{"mid": 1, "uname": "已有名", "face": "", "auto": False}]
    _patch_up_env(monkeypatch, ups, {1: [_vid("BVnew", 200)]})
    renamed = []
    monkeypatch.setattr("app.up_list.set_uname",
                        lambda mid, uname: renamed.append((int(mid), uname)) or True)
    client.get("/api/up-updates")
    assert renamed == []


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


# ---------- /api/up-list/set-auto ----------

def test_up_list_set_auto_route(client, monkeypatch):
    """POST /api/up-list/set-auto {mid, auto} 落到数据层。"""
    calls = []
    monkeypatch.setattr("app.up_list.set_auto",
                        lambda mid, auto: calls.append((int(mid), auto)) or True)
    r = client.post("/api/up-list/set-auto",
                    json={"mid": 42, "auto": True})
    assert r.get_json()["ok"]
    assert calls == [(42, True)]
    # 缺参数报 400
    assert client.post("/api/up-list/set-auto", json={"mid": 42}).status_code == 400


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


def test_frontend_audio_and_auto_and_badge():
    """2026-08-15 批次前端:仅音频选项 / UP 追更开关 / 已下载角标 / 设置开关。"""
    html = _tpl()
    assert '<option value="音频">' in html, "清晰度下拉应有仅音频"
    assert "upToggleAuto" in html, "UP 管理应有自动追更开关"
    assert "/api/up-list/set-auto" in html
    assert "dl-badge" in html, "列表项应有已下载角标"
    assert "config-save-danmaku" in html, "设置区应有弹幕开关"
    assert "config-auto-interval" in html, "设置区应有追更间隔"


def test_frontend_up_history_local():
    """2026-08-16:「查看历史」走本地 /api/up-history,不再全量打 B 站。"""
    html = _tpl()
    assert "/api/up-history" in html
    assert "查看历史(本地)" in html


# ---------- 2026-08-17 侧边栏改版 ----------

def test_frontend_sidebar_pages():
    """左侧菜单栏七个页面:下载记录/观看历史/关注账号/更新推送/收藏夹/稍后再看/设置。"""
    html = _tpl()
    for p in ("downloads", "history", "follows", "feed", "fav", "later", "settings"):
        assert f'data-page="{p}"' in html, f"缺侧边栏页面 {p}"
    for label in ("下载记录", "观看历史", "关注账号", "更新推送", "收藏夹", "稍后再看", "设置"):
        assert label in html


def test_frontend_no_emoji_icons():
    """图标全部用内联 SVG,不再用 emoji。"""
    html = _tpl()
    for e in ("🌙", "☀", "⚙", "🔍", "📋", "🟦", "📦", "💾", "📅", "📆",
              "↻", "⚡", "📜", "🎵", "⚠", "✓", "✗", "›", "↓"):
        assert e not in html, f"仍有 emoji 图标: {e}"
    assert "<svg" in html, "应有内联 SVG 图标"


def test_frontend_single_download_entry():
    """单视频/批量合并为一个入口,自动判断。"""
    html = _tpl()
    assert "data-dlmode" not in html, "不应再有单/批量双 tab"
    assert "smartParse" in html, "应有统一的解析入口(自动判断单个/批量)"


def test_frontend_unified_toolbar():
    """各列表页工具栏结构统一(刷新+搜索+视图切换同位置)。"""
    html = _tpl()
    n = html.count('class="lib-toolbar"')
    assert n >= 5, f"五个列表页都应有统一工具栏,当前 {n} 处"
    assert html.count("lib-search") >= 5
