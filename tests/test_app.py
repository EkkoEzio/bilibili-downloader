"""路由层行为测试:UP 清单自动下载(2026-08-18 改版)与 /api/stats(C3)。"""
import json
import threading
import time

import pytest


@pytest.fixture()
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _vid(bvid, ts, title="视频"):
    return {"bvid": bvid, "title": title, "uploader": "UP", "thumbnail": "",
            "view_at": ts, "url": f"https://www.bilibili.com/video/{bvid}"}


# ---------- /api/up-list:清单读取 ----------

def test_up_list_includes_fetched_at_and_quality(client, monkeypatch, tmp_path):
    """/api/up-list 返回 fetched_at 与默认清晰度,管理面板渲染用。"""
    import up_list as up_list_module
    monkeypatch.setattr(up_list_module, "UP_LIST_FILE", tmp_path / "up_list.json")
    up_list_module.add_up(1, "UPA", "")
    up_list_module.set_quality(1, "720P")
    up_list_module.mark_seen(1, "BVx", 100, "标题", fetched_at=1234)
    r = client.get("/api/up-list").get_json()
    assert r["ups"][0]["fetched_at"] == 1234
    assert r["ups"][0]["quality"] == "720P"


# ---------- /api/up-list/add:名称解析 + 只追「加入之后」的新投稿 ----------

def test_up_list_add_uses_user_card(client, monkeypatch, tmp_path):
    """添加 UP 时优先走用户卡片接口拿名称+头像(mid 人记不住,必须显示名称)。"""
    import up_list as up_list_module
    monkeypatch.setattr(up_list_module, "UP_LIST_FILE", tmp_path / "up_list.json")
    monkeypatch.setattr("app.bapi.fetch_up_card",
                        lambda mid: {"uname": "远古野", "face": "https://x/face.jpg"})
    monkeypatch.setattr("app.bapi.fetch_up_videos",
                        lambda mid, ps=10, pn=1: [])
    r = client.post("/api/up-list/add", json={"input": "46817116"})
    body = r.get_json()
    assert body["ok"]
    assert body["up"]["uname"] == "远古野"
    assert body["up"]["face"] == "https://x/face.jpg"

    # 存量无名称记录:重新添加同一个 mid 会补上名称
    up_list_module.UP_LIST_FILE.write_text(
        json.dumps({"ups": [{"mid": 46817116, "uname": "", "face": "", "added_at": 1}],
                    "last_seen": {}}), encoding="utf-8")
    r2 = client.post("/api/up-list/add", json={"input": "46817116"})
    assert r2.get_json()["up"]["uname"] == "远古野"


def test_up_list_add_baselines_last_seen(client, monkeypatch, tmp_path):
    """加入清单时以当前最新投稿为基线:只自动下载「加入之后」的新投稿,
    不会把 UP 的历史存货一口气全下回来。"""
    import up_list as up_list_module
    monkeypatch.setattr(up_list_module, "UP_LIST_FILE", tmp_path / "up_list.json")
    monkeypatch.setattr("app.bapi.fetch_up_card", lambda mid: None)
    monkeypatch.setattr("app.bapi.fetch_up_videos",
                        lambda mid, ps=10, pn=1: [_vid("BVold", 100)])
    r = client.post("/api/up-list/add", json={"input": "https://space.bilibili.com/42"})
    assert r.get_json()["ok"]
    last = up_list_module.get_last_seen(42)
    assert last["bvid"] == "BVold" and last["ts"] == 100
    assert last["fetched_at"], "基线要带拉取时间(自适应页大小用)"


def test_up_list_add_baseline_falls_back_to_now(client, monkeypatch, tmp_path):
    """拿不到最新投稿(限流/私密)时用当前时刻兜底,同样不回补历史存货。"""
    import up_list as up_list_module
    monkeypatch.setattr(up_list_module, "UP_LIST_FILE", tmp_path / "up_list.json")
    monkeypatch.setattr("app.bapi.fetch_up_card", lambda mid: None)
    monkeypatch.setattr("app.bapi.fetch_up_videos", lambda mid, ps=10, pn=1: [])
    before = int(time.time())
    r = client.post("/api/up-list/add", json={"input": "42"})
    assert r.get_json()["ok"]
    last = up_list_module.get_last_seen(42)
    assert last["ts"] >= before, "无投稿数据时以当前时刻为基线"


# ---------- /api/up-list/check:手动触发一轮检查 ----------

def test_up_list_check_runs_refresh_in_background(client, monkeypatch):
    """手动检查与后台自动下载同一逻辑(auto_refresh.refresh_once),异步执行。"""
    import app as app_module
    import auto_refresh
    done = threading.Event()
    calls = []

    def fake_refresh():
        calls.append(1)
        done.set()

    monkeypatch.setattr(app_module.auto_refresh, "refresh_once", fake_refresh)
    r = client.post("/api/up-list/check")
    assert r.get_json()["ok"]
    assert done.wait(2), "检查应在后台线程执行"
    assert calls == [1]


def test_up_list_check_never_concurrent(client, monkeypatch):
    """refresh_once 内部有锁,手动+后台不会叠着打 B 站接口(这里只验路由不炸)。"""
    import app as app_module
    monkeypatch.setattr(app_module.auto_refresh, "refresh_once", lambda: 0)
    assert client.post("/api/up-list/check").get_json()["ok"]


# ---------- /api/up-list/set-quality ----------

def test_up_list_set_quality_route(client, monkeypatch):
    """POST /api/up-list/set-quality {mid, quality} 落到数据层。"""
    calls = []
    monkeypatch.setattr("app.up_list.set_quality",
                        lambda mid, q: calls.append((int(mid), q)) or True)
    r = client.post("/api/up-list/set-quality", json={"mid": 42, "quality": "1080P"})
    assert r.get_json()["ok"]
    assert calls == [(42, "1080P")]
    # 缺参数报 400
    assert client.post("/api/up-list/set-quality", json={}).status_code == 400


# ---------- /api/task/<id>/reveal:在 Finder 中定位文件 ----------

def test_task_reveal_opens_file(client, monkeypatch):
    """找到文件时定位并选中该文件(按 UP 归档时也能定位)。

    具体平台命令归 files.reveal_path(见 test_filename_alignment),
    这里只验路由把正确的路径交给它。
    """
    from pathlib import Path
    revealed = []
    monkeypatch.setattr("app.downloader.find_task_files",
                        lambda tid: [Path("/dl/UP/视频.mp4")])
    monkeypatch.setattr("app.downloader.reveal_path",
                        lambda p: revealed.append(str(p)) or True)
    r = client.post("/api/task/abc/reveal").get_json()
    assert r["ok"] and r["found"] is True
    assert revealed == ["/dl/UP/视频.mp4"]


def test_task_reveal_falls_back_to_download_dir(client, monkeypatch):
    """找不到文件时退回打开下载目录,并如实回报 found=False。"""
    from pathlib import Path
    opened = []
    monkeypatch.setattr("app.downloader.find_task_files", lambda tid: [])
    monkeypatch.setattr("app.downloader.get_task", lambda tid: {"title": "x"})
    monkeypatch.setattr("app.downloader.open_dir",
                        lambda p: opened.append(str(p)) or True)
    import app as app_module
    monkeypatch.setattr(app_module.config, "DOWNLOAD_DIR", Path("/dl"))
    r = client.post("/api/task/abc/reveal").get_json()
    assert r["ok"] and r["found"] is False
    assert opened == ["/dl"]


# ---------- 我追的合集(2026-08-18) ----------

def test_fav_seasons_route(client, monkeypatch):
    monkeypatch.setattr("app.bapi.fetch_collected_seasons",
                        lambda: [{"id": 1761108, "title": "数理小品",
                                  "media_count": 30, "cover": "https://x/c.jpg"}])
    r = client.get("/api/fav-seasons").get_json()
    assert r["ok"] and r["seasons"][0]["id"] == 1761108


def test_routes_do_not_pollute_real_cache(client, monkeypatch):
    """路由测试不得把 mock 数据写进真实 data/cache。
    曾因 test_fav_seasons_route 未隔离 CACHE_DIR,每跑一次测试就把用户端
    追更合集缓存覆写成单条 mock——下次打开页面与真实数据对比必不一致,
    「查看最新」提示每次重启后必弹一次。"""
    from pathlib import Path
    real = Path(__file__).resolve().parent.parent / "data" / "cache" / "fav_seasons.json"
    snapshot = real.read_text(encoding="utf-8") if real.exists() else None
    monkeypatch.setattr("app.bapi.fetch_collected_seasons", lambda: [{"id": 999}])
    assert client.get("/api/fav-seasons").get_json()["ok"]
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert after == snapshot, "测试把 mock 数据写进了真实 data/cache"


def test_season_content_route(client, monkeypatch):
    monkeypatch.setattr("app.bapi.fetch_season_content",
                        lambda sid: [{"bvid": "BV1", "title": "t", "url": "u"}])
    r = client.get("/api/season-content?season_id=1761108").get_json()
    assert r["ok"] and r["items"][0]["bvid"] == "BV1"
    assert client.get("/api/season-content").status_code == 400


def test_tasks_clean_invalid_route(client, monkeypatch):
    monkeypatch.setattr("app.downloader.clean_invalid_records",
                        lambda: {"failed": 2, "invalid": 3, "removed": 5})
    r = client.post("/api/tasks/clean-invalid").get_json()
    assert r["ok"] and r["failed"] == 2 and r["invalid"] == 3 and r["removed"] == 5


# ---------- 扫码登录 ----------

def test_qrcode_image_uses_passed_url_not_new_key(client, monkeypatch):
    """/api/login/qrcode-image 必须用传入的 ?url= 画码。
    重新 qrcode_generate() 会产生新 key,图与前端轮询的 key 不一致,
    扫码确认了也没人认(一直「未扫码」直到过期)——回归 2026-08-19 评审。"""
    import bilibili_auth

    def _boom():
        raise AssertionError("image 接口不得重新 generate(会换 key)")

    monkeypatch.setattr(bilibili_auth, "qrcode_generate", _boom)
    r = client.get("/api/login/qrcode-image"
                   "?url=https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=x")
    assert r.status_code == 200 and r.mimetype == "image/png"
    # 无 url / 非 B 站域名(防钓鱼二维码)一律拒绝
    assert client.get("/api/login/qrcode-image").status_code == 400
    assert client.get("/api/login/qrcode-image?url=https://evil.com/phish").status_code == 400


def test_frontend_qrcode_img_passes_url():
    """前端二维码图应把 /api/login/qrcode 返回的 url 传给图片接口。"""
    html = _tpl()
    assert "/api/login/qrcode-image?url=${encodeURIComponent(r.url)}" in html


# ---------- 页面缓存:缓存优先 + 后台拉新(2026-08-18(e)) ----------

def test_cache_roundtrip_and_whitelist(client, tmp_path, monkeypatch):
    """save 写的缓存 /api/cache 读得到;不在白名单的键拒绝。"""
    import pagecache
    monkeypatch.setattr(pagecache, "CACHE_DIR", tmp_path)
    pagecache.save("follow_updates", {"ok": True, "items": [{"bvid": "BVx"}]})
    r = client.get("/api/cache/follow_updates").get_json()
    assert r["ok"] and r["data"]["items"][0]["bvid"] == "BVx"
    assert r["saved_at"] > 0
    assert client.get("/api/cache/evil_key").get_json()["ok"] is False


def test_follow_updates_writes_cache(client, monkeypatch, tmp_path):
    """数据端点成功返回时把结果写入缓存,前端下次打开秒显。"""
    import pagecache
    monkeypatch.setattr(pagecache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr("app.bapi.fetch_follow_updates",
                        lambda force=False: [{"bvid": "BV1", "title": "t"}])
    r = client.get("/api/follow-updates").get_json()
    assert r["ok"]
    cached = client.get("/api/cache/follow_updates").get_json()
    assert cached["ok"] and cached["data"]["items"][0]["bvid"] == "BV1"


def test_cache_empty_when_never_fetched(client, tmp_path, monkeypatch):
    import pagecache
    monkeypatch.setattr(pagecache, "CACHE_DIR", tmp_path)
    assert client.get("/api/cache/watchlater").get_json()["ok"] is False


# ---------- 7 天静默换新(2026-08-19:重启后首开不再必弹「查看最新」) ----------

def test_pagecache_purge_stale_discards_30d(client, tmp_path, monkeypatch):
    """启动清理:超 30 天没更新的缓存文件删除,其余保留。"""
    import os as _os
    import time as _time
    import pagecache
    monkeypatch.setattr(pagecache, "CACHE_DIR", tmp_path)
    fresh_f = tmp_path / "watchlater.json"
    fresh_f.write_text('{"saved_at": 1, "data": {}}', encoding="utf-8")
    old_f = tmp_path / "fav_seasons.json"
    old_f.write_text('{"saved_at": 1, "data": {}}', encoding="utf-8")
    old_ts = _time.time() - 31 * 86400
    _os.utime(old_f, (old_ts, old_ts))
    pagecache.purge_stale()
    assert not old_f.exists(), "超 30 天的缓存文件应被清理"
    assert fresh_f.exists(), "近期缓存文件不得误删"


def test_frontend_fast_apply_never_breaks_checking():
    """fast 无感替换必须带勾选守卫:用户已勾选条目时静默重渲染会错位勾选索引。"""
    html = _tpl()
    assert "state.checked.size === 0" in html, "fast 分支应检查无勾选才静默替换"


# ---------- /api/stats ----------

def test_stats_month_is_calendar_month(client, monkeypatch, tmp_path):
    """「本月」按自然月算,而不是近 30 天:上月末完成的任务不算本月。"""
    import datetime
    first = datetime.date.today().replace(day=1)
    month_start = int(time.mktime(first.timetuple()))
    monkeypatch.setattr("taskstore.list_tasks", lambda: [
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
    """前端整体包:结构(index.html) + 样式 + 全部脚本模块拼接。

    2026-08-19 前端三分离;2026-08-23 起 app.js 进一步拆成 ES modules
    (公共框架 lib.js + pages/ 页控制器)。既有断言语义不变——仍是
    「整个前端」。not in 断言同样覆盖全部文件,语义更严格(哪里都不该出现)。"""
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent
    parts = [base / "templates" / "index.html",
             base / "static" / "css" / "main.css",
             *sorted((base / "static" / "js").rglob("*.js"))]
    return "\n".join(p.read_text(encoding="utf-8") for p in parts)


def test_frontend_split_into_static_files():
    """前端三分离:index.html 不再内嵌 <style>/<script>(可缓存/可单测)。"""
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent
    html = (base / "templates" / "index.html").read_text(encoding="utf-8")
    assert "<style>" not in html and "<script>" not in html, \
        "样式/脚本应走 static/,index.html 只留结构"
    assert 'href="/static/css/main.css' in html
    assert 'src="/static/js/app.js' in html
    assert 'type="module"' in html, "脚本应以 ES module 入口加载"
    js = "\n".join(p.read_text(encoding="utf-8")
                   for p in sorted((base / "static" / "js").rglob("*.js")))
    assert "cachedLoad" in js, "脚本应完整迁移"
    assert "{{" not in js and "{%" not in js, "静态 JS 里不该有 Jinja 语法"


# ---------- window 桥完整性 ----------
# 拆 ES modules 后,内联 onclick 依赖 app.js 把函数挂到 window。这条链有两个
# 断点,且都躲得过「对拼接文本做子串断言」——名字在两个文件里都出现,缺的只是
# 一个 export 关键字 / 一行桥条目:
#   1. 模块忘了 export → 命名空间对象上取到 undefined → window.fn 是 undefined
#      → 内联 onclick 直接 TypeError(v2.1 重构时 newdl.batchDownload 真踩了,
#        「批量下载」按钮点了没反应,161 个测试全绿)
#   2. 新增内联 onclick 但忘了挂桥 → ReferenceError
# 下面两条测试分别锁住这两个方向。

def _js_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / "static" / "js"


def _strip_block_comments(src: str) -> str:
    """去掉 /* */ 块注释:注释里的示例代码不是真实引用(app.js 的桥说明里
    就有 onclick="xxx()" 这种占位)。"""
    import re
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def _window_bridge():
    """解析 app.js 的 window 桥,返回 [(全局名, 模块别名, 成员名)]。"""
    import re
    app_js = (_js_dir() / "app.js").read_text(encoding="utf-8")
    # `name: mod.member,` 或简写 `mod.member,`
    entries = re.findall(r"^\s*(?:(\w+):\s*)?(\w+)\.(\w+),\s*$", app_js, re.M)
    return [(alias or member, mod, member) for alias, mod, member in entries]


def _module_files():
    """app.js 的 `import * as X from './y.js'` → {别名: 文件路径}。"""
    import re
    app_js = (_js_dir() / "app.js").read_text(encoding="utf-8")
    return {alias: _js_dir() / rel for alias, rel
            in re.findall(r"import \* as (\w+) from '\./([\w/.]+)'", app_js)}


def test_frontend_window_bridge_targets_are_exported():
    """桥上每个 `mod.member` 都必须真的被该模块 export。

    未导出时命名空间对象上是 undefined,window.fn 也就是 undefined,
    内联 onclick 调用直接 TypeError——而子串断言完全看不出来。"""
    import re
    mods = _module_files()
    bridge = _window_bridge()
    assert len(bridge) > 30, f"桥条目解析异常(只解析到 {len(bridge)} 条)"
    missing = []
    for win_name, mod, member in bridge:
        f = mods.get(mod)
        if not f or not f.exists():
            continue
        src = f.read_text(encoding="utf-8")
        exported = (
            re.search(rf"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+{member}\b", src)
            or re.search(rf"export\s*\{{[^}}]*\b{member}\b[^}}]*\}}", src)
        )
        if not exported:
            missing.append(f"window.{win_name} <- {mod}.{member} ({f.name} 未 export)")
    assert not missing, "桥指向了未导出的成员(运行时是 undefined):\n" + "\n".join(missing)


def test_frontend_inline_handlers_are_bridged():
    """内联 on* 处理器引用的全局函数都必须挂在 window 桥上,否则 ReferenceError。"""
    import re
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent
    sources = [(base / "templates" / "index.html").read_text(encoding="utf-8")]
    sources += [_strip_block_comments(p.read_text(encoding="utf-8"))
                for p in sorted(_js_dir().rglob("*.js"))]

    called = set()
    for src in sources:
        for handler in re.findall(r'on(?:click|change|input)="([^"]*)"', src):
            # 前面不是 . 或字母的标识符才是全局调用(排除 this.x() / event.y())
            called.update(re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", handler))

    bridged = {win for win, _mod, _member in _window_bridge()}
    unbridged = sorted(called - bridged)
    assert not unbridged, ("内联处理器引用了未挂桥的函数:"
                           + ", ".join(f"{n}()" for n in unbridged))


def test_frontend_history_load_more():
    """观看历史支持「加载更早」:走服务端分页逐页追加,而不是一次拉完。"""
    html = _tpl()
    assert "maybeLoadMoreHistory" in html, "缺少加载更早入口"
    assert "/api/history?page=" in html, "历史应按 page 参数分页请求"


def test_frontend_grid_initial_scroll():
    """网格首屏要多渲染一行 + 不足一屏自动补块,否则滚动条不出现、无限滚卡死。"""
    html = _tpl()
    assert "maybeFillScreen" in html, "缺少首屏补块逻辑"
    assert "/ rowH) + 1" in html, "网格行数应 +1,保证首屏可滚动"


def test_frontend_modal_uses_theme_vars():
    """删除确认弹窗背景走主题变量,深色模式下文字才可读。"""
    html = _tpl()
    css_start = html.find(".modal {")
    assert css_start > 0
    assert "var(--bg-card)" in html[css_start:css_start + 200], "弹窗应使用 var(--bg-card)"


def test_frontend_audio_and_quality_config():
    """清晰度:批量下拉有仅音频;UP 清单可配每个 UP 的默认清晰度。"""
    html = _tpl()
    assert '<option value="音频">' in html, "清晰度下拉应有仅音频"
    assert "upSetQuality" in html, "UP 管理应有默认清晰度设置"
    assert "/api/up-list/set-quality" in html
    assert "dl-badge" in html, "列表项应有已下载角标"


def test_frontend_no_pending_confirm_flow():
    """加入清单即自动下载:待确认横幅/角标/接口都不应再出现。"""
    html = _tpl()
    for gone in ("/api/pending", "/api/up-updates", "/api/up-history",
                 "downloadPending", "dismissPending", "upToggleAuto"):
        assert gone not in html, f"追更确认制残留:{gone}"


def test_frontend_feed_three_views():
    """UP 更新页三个视图:关注动态 / 关注列表 / 自动下载清单。"""
    html = _tpl()
    for src in ("bili", "all", "mine"):
        assert f'data-src="{src}"' in html, f"缺 UP 更新视图 {src}"
    for label in ("关注动态", "关注列表", "自动下载"):
        assert label in html


def test_frontend_sidebar_pages():
    """左侧菜单六个页面,顺序:下载记录/观看历史/关注动态/追更合集/我的收藏/稍后再看;
    左下角设置在前、账号在后。"""
    html = _tpl()
    tags = [f'data-page="{p}"' for p in
            ("downloads", "history", "feed", "seasons", "fav", "later")]
    pos = [html.find(t) for t in tags]
    assert all(p >= 0 for p in pos), f"缺侧边栏页面:{[t for t, p in zip(tags, pos) if p < 0]}"
    assert pos == sorted(pos), "侧边栏页面顺序不对"
    for label in ("下载记录", "观看历史", "关注动态", "追更合集", "我的收藏", "稍后再看"):
        assert label in html
    foot = html[html.find('class="sidebar-foot"'):]
    assert "openDrawer('account')" in foot, "左下角应有账号入口"
    assert "openDrawer('settings')" not in html, "设置已并回账号抽屉,不再单独入口"


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
    """各列表页工具栏结构统一(搜索+视图切换同位置)。"""
    html = _tpl()
    assert html.count("lib-search") >= 5
    assert html.count("view-btn") >= 5


def test_frontend_settings_autosave():
    """设置并回账号抽屉:改动即生效、无保存按钮,每项控件 onchange 直接落库。"""
    html = _tpl()
    assert "saveConfigField" in html
    assert "保存设置" not in html, "不应再有保存按钮"
    acct = html[html.find('id="dpanel-account"'):]
    assert 'id="config-dir-input"' in acct, "设置内容应在账号抽屉内"
    assert "save-flash" in html, "保存成功应有轻提示"


def test_frontend_downloads_play_button():
    """下载记录:封面右下角播放按钮 → 定位该视频下载到的文件。"""
    html = _tpl()
    assert "vi-play" in html
    assert "revealTask" in html
    assert "/api/task/' + taskId + '/reveal" in html or "revealTask('" in html


def test_frontend_fav_seasons_and_grid():
    """追更合集独立一级页(seasonEnter),收藏夹/合集都有网格卡片。"""
    html = _tpl()
    assert 'data-page="seasons"' in html, "追更合集应为独立页面"
    assert "seasonEnter" in html
    assert "/api/fav-seasons" in html
    assert "/api/season-content" in html
    assert "fav-card" in html, "收藏夹/合集应有网格卡片"
    assert 'id="fav-view-btn"' in html and 'style="display:none;" id="fav-view-btn"' not in html, \
        "视图切换在收藏夹首屏应常驻"
    assert "favEnterSeason" not in html, "旧的合集入口已并入 seasonEnter"


def test_frontend_clean_invalid_option():
    """「清除失败/失效记录」独立常驻页头,直接清理;删除/清除不再弹二次选择弹窗。"""
    html = _tpl()
    assert "清除全部记录" in html
    assert "清除失败/失效记录" in html
    assert "clearInvalidRecords" in html
    assert "/api/tasks/clean-invalid" in html
    assert "del-modal" not in html, "删除选择弹窗应移除(选中后直接按按钮删除)"


def test_frontend_cache_first_loading():
    """统一缓存优先:先读 /api/cache 秒显,后台拉新;慢源有变化提示「查看最新」。"""
    html = _tpl()
    assert "cachedLoad" in html
    assert "/api/cache/" in html
    assert "fresh-hint" in html
    assert "applyFresh" in html
    # 五个数据源都走统一加载器
    for fn in ("loadFollowBili", "loadFollowAll", "favLoadFolders", "loadSeasons", "loadWatchlater"):
        assert f"cacheKey" in html, "加载器应声明 cacheKey"


def test_frontend_fast_sources_auto_apply():
    """快数据源(追更合集/收藏夹/稍后再看,单接口毫秒级)拉新后无感替换渲染,
    不弹「查看最新」提示条——提示条只为串行慢源(关注动态/关注列表)保留。
    起因:重启后首开追更合集必弹一次提示(对比基准是上次会话的缓存,隔天必过期),
    对几百毫秒就能返回的接口,无感替换才对齐官网行为。"""
    html = _tpl()

    def opts_of(cache_key):
        i = html.find(f"cacheKey: '{cache_key}'")
        assert i >= 0, f"缺少数据源 {cache_key}"
        start = html.rfind("cachedLoad({", 0, i)
        return html[start:html.find("});", i)]

    assert "opts.fast" in html, "cachedLoad 应实现 fast 分支"
    for key in ("fav_seasons", "fav_folders", "watchlater"):
        assert "fast: true" in opts_of(key), f"{key} 是快源,应 fast: true 无感替换"
    for key in ("follow_updates", "followings"):
        assert "fast: true" not in opts_of(key), f"{key} 是慢源,应保留提示条"


def test_frontend_uplist_drawer_removed():
    """UP 清单抽屉已删:管理功能并入「自动下载」视图。"""
    html = _tpl()
    assert "dpanel-uplist" not in html, "抽屉应删除"
    assert "管理 UP 清单" not in html
    assert 'id="feed-mine-tools"' in html, "自动下载视图应有管理条(添加+立即检查)"
    assert 'id="up-check-btn"' in html
    assert 'id="up-add-input"' in html
    assert "up-card mgr" in html, "UP 卡片应带清晰度/移除控件"


def test_frontend_unified_toolbar_everywhere():
    """六个页面统一:搜索 + 刷新 + 视图切换;收藏夹首屏也支持搜索。"""
    html = _tpl()
    assert html.count("lib-search") >= 6
    assert html.count("view-btn") >= 6
    assert 'onclick="loadDownloads()"' in html, "下载记录也应有刷新按钮"
    assert "搜索收藏夹 / 标题 / UP 主" in html


def test_launcher_kills_old_instance():
    """启动.command 启动前先关闭旧实例(否则端口被占,新代码不生效)。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "启动.command").read_text(encoding="utf-8")
    assert "lsof -ti tcp:5050" in src
    assert "kill $OLD_PID" in src
    assert "kill -9" in src, "杀不掉时要强制结束"


def test_frontend_queue_merged_into_downloads():
    """下载队列并入下载记录页:无队列抽屉,列表含进行中任务卡,操作条按页切换动作。"""
    html = _tpl()
    assert "dpanel-queue" not in html, "队列抽屉应删除"
    assert "openDrawer('queue')" not in html
    assert "taskCardHtml" in html, "进行中/失败任务应渲染成进度卡"
    assert "updateTaskCard" in html, "SSE 应就地更新任务卡"
    assert 'id="dl-badge"' in html, "进行中任务数显示在侧边栏「下载记录」角标"
    assert 'id="ab-del-rec"' in html and 'id="ab-del-file"' in html
    assert "delSelected" in html and "clearAllRecords" in html


def test_frontend_switch_back_while_bg_fetch_in_flight():
    """关注列表↔关注动态切换:缓存渲染不得被在途后台请求阻断(回归 2026-08-18)。"""
    html = _tpl()
    assert "_bgInFlight" in html, "同源后台请求应有在途保护,且不阻断缓存渲染"
    assert "_biliChecking" not in html, "旧的整函数级锁会卡死切回,应移除"
