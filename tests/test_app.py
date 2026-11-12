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


def _stub_auto_refresh_round(monkeypatch):
    """给 auto_refresh 的一轮检查彻底断网,并给「打 B 站接口」的入口装计数桩。

    返回 fetch 调用记录。清单里放了 1 个 UP,所以 refresh_once 真跑起来时
    `fetch_up_videos` 必然被调至少一次 —— 「记录为空」就是「本次未发起任何
    B 站请求」的证据(2.9),而不是「反正也没什么可请求的」。

    桩同时起断网作用:未修复代码那个排队线程在测试释放锁后会真的跑一轮,
    不打桩就会去打真实的 arc/search。
    """
    import auto_refresh
    fetch_calls = []

    def fake_fetch(mid, ps=10, pn=1):
        fetch_calls.append((int(mid), ps, pn))
        return []          # 空页 = 限流形态:不入队、不推进 last_seen,一轮秒结束

    monkeypatch.setattr(auto_refresh.up_list, "list_ups",
                        lambda: [{"mid": 1, "uname": "UP"}])
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen_all", lambda: {})
    monkeypatch.setattr(auto_refresh.bapi, "fetch_up_videos", fake_fetch)
    monkeypatch.setattr(auto_refresh.time, "sleep", lambda s: None)
    return fetch_calls


def _join_manual_check_threads(timeout=5.0):
    """等路由起的后台检查线程收尾(线程名 bd-manual-check,daemon)。

    未修复代码无条件 start() 一个,它会阻塞排队在 `auto_refresh._lock` 上。
    不等它跑完就退出测试,monkeypatch 撤桩后它会去打真实 B 站接口;
    「零调用」这类断言也会变成竞态。调用前必须已经释放锁,否则是死等。
    """
    for th in threading.enumerate():
        if th.name == "bd-manual-check":
            th.join(timeout=timeout)


def test_up_list_check_never_concurrent(client, monkeypatch):
    """检查进行中再触发一次:路由必须如实回报 started=False,且零 B 站请求。

    Property 4: Bug Condition —— 后来者跳过而非排队。
    对应 isBugCondition_B4:已有一轮 refresh 正在运行。

    改写自旧版:旧版 docstring 写着「refresh_once 内部有锁,手动+后台不会叠着打
    B 站接口(这里只验路由不炸)」,却 monkeypatch 了 `refresh_once → lambda: 0`
    —— 把唯一会碰锁的那段整个换掉了,锁的行为压根没进入断言范围,修复前后都绿。
    这是 B4 躲过 183 个用例的原因之一。

    新版不 patch `refresh_once`,改成主线程真的持住 `auto_refresh._lock` 来模拟
    「一轮正在跑」,再走完整路由:

      未修复:路由无条件 `threading.Thread(...).start()` + `return jsonify({"ok": True})`
              → 响应里根本没有 started 字段;那个后台线程阻塞排队在锁上,
                主线程一释放它就完整再跑一轮(每个 UP 一次 arc/search、UP 间隔 5 秒)。
      修复后:`auto_refresh.is_running()` 为真 → 直接返回 started=False,不起线程。

    Validates: Requirements 2.9, 2.10
    """
    import auto_refresh
    fetch_calls = _stub_auto_refresh_round(monkeypatch)

    lock = auto_refresh._lock
    assert lock.acquire(timeout=5), "取不到 auto_refresh._lock(疑有用例泄漏了锁)"
    try:
        body = client.post("/api/up-list/check").get_json()
        assert body["ok"] is True, "「已有检查在进行中」不是错误,ok 仍应为 True"
        assert body.get("started") is False, (
            "一轮正在跑时路由必须如实回报 started=False(2.10);"
            f"实际响应 {body} —— 未修复代码无条件 return {{'ok': True}},"
            "前端无从区分,只能固定禁用按钮 30 秒装样子")
        assert fetch_calls == [], (
            f"本次触发不得发起任何 B 站请求(2.9),实际 {fetch_calls}")
    finally:
        # 先释放锁、再等排队线程收尾:顺序反了就是死等(它正卡在这把锁上)
        lock.release()
        _join_manual_check_threads()


def test_up_list_check_reports_already_running(client, monkeypatch):
    """已有一轮在跑时:路由如实回报 started=False,且压根不起后台线程。

    Property 4: Bug Condition —— 后来者跳过而非排队。

    与上一条的分工:上一条用**真实锁**跑通「路由 + auto_refresh」整链;这一条把
    原子预约换成失败桩,单独锁住**路由自己的分支** —— 它必须先成功预约
    才能起线程，不能用 is_running() 的“先检查、后启动”制造 TOCTOU 窗口。

    Validates: Requirements 2.10
    """
    import app as app_module
    calls = []
    monkeypatch.setattr(app_module.auto_refresh, "try_reserve_refresh", lambda: None)
    monkeypatch.setattr(app_module.auto_refresh, "refresh_once",
                        lambda: calls.append(1) or 0)

    body = client.post("/api/up-list/check").get_json()
    # 未修复代码会起一个 bd-manual-check 线程;先等它收尾,计数断言才不是竞态
    _join_manual_check_threads()

    assert body["ok"] is True, "「已有检查在进行中」不是错误,ok 仍应为 True"
    assert body.get("started") is False, (
        f"已有一轮在跑时必须回报 started=False(2.10),实际响应 {body}")
    assert "已有检查在进行中" in (body.get("message") or ""), (
        f"响应要如实说明本次未启动新一轮,实际 message={body.get('message')!r}")
    assert calls == [], (
        "预约失败时路由不该起后台线程、更不该调 refresh_once;"
        f"实际调用 {len(calls)} 次")


def test_up_list_check_double_click_runs_one_round(client, monkeypatch):
    """连点两次「立即检查新视频」:只跑一轮,B 站请求数等于一轮的量而不是两轮。

    Property 4: Bug Condition —— 后来者跳过而非排队(端到端)。
    对应 isBugCondition_B4:已有一轮 refresh 正在运行。

    这是 B4 的核心承诺——「连点 N 次不再叠加 N 轮」——唯一一条从 HTTP 入口
    量到实际请求次数的用例。前两条各自验的是半条链:一条持真实锁走路由,一条把
    is_running 换成谓词桩;都没有让**两次真实请求**先后打进同一个进程,也都没有
    数过 arc/search 到底被调了几次。这条数。

    确定性从哪来(这是本用例唯一的设计难点):路由是异步起线程的,如果只是
    「连发两次 POST 然后断言第二次 started is False」,第一次那个 bd-manual-check
    线程可能还没抢到 `_lock`,第二次请求就已经读到 is_running() == False ——
    断言变成拼调度的竞态,时绿时红。

    这里用一个闸门把竞态消掉:`fetch_up_videos` 桩在**第一次**被调用时 set
    `entered` 再 `release.wait(...)` 卡住。被调到就意味着第一轮已经进了锁里
    (acquire → list_ups → fetch_new → fetch),所以主线程 `entered.wait()` 返回
    之后,「已有一轮在跑」是既成事实而不是期望。此时再发第二次请求,started
    必然是 False。放行后 join,再数总调用次数。

      未修复:第二次请求那个线程阻塞排队在 `with _lock:` 上,第一轮释放后它完整
              补跑一轮 → 总调用 2×len(UPS)(30 个 UP 的清单就是 60 次 arc/search、
              两段各两分半的连续请求),且响应里根本没有 started 字段。
      修复后:`is_running()` 为真 → 直接回 started=False、不起线程;
              即便撞上那个极窄窗口起了线程,`_lock.acquire(blocking=False)` 也会
              让它立即 return 0 —— 无论走哪条路,总调用都是一轮的量。

    不挂死的保证:所有等待都带 timeout(`entered.wait(5)` / `release.wait(5)` /
    `join(5)`),`finally` 里无条件放行闸门并 join,最后兜底收锁。就算路由退化成
    同步执行,最坏也是 5 秒后干净地断言失败,不会把测试套件挂住。

    Validates: Requirements 2.9, 2.10
    """
    import auto_refresh
    UPS = [{"mid": 1, "uname": "UP1"}, {"mid": 2, "uname": "UP2"},
           {"mid": 3, "uname": "UP3"}]
    ONE_ROUND = len(UPS)      # 空页 → fetch_new 首页就 break → 每个 UP 恰好 1 次

    fetch_calls = []
    entered, release = threading.Event(), threading.Event()

    def gated_fetch(mid, ps=10, pn=1):
        """第一轮的第一次调用卡在这里,把「一轮正在跑」变成既成事实。"""
        first = not fetch_calls
        fetch_calls.append((int(mid), pn))
        if first:
            entered.set()
            release.wait(5)   # 有上限:路由若变同步,本条秒级变红而不是挂死
        return []             # 空页 = 限流形态:不入队、不推进 last_seen

    monkeypatch.setattr(auto_refresh.up_list, "list_ups", lambda: UPS)
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen_all", lambda: {})
    monkeypatch.setattr(auto_refresh.bapi, "fetch_up_videos", gated_fetch)
    monkeypatch.setattr(auto_refresh.time, "sleep", lambda s: None)

    assert auto_refresh.is_running() is False, \
        "起点必须是「没人在跑」(疑有用例泄漏了 auto_refresh._lock)"
    try:
        first = client.post("/api/up-list/check").get_json()
        assert first["ok"] is True
        assert first.get("started") is True, (
            f"没人在跑时第一次触发应真的启动一轮,实际响应 {first}")

        assert entered.wait(5), (
            "第一轮应已进入锁内并打到 arc/search 桩 —— 没进来说明路由没起线程"
            "或 refresh_once 没拿到锁")

        second = client.post("/api/up-list/check").get_json()
        assert second["ok"] is True, "「已有检查在进行中」不是错误,ok 仍应为 True"
        assert second.get("started") is False, (
            "第一轮确凿在跑时,第二次触发必须如实回报 started=False(2.10);"
            f"实际响应 {second}")
        assert "已有检查在进行中" in (second.get("message") or ""), (
            f"第二次响应要说明本次未启动新一轮,实际 message={second.get('message')!r}")
        assert len(fetch_calls) == 1, (
            "第二次触发不得立刻发起任何 B 站请求(2.9);此刻只该有第一轮卡住的"
            f"那一次,实际 {fetch_calls}")
    finally:
        # 先放行闸门、再等线程收尾:顺序反了就是死等(它正卡在 release 上)。
        # 桩还在,所以补跑的部分全程离线且瞬间结束,不会打真实接口。
        release.set()
        _join_manual_check_threads()
        if auto_refresh._lock.locked():   # 兜底,避免拖垮后续碰锁的用例
            auto_refresh._lock.release()

    assert len(fetch_calls) == ONE_ROUND, (
        f"连点两次只应跑一轮:arc/search 总调用应为 {ONE_ROUND} 次(UP 数),"
        f"两轮是 {2 * ONE_ROUND} 次;实际 {len(fetch_calls)} 次 {fetch_calls}")
    assert [mid for mid, _ in fetch_calls] == [1, 2, 3], (
        f"一轮里每个 UP 恰好被拉一次、顺序不乱,实际 {fetch_calls}")


def test_up_list_check_returns_without_waiting_for_refresh(client, monkeypatch):
    """没人在跑时:路由起后台线程并**立即**返回,不等这一轮跑完。

    Property 10: Preservation —— 单轮检查的执行语义(路由侧)。

    与 `test_up_list_check_runs_refresh_in_background` 的分工:那条只证明
    「refresh_once 被调到了」,它的桩瞬间返回 —— 路由改成同步调用也照样绿,
    所以「立即返回、不阻塞前端」这半句其实没有任何用例守着。一轮检查是
    30 个 UP × (一次 arc/search + 5 秒间隔) ≈ 数分钟,同步化就是把前端挂死。

    这里把 refresh_once 换成「进门后卡住」的桩:响应能在它还没返回时就拿到,
    就是异步的证据。桩用带超时的 wait 而不是无限阻塞 —— 万一路由真变成同步,
    这条会在几秒后干净地失败,不会挂死测试套件。

    Validates: Requirements 3.16
    """
    import app as app_module
    entered, release, returned = (threading.Event(), threading.Event(),
                                  threading.Event())
    calls = []

    def blocking_refresh():
        calls.append(threading.current_thread().name)
        entered.set()
        release.wait(5)      # 有上限:同步化时本条秒级变红,而不是挂住
        returned.set()
        return 0

    monkeypatch.setattr(app_module.auto_refresh, "refresh_once", blocking_refresh)
    try:
        t0 = time.monotonic()
        body = client.post("/api/up-list/check").get_json()
        elapsed = time.monotonic() - t0

        assert body["ok"] is True
        assert entered.wait(2), "refresh_once 应当被后台线程调起"
        assert app_module.auto_refresh.is_running() is True, (
            "路由返回 started=True 时预约必须仍覆盖整个 worker 生命周期")
        assert app_module.auto_refresh.run_refresh_with_status("scheduled") == 0, (
            "手动 worker 已预约时，定时触发必须立即跳过而不是抢跑")
        assert calls == ["bd-manual-check"], (
            "并发定时包装器不得再次调用 monkeypatch 的零参数 refresh_once")
        assert not returned.is_set(), (
            "响应必须在这一轮还没跑完时就返回 —— 路由同步执行 refresh_once 会把"
            "前端挂住整轮(30 个 UP 约数分钟)")
        assert elapsed < 1.0, f"路由应毫秒级返回,实际耗时 {elapsed:.2f}s"
    finally:
        # 先放行、再等线程收尾:撤桩后它若还在跑就会去打真实 B 站接口
        release.set()
        _join_manual_check_threads()


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
    """二维码 URL 正确，登录状态与认证 mutation 也必须有请求所有权守卫。"""
    html = _tpl()
    assert "/api/login/qrcode-image?url=${encodeURIComponent(r.url)}" in html

    login_js = (_js_dir() / "login.js").read_text(encoding="utf-8")
    refresh = _js_func_body(login_js, "refreshLogin")
    begin = _js_func_body(login_js, "beginLoginStatusRequest")
    current = _js_func_body(login_js, "loginStatusIsCurrent")

    assert "invalidateLoginStatus()" in begin
    assert "new AbortController()" in begin
    assert "sequence === _statusSequence" in current
    assert "controller === _statusController" in current
    assert "getJSON('/api/login/status', { signal: controller.signal })" in refresh
    owner_guard = (
        "if (!loginStatusIsCurrent(sequence, controller)) "
        "return STATUS_SUPERSEDED;"
    )
    assert owner_guard in refresh
    assert refresh.index(owner_guard) < refresh.index("renderLoginStatus(status)")
    assert refresh.index("renderLoginStatus(status)") < refresh.index("emitAuthChange(status)")
    assert "isAbortError(error) || !loginStatusIsCurrent(sequence, controller)" in refresh
    assert "if (loginStatusIsCurrent(sequence, controller)) _statusController = null" in refresh
    assert "_loggedIn = false" not in refresh, \
        "旧请求的 catch 不得绕过 owner guard 直接覆盖登录状态"

    begin_mutation = _js_func_body(login_js, "beginAuthMutation")
    mutation_current = _js_func_body(login_js, "authMutationIsCurrent")
    assert "MUTATION_SUPERSEDED = Symbol('auth-mutation-superseded')" in login_js
    assert "_mutationSequence++" in begin_mutation
    assert "invalidateLoginStatus()" in begin_mutation
    assert "sequence === _mutationSequence" in mutation_current

    mutation_requests = {
        "logout": "postEmpty('/api/login/logout')",
        "pollLogin": "getJSON('/api/login/qrcode/poll?key='",
        "useBrowser": "postEmpty('/api/login/use-browser')",
        "uploadCookieTxt": "postText('/api/login/cookie-txt', text)",
    }
    mutation_bodies = {}
    for function_name, request_marker in mutation_requests.items():
        body = _js_func_body(login_js, function_name)
        mutation_bodies[function_name] = body
        claim = "const mutationSequence = beginAuthMutation();"
        assert body.index(claim) < body.index(request_marker), \
            f"{function_name} 必须在认证请求前取得共享 mutation owner"
        first_owner_check = body.index("authMutationIsCurrent(mutationSequence)")
        assert body.index(request_marker) < first_owner_check < body.index("refreshLogin()"), \
            f"{function_name} 必须在响应后、刷新 status 前淘汰迟到 mutation"
        assert body.count("authMutationIsCurrent(mutationSequence)") >= 3, \
            f"{function_name} 必须在响应、刷新后和副作用前重复校验 owner"
        assert "MUTATION_SUPERSEDED" in body, \
            f"{function_name} 的迟到流程必须静默返回 superseded sentinel"
        first_announce = body.index("announce(")
        assert body.rfind(
            "authMutationIsCurrent(mutationSequence)", 0, first_announce
        ) != -1, f"{function_name} 在 announce 前必须再次校验 owner"

    # Edge 与 cookies.txt 共用同一个单调序列；后开始者取得新 owner 后，
    # 两种逆序完成中的旧流程都会在响应后检查处被淘汰，而不能另起 status。
    browser = mutation_bodies["useBrowser"]
    cookie = mutation_bodies["uploadCookieTxt"]
    for older, newer in ((browser, cookie), (cookie, browser)):
        assert "const mutationSequence = beginAuthMutation();" in older
        assert "const mutationSequence = beginAuthMutation();" in newer
        assert older.index("authMutationIsCurrent(mutationSequence)") \
            < older.index("refreshLogin()")

    logout = mutation_bodies["logout"]
    assert logout.index("if (status === STATUS_SUPERSEDED) return") \
        < logout.index("document.getElementById('qrcode-img').innerHTML = ''")
    assert logout.rfind(
        "authMutationIsCurrent(mutationSequence)",
        0,
        logout.index("document.getElementById('qrcode-img').innerHTML = ''"),
    ) != -1
    poll = mutation_bodies["pollLogin"]
    assert poll.index("if (status === STATUS_SUPERSEDED) return") \
        < poll.index("imgBox.innerHTML = ''")
    assert poll.rfind(
        "authMutationIsCurrent(mutationSequence)",
        0,
        poll.index("imgBox.innerHTML = ''"),
    ) != -1
    show_qr = _js_func_body(login_js, "showQrcode")
    assert "_qrController = controller" in show_qr
    assert "beginAuthMutation()" not in show_qr, \
        "二维码生成不改变认证上下文；只有可能保存会话的 poll 才取得 mutation owner"
    assert "invalidateLoginStatus()" not in show_qr, \
        "二维码生成控制器与登录 status 控制器必须保持独立"


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
    """「本月」按自然月算,而不是近 30 天:上月末完成的任务不算本月(需求 3.25)。

    顺带锁住需求 3.26:今天完成的任务必须计入「今日」。

    样本时间刻意以**自然日 / 自然月起点**为基准构造,而不是「当前时刻往前推」。
    旧写法用 `int(time.time()) - 3600` 表示「今天」,B6 把「今日」从滚动 24 小时
    改成自然日 00:00 之后,该时间戳在本地 00:00–01:00 之间跑会落到昨天
    (`< day_start`)→ `day_count` 变 0 → `assert r["day_count"] == 1` 挂掉,
    也就是一条只在凌晨复现的 flake。改用 `day_start + 60` 后期望值不再随运行
    时刻漂移:它在 00:00–00:01 之间跑是「未来 1 分钟」,但 `compute_stats` 只做
    `finished >= day_start` 比较、不校验未来,结果恒定。

    已知残余竞态(记录在案,不做代码改造):本测试算 `date.today()` 与
    `compute_stats` 算 `date.today()` 之间若正好跨过零点(微秒级窗口),两边
    `day_start` 相差一天,理论上仍可 flake。**不为此给 `compute_stats` 注入统一
    时钟** —— 那等于在 `taskstore` 里为测试开一道时钟接缝,是 design.md 明确
    否掉的方案;真要消除只能在测试侧 monkeypatch `taskstore.datetime.date` 把
    `today()` 固定住。
    (实测补充:`datetime.date.today()` 会跟随被 monkeypatch 的 `time.time`,
    即测试侧的 `time.time()` 与 `compute_stats` 侧的 `date.today()` 本就是同一个
    时钟 —— 正因如此,把样本锚到自然日起点而非「当前时刻往前推」才是消除凌晨
    flake 的正确做法。)

    另一条**既有**不稳定窗口(与本次改造无关,改造前后表现一致,B6 修完即消失):
    滚动 24 小时的旧口径下,每月 1 日 00:00–11:59 运行时 `day_start` 落在上月最后
    一天,上月末样本(`month_start - 43200`,即上月末 12:00)会被算进「今日」→
    `day_count == 2`;其中 00:00–00:59 还会让旧的 `now - 3600` 样本落到上月 →
    `month_count == 0`。自然日口径下 `day_start` 恒为今天 00:00,上月末样本永远
    早于它,这个窗口随 B6 修复自动消失。

    覆盖需求 3.25(本月自然月)与 3.26(今天完成的计入今日)。
    """
    import datetime
    today = datetime.date.today()
    day_start = int(time.mktime(today.timetuple()))                    # 本地自然日 00:00
    month_start = int(time.mktime(today.replace(day=1).timetuple()))   # 本地自然月 1 日 00:00
    monkeypatch.setattr("taskstore.list_tasks", lambda: [
        {"status": "done", "downloaded_bytes": 100, "finished_at": day_start + 60},        # 今天 00:01
        {"status": "done", "downloaded_bytes": 100, "finished_at": month_start - 43200},   # 上月末 12:00
    ])
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", tmp_path)
    r = client.get("/api/stats").get_json()
    assert r["ok"]
    assert r["total_count"] == 2
    assert r["day_count"] == 1
    assert r["month_count"] == 1, "上月末完成的任务不属于本月(自然月口径)"


def test_stats_day_is_calendar_day(client, monkeypatch, tmp_path):
    """「今日」按本地自然日 00:00 算,而不是滚动 24 小时(缺陷 B6)。

    口径矛盾:同一份统计里「本月」走 `datetime.date(y, m, 1) → time.mktime`
    (自然月),而「今日」走 `now - 86400`(滚动 24 小时)。后者让昨晚 23:00
    完成的任务在今早 08:00 仍被算进「今日」——用户看到的「今日 3」里有 2 条
    是昨天的,而同一个面板的「本月」却严格不含上月末的任务。

    样本刻意以**自然日起点**为基准构造(`day_start + 60` / `day_start - 60`),
    而不是「当前时刻往前推」:后者在凌晨会翻到昨天,让期望值随运行时刻变化。

    不断言 `month_count`:「昨天 23:59」这条样本在每月 1 日 00:00–00:59 运行时
    属于上月,`month_count` 的期望值会随运行时刻变化。day 与 month 的断言拆到
    两条独立测试里(month 见 `test_stats_month_is_calendar_month`),各自的样本
    集才恒定。

    已知残余竞态:本测试算 `date.today()` 与 `compute_stats` 算 `date.today()`
    之间若正好跨过零点(微秒级窗口),两个样本都会落在「今天」之前 →
    `day_count == 0` 而 flake。不为此给 `compute_stats` 注入统一时钟(那等于在
    `taskstore` 里为测试开时钟接缝),这是 design.md 的决定;真要消除得靠
    monkeypatch `taskstore.datetime.date` 固定 `today()`。
    """
    import datetime
    day_start = int(time.mktime(datetime.date.today().timetuple()))  # 本地自然日 00:00
    monkeypatch.setattr("taskstore.list_tasks", lambda: [
        {"status": "done", "downloaded_bytes": 100, "finished_at": day_start + 60},  # 今天 00:01
        {"status": "done", "downloaded_bytes": 100, "finished_at": day_start - 60},  # 昨天 23:59
    ])
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", tmp_path)
    r = client.get("/api/stats").get_json()
    assert r["ok"]
    assert r["total_count"] == 2, "两条 done 样本都应被统计到,否则 day_count 的断言会空过"
    assert r["day_count"] == 1, "昨天 23:59 完成的任务不属于今日(自然日口径)"


def test_stats_totals_count_only_done(client, monkeypatch, tmp_path):
    """总量与周期计数只统计 `done` 任务(需求 3.27 前半 + 3.15 的统计侧对应)。

    B6 只改 `day_start` 的算法,`if t.get("status") != "done": continue` 这道
    闸门必须逐位不变。既有的 month / day 用例样本全是 `done`,`total_count == 2`
    在「非 done 也被计入」的实现下同样成立 —— 也就是说这条不变量此前没有任何
    用例守着,补上。

    非 done 样本刻意都带 `downloaded_bytes: 999` 且 `finished_at` 落在今天,
    一旦闸门失效,`total_count` / `total_bytes` / `month_count` / `day_count`
    四个字段会同时穿帮。
    """
    import datetime
    day_start = int(time.mktime(datetime.date.today().timetuple()))
    monkeypatch.setattr("taskstore.list_tasks", lambda: [
        {"status": "done", "downloaded_bytes": 100, "finished_at": day_start + 60},
        {"status": "queued", "downloaded_bytes": 999, "finished_at": day_start + 60},
        {"status": "downloading", "downloaded_bytes": 999, "finished_at": day_start + 60},
        {"status": "failed", "downloaded_bytes": 999, "finished_at": day_start + 60},
        {"status": "cancelled", "downloaded_bytes": 999, "finished_at": day_start + 60},
        {"status": "interrupted", "downloaded_bytes": 999, "finished_at": day_start + 60},
    ])
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", tmp_path)  # 空目录 → disk_bytes 0,不干扰兜底
    r = client.get("/api/stats").get_json()
    assert r["ok"]
    assert r["total_count"] == 1, "只有 done 计入总下载数"
    assert r["total_bytes"] == 100, "非 done 的 downloaded_bytes 不得累加"
    assert r["month_count"] == 1, "非 done 不计入本月"
    assert r["day_count"] == 1, "非 done 不计入今日"


def test_stats_total_bytes_falls_back_to_disk(client, monkeypatch, tmp_path):
    """`done` 任务缺 `downloaded_bytes` 时,总字节用下载目录**递归**磁盘实测值兜底。

    覆盖需求 3.27 后半,即 `total_bytes if total_bytes else disk_bytes` 这个表达式。
    `tests/test_downloader.py::test_disk_bytes_counts_subdirs` 只断言了
    `disk_bytes` 字段本身,没断言 `total_bytes` 会取它兜底,故补这一条。

    两条样本分别覆盖「字段缺失」与「字段显式为 0」——后者同样落在 `else` 分支
    (`0` 是假值),是历史记录里真实出现过的形态。
    """
    import datetime
    day_start = int(time.mktime(datetime.date.today().timetuple()))
    (tmp_path / "顶层.mp4").write_bytes(b"a" * 100)
    up_dir = tmp_path / "某UP主"
    up_dir.mkdir()
    (up_dir / "归档.mp4").write_bytes(b"b" * 250)  # 子目录也要算进去
    monkeypatch.setattr("taskstore.list_tasks", lambda: [
        {"status": "done", "finished_at": day_start + 60},                        # 无该字段
        {"status": "done", "downloaded_bytes": 0, "finished_at": day_start + 60},  # 显式 0
    ])
    monkeypatch.setattr("app.config.DOWNLOAD_DIR", tmp_path)
    r = client.get("/api/stats").get_json()
    assert r["ok"]
    assert r["total_count"] == 2
    assert r["disk_bytes"] == 350, "磁盘用量递归统计,子目录文件也算"
    assert r["total_bytes"] == 350, "任务侧字节为 0 时用磁盘实测值兜底"


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
    # 页头工具栏抽成 Jinja 组件后，断言仍针对最终渲染出的整页，
    # 不把宏调用当成缺失的 DOM。
    import app as app_module
    with app_module.app.test_request_context():
        from flask import render_template
        template_html = render_template("index.html")
    parts = [base / "static" / "css" / "main.css",
             *sorted((base / "static" / "js").rglob("*.js"))]
    return template_html + "\n" + "\n".join(p.read_text(encoding="utf-8") for p in parts)


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
    """共享原生确认框使用主题背景，深色模式下内容保持可读。"""
    html = _tpl()
    assert '<dialog class="confirm-dialog" id="confirm-dialog"' in html
    css_start = html.find(".confirm-dialog{")
    assert css_start > 0
    assert "var(--bg-card)" in html[css_start:css_start + 300], \
        "确认框应使用 var(--bg-card)"


def test_frontend_audio_and_quality_config():
    """清晰度:批量下拉有仅音频;UP 清单可配每个 UP 的默认清晰度。"""
    html = _tpl()
    assert '<option value="音频">' in html, "清晰度下拉应有仅音频"
    assert "upAutoChanged" in html and "up-list/set-quality" in html, \
        "UP 管理应有默认清晰度设置(合并进自动下载控件)"
    assert "/api/up-list/set-quality" in html
    assert "dl-badge" in html, "列表项应有已下载角标"


def test_frontend_no_pending_confirm_flow():
    """加入清单即自动下载:待确认横幅/角标/接口都不应再出现。"""
    html = _tpl()
    for gone in ("/api/pending", "/api/up-updates", "/api/up-history",
                 "downloadPending", "dismissPending", "upToggleAuto"):
        assert gone not in html, f"追更确认制残留:{gone}"


def test_frontend_feed_and_subs_are_separate_pages():
    """关注动态与关注 UP 是两个独立页面。

    回归:两者曾合并在「关注动态」的三个视图里,浏览行为和订阅管理互相干扰;
    拆开后 feed 只负责投稿流,订阅管理(加 UP/自动下载/取关回关)在 subs 页。
    """
    html = _tpl()
    assert 'data-page="feed"' in html and 'data-page="subs"' in html, \
        "应有两个独立页面容器"
    assert 'data-src="bili"' not in html and 'data-src="ups"' not in html, \
        "feed 页不再有来源切换 seg"
    for label in ("关注动态", "关注 UP"):
        assert label in html

    subs_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    assert "upToggleFollow" in subs_js and "relation/modify" in subs_js, \
        "订阅页应有取关/回关能力"
    assert "local_ups" in subs_js, "订阅页应消费 local_ups 字段"
    assert "upAutoChanged" in subs_js, "订阅页应有自动下载开关"

    feed_js = (_js_dir() / "pages" / "feed.js").read_text(encoding="utf-8")
    assert "follow-updates" in feed_js, "feed 页只拉投稿流"
    assert "local_ups" not in feed_js, "feed 页不应再承载订阅管理"


def test_frontend_topbar_pages():
    """顶栏八个页面,顺序:首页推荐/关注动态/关注 UP/追更合集/我的收藏/稍后再看/
    观看历史/下载记录;右侧操作区设置在前、账号在后,新建下载常驻。"""
    html = _tpl()
    assert 'class="topbar"' in html, "应使用顶栏导航(2026-11-10 终选 B 版)"
    tags = [f'data-page="{p}"' for p in
            ("home", "feed", "subs", "seasons", "fav", "later", "history", "downloads")]
    pos = [html.find(t) for t in tags]
    assert all(p >= 0 for p in pos), f"缺顶栏页面:{[t for t, p in zip(tags, pos) if p < 0]}"
    assert pos == sorted(pos), "顶栏页面顺序不对"
    for label in ("首页推荐", "关注动态", "关注 UP", "追更合集", "我的收藏", "稍后再看", "观看历史", "下载记录"):
        assert label in html
    right = html[html.find('class="tb-right"'):]
    assert "openDrawer('account')" in right, "顶栏右侧应有账号入口"
    assert "openDrawer('newdl')" in right, "顶栏右侧应有新建下载入口"
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
    """两类清理入口常驻页头，并统一使用共享原生确认框。"""
    html = _tpl()
    assert "清除已结束记录" in html
    assert "清除失败/失效记录" in html
    assert "clearInvalidRecords" in html
    assert "/api/tasks/clean-invalid" in html
    assert '<dialog class="confirm-dialog" id="confirm-dialog"' in html
    assert "confirmAction" in html
    assert "del-modal" not in html, "旧删除选择弹窗不应恢复"


def test_frontend_cache_first_loading():
    """统一缓存优先:先读 /api/cache 秒显,后台拉新;慢源有变化提示「查看最新」。"""
    html = _tpl()
    assert "cachedLoad" in html
    assert "/api/cache/" in html
    assert "fresh-hint" in html
    assert "applyFresh" in html
    # 五个数据源都走统一加载器
    for fn in ("loadFollowBili", "loadMergedUps", "favLoadFolders", "loadSeasons", "loadWatchlater"):
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
    """UP 清单抽屉已删:管理功能在独立的「关注 UP」页。"""
    html = _tpl()
    assert "dpanel-uplist" not in html, "抽屉应删除"
    assert "管理 UP 清单" not in html
    assert 'id="up-check-btn"' in html, "订阅页应有「立即检查」按钮"
    assert 'id="up-add-input"' in html
    assert "upAutoControlHtml" in html, "UP 卡片应带清晰度/移除控件"


def test_frontend_unified_toolbar_everywhere():
    """八个页面统一:搜索 + 刷新 + 视图切换;收藏夹首屏也支持搜索。"""
    html = _tpl()
    assert html.count('class="search-input lib-search"') == 8
    assert html.count('class="ghost icon-only view-btn"') == 8
    for handler in ("loadHomeFeed()", "loadDownloads()", "refreshLib()",
                    "refreshSubs()", "seasonRefresh()", "favRefresh()"):
        assert f'onclick="{handler}"' in html, f"缺少刷新入口:{handler}"
    # 总览只按名称过滤,占位符不再承诺 UP 主/标题(2026-11-11 复查)
    assert "搜索收藏夹名称" in html


def test_launcher_reports_port_conflict_and_only_cleans_own_child():
    """端口冲突:同源旧实例 INT→TERM→KILL 递进优雅停止,未知进程只报告;
    ready helper 受单调 deadline 且无 PID 能力。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "启动.command").read_text(encoding="utf-8")
    assert 'PORT="${BD_PORT:-5050}"' in src
    assert 'lsof -nP -iTCP:"$PORT" -sTCP:LISTEN' in src
    assert "kill -9" not in src, "不得以 -9 拼写强制终止进程"
    assert "CHILD_PID" not in src and "PID_FILE" not in src and "$!" not in src
    assert "pidfile" not in src.lower()
    assert "wait_for_owned_child" not in src and "trap cleanup" not in src
    # 发信号的前置条件是归属判定:工作目录为本目录且命令行在运行 app.py。
    assert '[[ "$pid_cwd" == "$SCRIPT_DIR" && "$pid_cmd" == *app.py* ]]' in src, \
        "接管旧实例前必须现查进程归属"
    assert "被非本项目进程占用。启动器不会终止未知进程。" in src
    assert src.count('pid_cwd="$(lsof -a -p "$pid" -d cwd -Fn') == 2, \
        "归属判定现查两次:接管前一次,SIGKILL 前对每个 PID 再核验一次"
    assert src.count("kill -") == 3, "全脚本只允许 INT/TERM/KILL 三处信号"
    assert 'kill -INT ${OWN_PIDS[@]+"${OWN_PIDS[@]}"}' in src
    assert 'kill -TERM ${OWN_PIDS[@]+"${OWN_PIDS[@]}"}' in src
    assert 'kill -KILL "$pid"' in src, "SIGKILL 只发给逐个再核验过的存活 PID"
    assert src.index('kill -KILL "$pid"') \
        > src.rindex('pid_cwd="$(lsof -a -p "$pid" -d cwd -Fn'), \
        "SIGKILL 必须发生在第二次归属核验之后"
    assert 'exec "$PYTHON" app.py' in src, \
        "launcher 必须由 app.py 原位替换，让终端信号和退出码直接属于 Python"
    assert '"$PYTHON" app.py &' not in src, "服务本身不得作为裸 PID 后台 child 启动"
    assert "for _ in $(seq 1 60)" not in src, "固定次数不能证明 60 秒墙钟上限"
    assert "deadline = time.monotonic() + 60.0" in src
    assert src.count("remaining = deadline - time.monotonic()") >= 3
    assert "timeout=min(1.0, remaining)" in src
    assert "time.sleep(min(1.0, remaining))" in src
    assert '["open", base_url]' in src and "timeout=remaining" in src
    assert "辅助进程不会停止服务" in src
    assert src.index('status_url = base_url + "/api/config"') \
        < src.index('["open", base_url]') < src.index('exec "$PYTHON" app.py')
    assert src.rstrip().endswith('exec "$PYTHON" app.py')
    assert 'PYTHON="$SCRIPT_DIR/.venv/bin/python"' in src


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

# ---------- 标准列表渲染:勾选下标必须是 state.items 的原始下标(缺陷 B1) ----------

def _js_func_body(src: str, name: str) -> str:
    """截出 JS 源码里 `function <name>(...) { ... }` 的函数体(含花括号)。

    整包子串断言看不出「谁在调用」——`visiblePairs(` 出现在拼接串里不等于
    renderDefaultList 真的用了它。这里按花括号配平把断言收窄到具体函数。
    目标函数体内的模板串只含配平的 `${...}`,计数法足够。"""
    i = src.find(f"function {name}(")
    assert i >= 0, f"lib.js 里找不到函数 {name}"
    params_start = src.index("(", i)
    paren_depth = 0
    signature_end = -1
    for j in range(params_start, len(src)):
        if src[j] == "(":
            paren_depth += 1
        elif src[j] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                signature_end = j + 1
                break
    assert signature_end > params_start, f"函数 {name} 参数括号不配平"
    start = src.index("{", signature_end)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"函数 {name} 花括号不配平")


def test_frontend_default_list_uses_stable_business_id():
    """标准列表的渲染身份必须来自业务内容，不能来自数组位置。

    搜索、分页、刷新和 SSE 重排都可能改变 ``state.items`` 的位置。可见条目
    因此必须携带 ``itemBusinessId(it)``，后续选择与批量操作只消费该稳定 ID。

    Validates: Requirements 2.1, 2.2, 2.3
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    rdl = _js_func_body(lib_js, "renderDefaultList")
    vp = _js_func_body(lib_js, "visiblePairs")

    # pairs 只有一个构造入口，且保持 state.items 的展示顺序。
    assert "visiblePairs(q)" in rdl
    assert "state.items.forEach((it, index)" in vp
    assert "pairs.push({ it, id: itemBusinessId(it) })" in vp, \
        "可见条目必须携带稳定业务 ID，而不是过滤结果或数组位置"

    # 任何以 idx/index 作为条目身份的旧实现都不得复活。
    assert "pairs.push({ it, idx" not in vp
    assert "pairs.push({ it, index" not in vp
    assert "data-libidx" not in lib_js

    # 初次渲染与无限滚推进复用同一套身份规则。
    for fn in ("renderDefaultList", "advanceDefaultChunk"):
        assert "visiblePairs(" in _js_func_body(lib_js, fn), \
            f"{fn} 应复用 visiblePairs()，不得自建位置身份"

# ---------- 勾选与批量操作:B1 修复必须逐位保持的既有正确行为 ----------
# 下面这批断言锁的是**当前已经正确**的部分,不是待修的缺陷:
# 缺陷 B1 只错在 renderDefaultList() 构造 pairs 时把「第几个命中」当原始下标,
# 消费方(renderLibList / delSelected / libDownload / toggleLibCheck /
# libToggleAll / updateLibCount)与两处勾选守卫(cachedLoad 的快源无感替换、
# downloads 的推迟重拉)语义一直是对的。修 pairs 构造时它们一字都不该动 ——
# 所以在未修复代码上先把基线钉死,修完重跑必须全绿。


def _js_listener_body(src: str, marker: str) -> str:
    """截出 document.addEventListener('click', ...) 这类匿名处理器的源码片段。

    封面点选切换勾选是委托到 document 上的匿名箭头函数,没有函数名,
    `_js_func_body` 用不上;按标志串定位到该处理器闭合的 `});` 即可。"""
    i = src.find(marker)
    assert i >= 0, f"源码里找不到标志串 {marker}"
    end = src.find("});", i)
    assert end > i, f"标志串 {marker} 之后找不到处理器闭合"
    return src[i:end]


def test_frontend_check_consumers_resolve_stable_ids():
    """复选框只发出稳定 ID，批量操作经唯一 Map 反查当前条目。

    DOM 仍限定“当前可见且已勾选”的操作范围；搜索隐藏的选择不会被提交。
    刷新或排序只会替换 Map 中同一业务 ID 的值，不会把操作转移给其他条目。

    Validates: Requirements 3.1, 3.2
    """
    import re
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")

    # renderLibList 是 data-libid 的唯一发出点，并以同一 ID 恢复选择状态。
    rll = _js_func_body(lib_js, "renderLibList")
    assert 'data-libid="${escapeAttr(libId)}"' in rll
    assert "toggleLibCheck(this.dataset.libid, this.checked)" in rll
    assert "state.checked.has(libId)" in rll
    emitters = {p.relative_to(_js_dir()).as_posix()
                for p in sorted(_js_dir().rglob("*.js"))
                if 'data-libid="' in p.read_text(encoding="utf-8")}
    assert emitters == {"lib.js"}, \
        f"data-libid 应只由 lib.js 的 renderLibList 发出，现在还有：{emitters}"

    # 唯一选择解析器从可见 DOM 取 ID，再经 itemById 反查；绝不索引数组。
    selected = _js_func_body(lib_js, "selectedVisibleItems")
    assert "input[data-libid]:checked" in selected
    assert "state.itemById.get(String(input.dataset.libid || ''))" in selected
    assert ".filter(Boolean)" in selected
    assert "state.items[" not in selected

    # 删除和批量下载共享上述解析器，且删除完成后才清空选择集。
    ds = _js_func_body(dl_js, "delSelected")
    assert "selectedVisibleItems()" in ds
    assert ".map(item => item.task_id)" in ds
    assert "/api/task/delete" in ds
    assert re.findall(r"state\.checked\.(\w+)", ds) == ["clear"]

    ld = _js_func_body(dl_js, "libDownload")
    assert "selectedVisibleItems()" in ld
    assert "state.checked" not in ld, \
        "批量下载只能处理可见 DOM 解析出的条目，不能遍历含隐藏选择的 Set"


def test_frontend_check_toggles_stay_dom_scoped():
    """单选、全选、封面点选和计数都使用稳定 ID，并只作用于可见 DOM。

    Validates: Requirements 3.3, 3.4
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")

    # 单条切换只接受当前 Map 中存在的 ID，陈旧 DOM ID 会被取消。
    tlc = _js_func_body(lib_js, "toggleLibCheck")
    assert "state.itemById.has(id)" in tlc
    assert "state.checked.add(id)" in tlc and "state.checked.delete(id)" in tlc
    assert "findVisibleCheckbox(id)" in tlc
    assert "cb.checked = next" in tlc
    assert "updateLibCount()" in tlc

    # “全选”遍历当前可见卡片，将 data-libid 写入唯一选择集。
    lta = _js_func_body(lib_js, "libToggleAll")
    assert ".page.active .video-item" in lta
    assert "state.items" not in lta, \
        "全选不得遍历被搜索隐藏的条目"
    assert "cb.dataset.libid" in lta
    assert "state.checked.add(id)" in lta and "state.checked.delete(id)" in lta
    assert "updateLibCount()" in lta

    # 封面点击先翻转 DOM，再将同一稳定 ID 交给统一切换函数。
    thumb = _js_listener_body(lib_js, ".video-item .thumb-wrap")
    assert "input.vi-check" in thumb
    assert "cb.checked = !cb.checked" in thumb
    assert "toggleLibCheck(cb.dataset.libid, cb.checked)" in thumb

    # 操作条计数口径仍是当前可见且勾选的 DOM，而不是全部历史选择。
    ulc = _js_func_body(lib_js, "updateLibCount")
    assert "input[data-libid]:checked" in ulc
    assert "state.checked" not in ulc
    assert ".lib-count" in ulc and "updateActionBar()" in ulc


def test_frontend_fast_apply_guard_lives_in_cached_load():
    """快数据源无感替换的勾选守卫一直在 cachedLoad 里,B1 修复不得动它。

    Property 7: Preservation —— 无搜索词时的勾选与批量操作。

    观察未修复代码(static/js/lib.js 的 cachedLoad):
        if (opts.fast && state.checked.size === 0) apply(r2);
        else showFreshHint(pageName, () => { ... });
    即快源拉到新数据时,只有勾选集为空才静默重渲染;用户已有勾选就退回
    「查看最新」提示条,不打断选择。守卫用的是 state.checked.size(含被搜索
    隐藏的勾选),范围比 DOM 计数更保守 —— 修复后仍应保持这个保守口径。

    既有的 test_frontend_fast_apply_never_breaks_checking 对整包做子串断言,
    看不出这个守卫落在谁身上;这里收窄到 cachedLoad 函数体,守卫被挪走或
    被换成 DOM 计数都会被抓到。

    锁住:3.5(快源后台拉到新数据且用户已有勾选 → 退回「查看最新」提示条)

    Validates: Requirements 3.5
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    cl = _js_func_body(lib_js, "cachedLoad")
    assert "opts.fast && state.checked.size === 0" in cl, \
        "cachedLoad 的无感替换分支应带 state.checked.size === 0 守卫"
    guard_at = cl.index("opts.fast && state.checked.size === 0")
    assert "showFreshHint(" in cl[guard_at:], \
        "守卫不成立时应退回 showFreshHint(「查看最新」提示条),而不是照样重渲染"


def test_frontend_downloads_uses_single_aggregate_stream_and_deferred_render():
    """下载状态只有一个聚合 SSE；健康时停轮询，有选择时延后整体重绘。

    稳定 ID 使任务 Map 可以持续接收事件，而 DOM 正在选择时只推迟重绘。
    清空选择后从最新 Map 快照补绘，不再维护“每任务 SSE + 固定轮询”双轨。

    Validates: Requirements 3.6
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")

    # 可见选择守卫与延迟重绘都使用稳定 ID DOM。
    visible = _js_func_body(dl_js, "hasVisibleChecks")
    assert "input[data-libid]:checked" in visible
    commit = _js_func_body(dl_js, "commitTaskCollection")
    assert "forceRender && hasVisibleChecks()" in commit
    assert "_dirty = true" in commit

    # 全应用只有一个聚合 EventSource；兼容入口不会创建每任务连接。
    ensure = _js_func_body(dl_js, "ensureTaskStream")
    assert "new EventSource('/api/tasks/events')" in ensure
    assert dl_js.count("new EventSource(") == 1
    assert "_activeSSE" not in dl_js
    assert "/api/progress/" not in dl_js
    subscribe = _js_func_body(dl_js, "subscribeProgress")
    assert "ensureTaskStream()" in subscribe and "EventSource" not in subscribe

    # snapshot 建立 revision，patch 只接受严格连续版本并支持增删。
    snapshot = _js_func_body(dl_js, "handleTaskSnapshot")
    assert "_taskRevision = revision" in snapshot
    assert "replaceTaskMap(payload.tasks)" in snapshot
    patch = _js_func_body(dl_js, "handleTaskPatch")
    assert "revision !== _taskRevision + 1" in patch
    assert "_taskById.set(taskId, task)" in patch
    assert "_taskById.delete(String(rawId))" in patch
    assert "_taskRevision = revision" in patch

    # 健康流期间轮询直接返回；仅断线/超时才请求全量快照。
    poll = _js_func_body(dl_js, "pollTasks")
    assert "if (!force && streamIsRecent()) return;" in poll
    assert "getJSON('/api/tasks'" in poll

    # 选择清零后从最新任务 Map 补上被推迟的重绘。
    hook_at = dl_js.index("onChecksEmpty:")
    hook = dl_js[hook_at:dl_js.index("},", hook_at)]
    assert "_dirty" in hook and "_dirty = false" in hook
    assert "commitTaskCollection(taskSnapshot(), { forceRender: true })" in hook
    ulc = _js_func_body(lib_js, "updateLibCount")
    assert "n === 0 && _itemExtras.onChecksEmpty" in ulc


def test_frontend_default_list_inits_shown_before_window():
    """非搜索态始终渲染 state.items 的 shown 前缀，且先初始化窗口。

    Validates: Requirements 3.1, 3.2, 3.3
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    rdl = _js_func_body(lib_js, "renderDefaultList")
    vp = _js_func_body(lib_js, "visiblePairs")

    assert "index < state.shown" in vp, \
        "非搜索态必须按原顺序限制在 state.shown 前缀"
    shown_at = rdl.find("state.shown = curChunk()")
    window_at = rdl.find("visiblePairs(q)")
    assert shown_at >= 0, "renderDefaultList 应负责首屏窗口初始化"
    assert window_at >= 0, "renderDefaultList 应通过 visiblePairs(q) 取窗"
    assert shown_at < window_at, \
        "state.shown 必须在 visiblePairs(q) 读取窗口之前初始化"


def test_frontend_visible_pairs_equals_prefix_slice():
    """visiblePairs('') 保序选取 shown 前缀，并为每项计算稳定业务 ID。

    数组下标只用于窗口边界，绝不进入 pair、DOM 或选择状态。

    Validates: Requirements 3.1, 3.2, 3.3
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    vp = _js_func_body(lib_js, "visiblePairs")

    assert "state.items.forEach((it, index)" in vp
    for bad in (".sort(", ".reverse(", ".filter(", ".slice("):
        assert bad not in vp, f"visiblePairs 内不得 {bad} 重排 state.items"

    assert "index < state.shown" in vp
    assert "pairs.push({ it, id: itemBusinessId(it) })" in vp
    assert "pairs.push({ it, index" not in vp
    assert "pairs.length" not in vp, \
        "不得用命中计数器构造身份"

# ---------- 已下载判定的键规则:前后端必须是同一条(缺陷 B3) ----------

def test_frontend_download_key_shared_rule():
    """「已下载」判定的键规则前后端绑定:分隔符常量相等 + 两侧取 page 的方式不写反。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)。

    前后端天然是两份运行时,无法共享代码;纪律的落地方式是把分隔符提到两侧各自的
    唯一来源(后端 extras.DL_KEY_SEP / 前端 static/js/util.js 的 DL_KEY_SEP),
    再用测试把两个常量绑定比较 —— 沿用本项目 TASK_ACTIVE ↔ taskstore.ACTIVE 与
    quality.js ↔ up_list.QUALITY_CHOICES 的既有做法。

    这条测试同时抓两类漂移,它们都不会在运行时报错、只会让角标与批量跳过各说各话:

      1. **少了 page 维度**:未修复现场 static/js/pages/downloads.js:16 是
         `let _doneBvids = new Set();`,集合只按 BV 建
         (`tasks.filter(...).map(t => bvFromUrl(t.url))`),于是 P1 下完之后
         同 BV 的全部分 P 都挂上「已下载」角标,newdl.batchDownload() 里
         `lib.isUrlDownloaded(it.url)` 把 P2–P5 全过滤掉、提示「所选项均已下载过」
         (1.7 的真实现场:resolve_playlist 对多 P 视频产出的正是 `?p=N` 形态)。
         → 用「旧名 _doneBvids 不得再出现」+「必须出现 dlKey(...)」两侧夹住。

      2. **两侧取 page 的方式写反**:任务侧只能读 `t.page`(/api/tasks 的任务对象
         已含该字段;任务 url 常不带 `?p=`,解析它会得到 1),查询侧只能解析 URL
         (isDownloaded 的入参只有 URL)。这个不对称是刻意的,写反了判定照样全错。
         → 断言 `dlKey(bv, t.page || 1)`(建集合)与 `dlKey(bv, pageFromUrl(url))`
           (查集合)逐字出现。

      3. **分隔符漂移**:后端 `f"{bv}:{page}"` 配前端 `bv + '#p' + page`,集合永远
         对不上。→ regex 提取前端字面值与后端常量比较。

    键规则由任务 9.1(后端)/ 9.3、9.4(前端)引入;落地前守卫断言先挂。

    Validates: Requirements 2.6, 2.7, 2.8
    """
    import re
    import extras
    html = _tpl()
    util_js = (_js_dir() / "util.js").read_text(encoding="utf-8")
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")

    # 1) 分隔符常量绑定:后端 extras.DL_KEY_SEP ↔ 前端 util.js 的 DL_KEY_SEP
    assert hasattr(extras, "DL_KEY_SEP"), (
        "后端 extras.DL_KEY_SEP 尚未引入(任务 9.1):键规则没有唯一来源可绑定")
    m = re.search(r"DL_KEY_SEP\s*=\s*['\"]([^'\"]*)['\"]", util_js)
    assert m, ("static/js/util.js 未找到 DL_KEY_SEP 定义(前端键规则的唯一来源);"
               "前端若把分隔符散落在拼接处,两侧漂移不会有任何报错")
    assert m.group(1) == extras.DL_KEY_SEP, (
        f"键分隔符前后端不一致:后端 {extras.DL_KEY_SEP!r} / 前端 {m.group(1)!r} "
        "—— 角标与批量跳过会各说各话")

    # 2) 前端键规则与后端同层同源:util.js 导出,downloads.js 只 import 不复制
    for name in ("dlKey", "pageFromUrl"):
        assert re.search(rf"export\s+(?:function|const|let|var)\s+{name}\b", util_js), (
            f"static/js/util.js 应导出 {name}(与 bvFromUrl 同层的键规则唯一来源)")
        assert f"function {name}" not in dl_js, (
            f"downloads.js 不得自建第二份 {name},必须从 ../util.js 引入")
    imp = re.search(r"import\s*\{([^}]*)\}\s*from\s*'\.\./util\.js'", dl_js, re.S)
    assert imp, "downloads.js 应从 '../util.js' 引入工具函数"
    imported = {n.strip() for n in imp.group(1).split(",")}
    assert {"dlKey", "pageFromUrl"} <= imported, (
        f"downloads.js 应从 util.js 引入 dlKey / pageFromUrl,现在只有:{sorted(imported)}")

    # 3) 任务侧读 t.page 建集合;查询侧解析 URL 查集合 —— 两侧取法必须不对称
    assert "dlKey(bv, t.page || 1)" in dl_js, (
        "已下载集合应按 (BV, 分P) 建键、任务侧读 t.page(缺失兜底 1);"
        "任务 url 常不带 ?p=,从它解析会把所有任务都算成 P1")
    assert "dlKey(bv, pageFromUrl(url))" in dl_js, (
        "isDownloaded 应用 (BV, URL 的分P) 查集合;查询侧只能从 URL 解析")

    # 4) 只按 BV 建集合的旧名必须绝迹(整个前端范围)
    assert "_doneBvids" not in html, (
        "仍有只按 BV 建的已下载集合 _doneBvids(缺陷 B3 现场):"
        "合集下完 P1 后其余分 P 全被当已下载而跳过")


# ---------- B3 端到端:合集下完 P1 后,其余分 P 必须真的能入队(缺陷 B3) ----------

def test_batch_download_enqueues_remaining_parts_end_to_end(client, monkeypatch, tmp_path):
    """1.7 的真实现场跑通一遍:多 P 合集已下载 P1,提交 P2–P5 必须 4 条全入队。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)。

    上面几条测试各自只锁一段:`is_downloaded` 的判定粒度(test_downloader.py)、
    前后端键规则一致(test_frontend_download_key_shared_rule)。它们全绿也不等于
    用户那条路走得通 —— 1.7 的后果是「一条都不入队 + 提示『所选项均已下载过』」,
    而这个提示是**过滤 + 提交**两段串起来的结果。这条把整条链一次跑完:

        POST /api/batch/parse
          → resolve_playlist 产出 `?p=N` 形态的条目(多 P 视频的真实输出)
          → 用 is_downloaded 过滤(前端 lib.isUrlDownloaded 的后端同构面)
          → POST /api/batch/download
          → start_batch 建任务 + 起线程
          → 断言 count == 4、分 P 分布恰好是 {2,3,4,5}

    未修复时链条在第二段就断:`is_downloaded` 只比 BV 号,P1 的 done 任务让
    P2–P5 全部命中 → 过滤后 items 为空 → 前端 `toast('所选项均已下载过')`,
    请求压根发不出去,count 恒为 0。

    两处打桩,都只为断网,不改被测逻辑:
      - `resolve_playlist` 会真的调 yt-dlp 抓 B 站;桩掉后返回 flat 模式对多 P
        视频的真实形态(`?p=N` 的 url + 「第 N 集」兜底标题,**没有 page 字段**
        —— page 由 newdl.batchDownload 从 `?p=` 现算,这里照同一规则算)。
      - `_run_download` 会真的下载;桩成 no-op 记账。`start_batch` 本身不联网
        (不像 start_download 会先 parse),所以桩掉它就足够全程离线。
    落盘走 conftest 已重定向的 TASKS_FILE,下载目录另指 tmp_path,真实 data/ 与
    真实下载目录全程不碰。

    Validates: Requirements 2.6, 2.7
    """
    import downloader
    import extras
    import taskstore

    bv = "BV1xx411c7mD"
    base = f"https://www.bilibili.com/video/{bv}"
    label = "1080P"

    monkeypatch.setattr("app.config.DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)

    # ---- 打桩 1:resolve_playlist(断网),形态照 parsing.resolve_playlist 的真实产出
    monkeypatch.setattr("app.downloader.resolve_playlist", lambda url: {
        "kind": "list", "title": "某合集",
        "items": [{"title": f"第 {n} 集", "url": f"{base}?p={n}", "duration": 60}
                  for n in range(1, 6)]})

    # ---- 打桩 2:_run_download(断网)。批量线程只调它,桩成 no-op 记账
    dispatched = []
    all_dispatched = threading.Event()

    def fake_run_download(task_id, url, label_, formats, page):
        dispatched.append((task_id, url, page))
        if len(dispatched) >= 4:
            all_dispatched.set()

    monkeypatch.setattr(downloader, "_run_download", fake_run_download)

    before = set(taskstore._tasks)
    try:
        # ---- 第 1 段:/api/batch/parse 产出 5 个 ?p=N 条目
        parsed = client.post("/api/batch/parse", json={"url": base}).get_json()
        assert parsed["ok"]
        items = parsed["items"]
        assert [it["url"] for it in items] == [f"{base}?p={n}" for n in range(1, 6)], \
            "多 P 视频的条目 URL 必须带 ?p=N —— 这是 B3 判定拿到分 P 的唯一途径"

        # ---- 第 2 段:P1 已下载完成
        taskstore._tasks["e2e_done_p1"] = {
            "task_id": "e2e_done_p1", "status": "done", "url": f"{base}?p=1",
            "label": label, "page": 1, "title": "第 1 集",
            "progress": 100, "error": "", "cancel": False,
        }

        # 过滤:前端 newdl.batchDownload 的 `items.filter(it => !isUrlDownloaded(it.url))`,
        # 后端同构面就是 is_downloaded。P1 命中被跳过,P2–P5 必须全部留下
        skipped = [it for it in items if downloader.is_downloaded(it["url"])]
        todo = [it for it in items if not downloader.is_downloaded(it["url"])]
        assert [it["url"] for it in skipped] == [f"{base}?p=1"], \
            "只有已完成的 P1 该被跳过(3.13)"
        assert len(todo) == 4, (
            "P2–P5 必须全部留下;未修复时这里是 0 条 → 前端弹「所选项均已下载过」,"
            "一条都不入队(1.7)")

        # ---- 第 3 段:提交。page 照 newdl.batchDownload 从 ?p= 现算(与后端同规则)
        payload = [{"url": it["url"], "title": it["title"],
                    "page": extras.page_from_url(it["url"]), "duration": it["duration"]}
                   for it in todo]
        assert [it["page"] for it in payload] == [2, 3, 4, 5]

        body = client.post("/api/batch/download",
                           json={"items": payload, "label": label}).get_json()

        # ---- 第 4 段:count 就是 1.7 的验收点
        assert body["ok"]
        assert body["count"] == 4, (
            f"提交 4 个未下载分 P 应全部入队,实际 count={body['count']}")
        assert len(body["task_ids"]) == 4

        # start_batch 的去重(`find_active_by_url(url, label) and page == 1`)不得吃掉它们:
        # 左操作数只认活跃态,P1 那条是 done;而且 page != 1 时右操作数恒假,
        # 多 P 条目压根走不到 continue。两条都点名断言,免得将来把 `and page == 1` 去掉
        # 后静默退化成「同 URL 只准有一个任务」。
        for it in payload:
            assert taskstore.find_active_by_url(it["url"], label) is None, \
                "P1 是 done(非活跃态),不该拦住任何分 P"
            assert it["page"] != 1, "多 P 条目 page > 1,去重条件的 `page == 1` 恒假"

        # 新建任务的分 P 分布恰好是 {2,3,4,5} —— 「入队了 4 条」不等于「入队的是对的 4 条」
        new_tasks = [taskstore._tasks[tid] for tid in body["task_ids"]]
        assert sorted(t["page"] for t in new_tasks) == [2, 3, 4, 5]
        assert {t["url"] for t in new_tasks} == {f"{base}?p={n}" for n in range(2, 6)}
        assert all(t["status"] == "queued" for t in new_tasks)

        # 4 条都真的被派给了下载执行体(不是只记了任务不跑)
        assert all_dispatched.wait(5), \
            f"应有 4 条派发到 _run_download,实际 {len(dispatched)} 条"
        assert sorted(p for _, _, p in dispatched) == [2, 3, 4, 5]
    finally:
        for tid in set(taskstore._tasks) - before:
            taskstore._tasks.pop(tid, None)


# ---------- 已下载判定的消费链保持基线(缺陷 B3 · Property 9) ----------

def test_frontend_downloaded_consumers_preserved():
    """3.13 保持基线:「已下载 → 批量提交时跳过 / 列表挂角标」这条消费链不许断。

    Property 9: Preservation —— 单 P 与同分 P 的已下载判定。

    与上一条测试的分工:上一条(任务 7)锁的是**判定本身**要加 page 维度,它在
    未修复代码上是红的;这一条锁的是判定的**消费方**,在未修复代码上就是绿的,
    记录 B3 修复不许动的部分。

    任务 9.4 会重写 downloads.js 的集合构建与 isDownloaded 实现(`_doneBvids`
    → `_doneKeys`、新增 doneKeySet)。重写时最容易顺手动掉的不是判定,而是这条
    穿过三个模块的消费链:

        downloads.js  isDownloaded ──setItemExtras──> lib.js _itemExtras
                                                        ├─ isUrlDownloaded(url)  → newdl.batchDownload 过滤
                                                        └─ 列表角标「已下载」
        downloads.js  isDownloaded ─────────────────────> libDownload 过滤

    任一环掉了都不会报错:注入名写错 → `_itemExtras.isDownloaded` 是 undefined,
    只在渲染时才炸;过滤行被删 → 已下载的条目重复入队、重复下载同一个文件
    (3.13 的「并在批量提交时跳过」当场失效)。所以逐环用子串断言钉住。

    断言刻意不碰 `_doneBvids` / `dlKey` 这些会被 9.4 改名的标识符 —— 那是
    test_frontend_download_key_shared_rule 的活儿,基线只锁不该变的部分。

    Validates: Requirements 3.13
    """
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    newdl_js = (_js_dir() / "newdl.js").read_text(encoding="utf-8")

    # 1) 注入点:downloads.js 把 isDownloaded 交给 lib 的 _itemExtras
    assert "isDownloaded," in dl_js, \
        "downloads.js 应把 isDownloaded 注入 setItemExtras(列表角标与 newdl 过滤都取这一份)"
    assert "export function isUrlDownloaded(url)" in lib_js, \
        "lib.js 应导出 isUrlDownloaded 作为跨页判定入口"
    assert "_itemExtras.isDownloaded(url)" in lib_js, \
        "isUrlDownloaded 应转发到注入的 isDownloaded,不得自建第二份判定"

    # 2) 列表角标:非下载页才挂「已下载」
    #   2026-11-10 起这里改用 downloadState(it.url):同一份注入来源、同一套
    #   (bv, 分P) 键规则,只是多返回一个「下载中」态供卡片显示。
    assert "_itemExtras.downloadState(it.url)" in lib_js, \
        "renderLibList 的下载状态角标应走同一份注入判定(downloadState)"
    assert "state.mode !== 'downloads'" in lib_js, \
        "角标只在内容页挂;下载记录页本身不需要(那里渲染的是任务卡)"

    # 3) 批量提交过滤:两个入口都要「统计跳过数 + 过滤 + 全跳过时提示」
    for name, src, pred in (("newdl.js", newdl_js, "isUrlDownloaded"),
                            ("pages/downloads.js", dl_js, "isDownloaded")):
        assert f"items.filter(it => {pred}(it.url)).length" in src, \
            f"{name} 应统计被跳过的已下载条目数(用户要看到「跳过 N 个已下载」)"
        assert f"items.filter(it => !{pred}(it.url))" in src, \
            f"{name} 的批量提交必须过滤掉已下载条目(3.13),否则重复下载同一文件"
        assert "toast('所选项均已下载过')" in src, \
            f"{name} 应在全部条目都已下载时明确提示,而不是静默提交空列表"
        assert "跳过 ${skipped} 个已下载" in src, \
            f"{name} 应在成功提示里回报跳过数量"


# ---------- 提交下载时的 page 维度:两个批量入口都不许丢(缺陷 C2 / C3) ----------

def test_frontend_lib_download_carries_page():
    """任务条目保留持久化 page，公共批量提交继续携带真实分 P。

    下载任务不能从原始 URL 猜 page（URL 可能不含 ``?p=``）；其他内容页没有
    持久化 page 时才使用 ``pageFromUrl``。任务映射与提交两层必须同时保留。

    Validates: Requirements 2.6, 2.7, 3.13
    """
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")

    task_item = _js_func_body(dl_js, "taskToItem")
    assert "page: task.page || 1," in task_item, \
        "任务投影必须保留 /api/tasks 返回的权威 page"
    assert "pageFromUrl(task.url)" not in task_item, \
        "任务 URL 可能不带分 P 参数，不能据此覆盖持久化 page"

    lib_body = _js_func_body(dl_js, "libDownload")
    assert "page: 1" not in lib_body
    assert "page: it.page || pageFromUrl(it.url)," in lib_body, \
        "任务记录应走 it.page，其他内容条目才回退到 URL 分 P"

    assert "function pageFromUrl" not in dl_js, \
        "downloads.js 不得复制 pageFromUrl，必须从 ../util.js 引入"


def test_frontend_batch_download_uses_page_from_url():
    """newdl.batchDownload 的分 P 解析必须走 pageFromUrl,不许第三份手写实现(缺陷 C3)。

    Property 3 的延伸 —— 键规则的唯一来源被绕过,行为与唯一来源不一致。

    缺陷表现:未修复现场 newdl.js 有第三份手写解析
    `it.url.includes('?p=') ? parseInt(it.url.split('?p=')[1]) : 1`,
    与 util.pageFromUrl(正则 `[?&]p=(\\d+)`)在两类输入上结论不同,且都不报错:

      - `&p=N` 形态(如 `?spm_id_from=333.999&p=2`):`includes('?p=')` 判假
        → 提交 page: 1(pageFromUrl 得 2)→ 下错集数。这不是理论形态:
        /api/batch/parse 的多链接模式与 resolve_playlist 的单视频分支都把
        **用户粘贴的原始 URL 原样透传**成条目 url,而从 B 站地址栏/分享按钮
        复制出来的链接普遍带 spm_id_from / vd_source 等参数在 p 之前。
      - `?p=abc`:`parseInt('abc')` → NaN → 提交 `page: NaN`
        (JSON 序列化成 null)(pageFromUrl 归一为 1)。

    修法是删掉这份副本、改调 pageFromUrl,并在顶部 import 里追加它 ——
    同一条规则不留第三份,将来改正则只需动 util.js 一处。

    Validates: Requirements 2.6, 2.7
    """
    import re
    newdl_js = (_js_dir() / "newdl.js").read_text(encoding="utf-8")

    # 1) 手写解析必须绝迹(整个文件范围)
    for frag in ("includes('?p=')", "split('?p=')"):
        assert frag not in newdl_js, (
            f"newdl.js 仍含手写分 P 解析 {frag!r}(缺陷 C3 现场):"
            "它与 util.pageFromUrl 在 `&p=N` / `?p=abc` 上结论不同,且不会报错")

    # 2) batchDownload 的提交体改走唯一来源
    body = _js_func_body(newdl_js, "batchDownload")
    assert "page: pageFromUrl(it.url)," in body, (
        "batchDownload 的 page 应取 pageFromUrl(it.url):"
        "条目只有 URL(拿不到任务对象),查询侧只能解析 ?p=,这是 B3 定下的原则")

    # 3) 顶部 import 里要有 pageFromUrl,否则运行时 ReferenceError
    imp = re.search(r"import\s*\{([^}]*)\}\s*from\s*'\./util\.js'", newdl_js, re.S)
    assert imp, "newdl.js 应从 './util.js' 引入工具函数"
    imported = {n.strip() for n in imp.group(1).split(",")}
    assert "pageFromUrl" in imported, (
        f"newdl.js 应从 util.js 引入 pageFromUrl,现在只有:{sorted(imported)}")


def test_frontend_page_parsing_only_in_util():
    """全 static/js/ 范围:`?p=` 的手写解析只许存在于 util.js 的 pageFromUrl 内部。

    Property 3 的延伸 —— 守住键规则的唯一来源,防止长出第四份。

    上两条各锁一处已知缺陷(C2 的 downloads.js、C3 的 newdl.js);这一条是方向性
    守卫:B3 把分 P 键规则收敛到 util.js 之后,任何文件再出现 `'?p='` 字面量,
    都意味着有人绕过唯一来源手写了新的解析。这类副本不会报错,只会在某个查询参数
    顺序上静默给出与唯一来源不同的分 P —— 正是 C3 的成因,所以按文件粒度直接禁掉。

    只禁被引号包起来的 `?p=` 字面量(手写解析的必要材料),不碰 util.js:唯一来源
    自己要写正则,且它用的是 `[?&]p=` 而非该字面量。

    按整行扫、不排除注释是刻意的:排除注释就得先判断「这一行是不是注释」,而
    `x = f(); // ...` 这类行注释能让真正的解析躲在同一行里混过去。代价是注释想提
    旧写法也会被拦(C3 修复时就撞到了),那就改成描述性措辞 —— 让守卫保持严格。

    Validates: Requirements 2.6, 2.7
    """
    offenders = {}
    for f in sorted(_js_dir().rglob("*.js")):
        if f.name == "util.js":
            continue  # 唯一来源自己实现规则
        src = f.read_text(encoding="utf-8")
        hits = [ln.strip() for ln in src.splitlines() if "'?p='" in ln or '"?p="' in ln]
        if hits:
            offenders[f.name] = hits
    assert not offenders, (
        f"以下文件出现了 '?p=' 字面量,是绕过 util.pageFromUrl 的手写分 P 解析:{offenders}"
        " —— 键规则必须只有一份(B3),副本会在某个查询参数顺序上静默给出不同的分 P")


# ---------- 手动检查按钮:删掉 30 秒假禁用窗口,按 started 给反馈(缺陷 B4) ----------

def test_frontend_up_check_has_no_fixed_window():
    """upCheckNow 必须读响应的 started,而不是靠固定 30 秒假禁用窗口猜。

    Property 4: Bug Condition —— 后来者跳过而非排队(前端侧)。

    未修复现场(static/js/pages/feed.js):路由无论有没有真的启动新一轮都返回
    同一个 {"ok": True},前端无从区分,于是写了

        // 串行检查耗时取决于 UP 数,先给个固定反馈窗口,期间队列轮询会带回新任务
        setTimeout(() => { btn.disabled = false; btn.innerHTML = old; }, 30000);

    这个窗口既不准也不对:真实一轮耗时取决于 UP 数量(UP 之间 sleep 5 秒,
    30 个 UP 就是两分半往上),30 秒到点解禁后再点仍然会叠加一轮 —— 它只是让按钮
    看起来被禁用了,一个请求都拦不住。假窗口是缺陷 B4 的**症状**:后端不肯说实话,
    前端只能猜。

    修复后后端如实回报 started(2.10),前端据此提示,「绝不并发、绝不排队」
    这个不变量由后端保证;按钮只在请求在途期间禁用。

    Validates: Requirements 2.10
    """
    feed_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    body = _js_func_body(feed_js, "upCheckNow")

    assert "r.started" in body, (
        "upCheckNow 应读响应的 started 字段来决定提示(2.10),而不是无视响应内容")
    assert "已有检查在进行中" in body, (
        "started 为 false 时要如实告知用户本次未启动新一轮")
    assert "30000" not in body, (
        "固定 30 秒假禁用窗口必须删除(缺陷 B4 的症状):真实一轮耗时取决于 UP 数量,"
        "窗口到点后再点仍会叠加一轮,它拦不住任何请求")


# ---------- B5 端到端:interrupted 任务在三处路由上一致地被当作已结束(缺陷 B5) ----------

def test_interrupted_task_consistent_across_routes_end_to_end(client, monkeypatch):
    """一条 `interrupted` 任务走完 2.12 的三处判断:取消被拒 → 不拦新下载 → 记录可清。

    Property 5: Bug Condition —— 终态任务不可被拉回活跃态。

    tests/test_downloader.py 那批测的是 `cancel_download` / `find_active_by_url` /
    `clear_tasks` 三个函数各自的返回值;这条把它们**串在同一条任务记录上**跑一遍
    HTTP 路由,因为 2.12 要的不是「三个函数分别正确」,而是「三处对终态的判断完全
    一致」—— 而用户实际踩到的是这条链:

        POST /api/cancel/<id>        → 未修复时返回 {"ok": true},状态 interrupted → cancelling
        POST /api/download 同 URL     → cancelling ∈ ACTIVE → find_active_by_url 命中
                                      → 409「该视频相同清晰度正在下载中」,这个视频从此下不了(1.12)
        POST /api/tasks/clear        → cancelling ∈ _ACTIVE → 当活跃任务跳过
                                      → count 为 0,「清除全部记录」永远清不掉这条僵尸(1.12)

    未修复时第一步就反了(`ok` 为 `true`),后面两步是它的下游效应。修复后
    `cancel_download` 走 `taskstore.TERMINAL` 唯一来源,链条从源头就不成立。

    一处打桩,只为断网,不改被测逻辑:`/api/download` 在过了 409 拦截之后会调
    `downloader.start_download`,那会真的 `parse_part(url, page)` 去抓 B 站。桩成
    「记账 + 返回假 task_id」,这样第 3 步断言的是**纯粹的「没被 409 拦住」**:
    状态码不是 409、且请求真的落到了 start_download(参数逐位相同)。桩故意不往
    任务表里塞记录 —— 让第 4 步的 `count == 1` 只可能来自那条 interrupted 记录。

    落盘安全:`clear_tasks` 会 `_persist()`。conftest 的 autouse fixture 已把
    `taskstore.TASKS_FILE` 重定向到 tmp_path 并给出干净任务表,真实 data/tasks.json
    全程不碰;`delete_file=False` 也不会走 `_task_files`,不碰下载目录。

    Validates: Requirements 2.11, 2.12
    """
    import taskstore

    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    label, page = "1080P 高码率", 2

    # ---- 打桩:start_download(断网)。签名照 app.download() 的调用方式
    started = []

    def fake_start_download(url_, label_, page_=1, uploader_mid=0):
        started.append((url_, label_, page_, uploader_mid))
        return "fake_tid_e2e"

    monkeypatch.setattr("app.downloader.start_download", fake_start_download)

    taskstore._tasks["e2e_intr"] = {
        "task_id": "e2e_intr", "status": "interrupted", "url": url,
        "label": label, "page": page, "title": "中断的视频",
        "progress": 42, "speed": 0, "eta": 0,
        "error": "服务重启,任务中断", "cancel": False,
    }
    try:
        # 前提:任务表只有这一条 —— 第 4 步的 count == 1 据此才是精确的
        assert set(taskstore._tasks) == {"e2e_intr"}, \
            "conftest 应给出干净任务表;表里有别的记录会让 clear 的 count 失真"

        # ---- 第 1 步:取消必须被拒(2.11)
        r = client.post("/api/cancel/e2e_intr")
        assert r.status_code == 200
        assert r.get_json() == {"ok": False}, (
            "interrupted 已是终态,/api/cancel 必须回 ok=false;"
            "未修复时回 ok=true 并把它改成 cancelling —— 无人收尾的永久僵尸态")

        # ---- 第 2 步:状态一字不变
        t = taskstore._tasks["e2e_intr"]
        assert t["status"] == "interrupted", (
            f"状态被改成 {t['status']!r};未修复时是 cancelling(活跃态)")
        assert t["cancel"] is False, "终态任务不该被置 cancel 标志"

        # ---- 第 3 步:同 URL + 清晰度 + 分 P 的新下载不得被 409 拦住(2.12)
        r2 = client.post("/api/download",
                         json={"url": url, "label": label, "page": page})
        assert r2.status_code != 409, (
            "interrupted 已终结,同 URL 新下载不该被「正在下载中」拦截(1.12);"
            f"实际 {r2.status_code} {r2.get_json()}")
        assert r2.get_json() == {"ok": True, "task_id": "fake_tid_e2e"}
        assert started == [(url, label, page, 0)], (
            "请求必须真的落到 start_download(参数逐位相同),"
            "才能证明是「放行」而不是在别处提前返回了 200")

        # ---- 第 4 步:「清除全部记录」能清掉这条(2.12)
        r3 = client.post("/api/tasks/clear", json={"delete_file": False})
        assert r3.status_code == 200
        assert r3.get_json() == {"ok": True, "count": 1}, (
            "interrupted 不属于活跃态,清除全部记录必须把它清掉;"
            "未修复时它已被改成 cancelling → count 为 0,记录永远删不掉(1.12)")
        assert "e2e_intr" not in taskstore._tasks, "记录应已从任务表移除"
    finally:
        taskstore._tasks.pop("e2e_intr", None)


# ---------- /api/home-feed:首页推荐流 ----------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.url = "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd"

    def json(self):
        return self._payload


class _FakeWbiIE:
    def _sign_wbi(self, params, video_id):
        return {**params, "w_rid": "fake", "wts": 1}


def _rcmd_item(bvid="BV1xx411c7mD", goto="av", ad=False, stock=0):
    return {
        "bvid": bvid, "goto": goto, "title": f"标题{bvid}",
        "pic": "http://i0.hdslb.com/bfs/x.jpg", "duration": 300,
        "pubdate": 1793000000,
        "owner": {"mid": 42, "name": "某UP"},
        "stat": {"view": 234500, "like": 12345},
        "rcmd_reason": {"reason_type": 3, "content": "2万点赞"},
        "is_ad_loc": ad, "is_stock": stock,
    }


def _setup_home_feed(monkeypatch, payload):
    import bilibili_api as bapi
    monkeypatch.setattr(bapi, "get_auth_context", lambda: {"authenticated": True}, raising=False)
    monkeypatch.setattr(bapi, "_auth_session", lambda context=None: (None, object()))
    monkeypatch.setattr(bapi, "_ensure_context_current", lambda ctx: None)
    monkeypatch.setattr(bapi, "_get_wbi_ie", lambda context=None: _FakeWbiIE())
    monkeypatch.setattr(bapi, "_get",
                        lambda url, **kw: _FakeResp(payload))


def test_home_feed_filters_ads_and_normalizes(client, monkeypatch):
    """推荐流只保留普通视频:广告位/直播/推广被过滤,字段归一化。"""
    payload = {
        "code": 0, "message": "0", "ttl": 1,
        "data": {"item": [
            _rcmd_item("BV1aaaaaaaa1"),
            _rcmd_item("BV1bbbbbbbb2", ad=True),
            _rcmd_item("BV1cccccccc3", goto="live"),
            _rcmd_item("BV1dddddddd4", stock=1),
            {"goto": "av", "title": "无bvid", "owner": {}},
        ]},
    }
    _setup_home_feed(monkeypatch, payload)
    resp = client.get("/api/home-feed?fresh_idx=1")
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True
    items = body["items"]
    assert [it["bvid"] for it in items] == ["BV1aaaaaaaa1"]
    it = items[0]
    assert it["url"] == "https://www.bilibili.com/video/BV1aaaaaaaa1"
    assert it["uploader"] == "某UP" and it["uploader_mid"] == 42
    assert it["thumbnail"].startswith("https://")
    assert it["rcmd_reason"] == "2万点赞"
    assert it["stat_view"] == "23.4万" and it["stat_like"] == "1.2万"
    assert it["duration"] == 300 and it["view_at"] == 1793000000


def test_home_feed_followed_reason(client, monkeypatch):
    """reason_type=1(已关注)映射为「已关注」文案。"""
    item = _rcmd_item()
    item["rcmd_reason"] = {"reason_type": 1, "content": ""}
    _setup_home_feed(monkeypatch,
                     {"code": 0, "data": {"item": [item]}})
    body = client.get("/api/home-feed?fresh_idx=2").get_json()
    assert body["items"][0]["rcmd_reason"] == "已关注"


def test_home_feed_upstream_error_passthrough(client, monkeypatch):
    """B 站返回非 0 code 时透出错误信息,不静默给空列表。"""
    _setup_home_feed(monkeypatch, {"code": -403, "message": "访问权限不足"})
    resp = client.get("/api/home-feed")
    assert resp.status_code // 100 != 2 or resp.get_json().get("ok") is False


def test_home_feed_fresh_idx_validation(client, monkeypatch):
    """fresh_idx 非法值按 400/错误处理,不发起请求。"""
    calls = []
    import bilibili_api as bapi
    monkeypatch.setattr(bapi, "fetch_home_feed",
                        lambda idx, **kw: calls.append(idx) or [])
    resp = client.get("/api/home-feed?fresh_idx=abc")
    assert resp.status_code == 400
    assert not calls


def test_home_feed_ps_passthrough(client, monkeypatch):
    """ps 原样透传给 fetch_home_feed:前端靠它首屏一次要够铺满整页的条数。"""
    calls = []
    import bilibili_api as bapi
    monkeypatch.setattr(bapi, "fetch_home_feed",
                        lambda idx, ps=12, **kw: calls.append((idx, ps)) or [])
    body = client.get("/api/home-feed?fresh_idx=1&ps=24").get_json()
    assert body["ok"] is True and calls == [(1, 24)]
    calls.clear()
    client.get("/api/home-feed?fresh_idx=3")
    assert calls == [(3, 12)], "省略 ps 时按 web 端惯用值 12"


def test_home_feed_ps_validation(client, monkeypatch):
    """ps 越界或非整数按 400,不发请求(接口上限 30)。"""
    calls = []
    import bilibili_api as bapi
    monkeypatch.setattr(bapi, "fetch_home_feed",
                        lambda idx, ps=12, **kw: calls.append((idx, ps)) or [])
    for bad in ("31", "0", "abc"):
        resp = client.get(f"/api/home-feed?fresh_idx=1&ps={bad}")
        assert resp.status_code == 400, bad
    assert not calls


def test_home_feed_first_page_saves_cache(client, monkeypatch):
    """fresh_idx=1 且非空的结果落缓存,且 /api/cache/home_feed 能读回——
    前端靠它「打开先出上次内容再后台刷新」。"""
    import pagecache
    payload = {"code": 0, "data": {"item": [_rcmd_item("BV1aaaaaaaa1")]}}
    _setup_home_feed(monkeypatch, payload)
    body = client.get("/api/home-feed?fresh_idx=1").get_json()
    assert body["ok"] is True
    cached = client.get("/api/cache/home_feed").get_json()
    assert cached["ok"] is True and cached["data"]["ok"] is True
    assert [it["bvid"] for it in cached["data"]["items"]] == ["BV1aaaaaaaa1"]
    assert pagecache.load("home_feed") is not None


def test_home_feed_empty_result_and_pagination_skip_cache(client, monkeypatch):
    """空结果(风控典型表现)不覆盖已有缓存;翻页(fresh_idx>1)不写缓存。"""
    _setup_home_feed(monkeypatch,
                     {"code": 0, "data": {"item": [_rcmd_item("BV1aaaaaaaa1")]}})
    client.get("/api/home-feed?fresh_idx=1")
    _setup_home_feed(monkeypatch, {"code": 0, "data": {"item": []}})
    client.get("/api/home-feed?fresh_idx=1")
    cached = client.get("/api/cache/home_feed").get_json()
    assert cached["ok"] is True, "空结果不得覆盖缓存"
    assert cached["data"]["items"][0]["bvid"] == "BV1aaaaaaaa1"
    _setup_home_feed(monkeypatch,
                     {"code": 0, "data": {"item": [_rcmd_item("BV1bbbbbbbb2")]}})
    client.get("/api/home-feed?fresh_idx=2")
    cached = client.get("/api/cache/home_feed").get_json()
    assert cached["data"]["items"][0]["bvid"] == "BV1aaaaaaaa1", "翻页不写缓存"


# ---------- /api/relation/modify:关注/取关 ----------

def test_relation_modify_dispatches_to_bapi(client, monkeypatch):
    """取关/回关路由把 (mid, action) 原样交给 bapi.modify_relation。"""
    calls = []
    import app as app_module
    monkeypatch.setattr(app_module.bapi, "modify_relation",
                        lambda mid, action, **kw: calls.append((mid, action)))
    r = client.post("/api/relation/modify",
                    json={"mid": 12345, "action": "unfollow"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    assert calls == [(12345, "unfollow")]


def test_relation_modify_rejects_unknown_action(client):
    """非法 action 按 400 拒绝,不触发任何上游调用。"""
    r = client.post("/api/relation/modify",
                    json={"mid": 12345, "action": "block"})
    assert r.status_code == 400
    assert r.get_json()["error"]


def test_relation_modify_requires_mid(client):
    r = client.post("/api/relation/modify", json={"action": "follow"})
    assert r.status_code == 400


def test_followings_keep_mtime_and_sign(monkeypatch):
    """关注列表解析保留关注时间与个性签名。

    回归:解析时只留 mid/uname/face,卡片上的「关注于」和个性签名拿不到,
    而要补这两项就得为每个 UP 额外打一次接口。
    """
    import bilibili_api as bapi
    monkeypatch.setattr(bapi, "_auth_session",
                        lambda context=None: (None, object()))
    monkeypatch.setattr(bapi, "_ensure_context_current", lambda ctx: None)
    captured = {}

    def fake_paged_get(url, base_params, *, what, max_pages, ps, gap,
                       mid_param, parse, context, cookiejar):
        items, _has_more = parse({"total": 2, "list": [
            {"mid": 1, "uname": "A", "face": "http://x/a.jpg",
             "mtime": 1700000000, "sign": "签名 A"},
            {"mid": 2, "uname": "B", "face": "http://x/b.jpg", "sign": None},
            {"mid": 3, "uname": "C", "face": "http://x/c.jpg",
             "mtime": "not-a-number", "sign": 0},
        ]}, 0)
        captured["items"] = items
        return items

    monkeypatch.setattr(bapi, "_paged_get", fake_paged_get)
    bapi.fetch_followings_all(force=True)
    items = captured["items"]
    assert items[0]["mtime"] == 1700000000 and items[0]["sign"] == "签名 A"
    assert items[1]["sign"] == "", "缺失签名字段应为空串,不能是 None"
    # 脏数据不得打断整页解析:非数值 mtime 归 0,空/零签名归空串
    assert items[2]["mtime"] == 0 and items[2]["sign"] == ""


def test_followings_response_includes_local_ups(client, monkeypatch):
    """关注列表响应带 local_ups(自动下载清单详情),供合并视图渲染。"""
    import app as app_module
    monkeypatch.setattr(app_module.bapi, "fetch_followings_all",
                        lambda **kw: {"items": [{"mid": 1, "uname": "A",
                                                 "face": "http://x/a.jpg"}],
                                      "total": 1})
    monkeypatch.setattr(app_module.up_list, "list_ups",
                        lambda: [{"mid": 2, "uname": "B", "face": "https://x/b.jpg",
                                  "quality": "1080P", "added_at": 1700000000},
                                 {"mid": 1, "uname": "A", "face": "http://x/a.jpg",
                                  "quality": ""}])
    monkeypatch.setattr(app_module.up_list, "get_last_seen_all",
                        lambda: {"2": {"ts": 1793000000}})
    r = client.get("/api/followings")
    body = r.get_json()
    assert body["ok"] is True
    local = {u["mid"]: u for u in body["local_ups"]}
    assert set(local) == {1, 2}
    assert local[2]["quality"] == "1080P"
    assert local[2]["face"].startswith("https://")
    # 卡片的时间行靠这两个字段:加入清单时间与最近投稿时间
    assert local[2]["added_at"] == 1700000000
    assert local[2]["last_pub_at"] == 1793000000
    assert local[1]["added_at"] == 0, "老记录没有 added_at 时按 0 兜底"
    assert local[1]["last_pub_at"] == 0
    assert "local_mids" not in body, "旧字段应被 local_ups 取代"


# ---------- 取关交互与操作条位置 ----------

def test_up_card_toggles_have_no_confirmation_dialog():
    """关注/自动下载两个开关都不弹二次确认:点击即切换、可立即反悔。

    回归:曾按写操作惯例给取关/移出清单加确认弹窗;用户反馈不必要——
    开关交互里反悔成本为零,确认只会打断操作节奏。
    """
    feed_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    body = _js_func_body(feed_js, "upToggleFollow")
    assert "confirmAction" not in body, "取关不应弹二次确认"
    assert "relation/modify" in body, "回关/取关仍要走 relation/modify"
    auto_body = _js_func_body(feed_js, "upAutoChanged")
    assert "confirmAction" not in auto_body, "自动下载开关不应弹二次确认"
    assert "up-list/remove" in auto_body and "up-list/add" in auto_body


def test_up_card_auto_is_single_merged_control():
    """自动下载与画质合成一个 select 控件,控件条压在封面底部。

    回归:旧卡片「画质下拉」单独占第二行且按钮挤得名称全是省略号;
    合并后开启态一个 select(选项=画质,含「不下载」),未开启态选画质即加入。
    控件条不再额外占行,整卡高度与视频卡片对齐。
    """
    feed_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    card = _js_func_body(feed_js, "upCardHtml")
    assert "upAutoControlHtml" in card and "uc-actions" in card, \
        "自动下载控件应收进 uc-actions"
    assert "upToggleFollow" in card, "卡片应有关注开关按钮"
    assert "dl-badge" not in card, "绿色自动下载标签应移除"
    assert "加入清单" not in card and ">移除<" not in card, \
        "旧的加入清单/移除按钮应移除"
    ctrl = _js_func_body(feed_js, "upAutoControlHtml")
    assert 'class="uc-auto on"' in ctrl and "不下载" in ctrl, \
        "开启态控件应含画质选项与「不下载」项"
    # 两态都要有显式的「不下载」:用户要能直接看到/选择"不自动下载"状态
    assert ctrl.count("不下载") >= 2, \
        "未开启态与开启态都应提供「不下载」选项"
    assert 'value="__off" selected>不下载' in ctrl, \
        "未开启态应以「不下载」为当前选中项(而非误导性的「自动下载」占位)"
    assert "停止下载" not in feed_js, "选项文案统一为「不下载」"
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    assert ".uc-auto" in css, "清晰度/自动下载合并控件应有样式"
    assert ".uc-auto.on" in css and "background-color:var(--accent-soft)" in css, \
        "开启态应有强调样式,且用 background-color 长属性(否则 shorthand 覆盖自绘箭头)"
    assert ".up-card .uc-actions > button{flex:0 0 auto;width:64px;padding:0;justify-content:center;white-space:nowrap}" in css, \
        "关注/不关注按钮必须等宽,切换时不跳动"
    assert "flex:1 1 auto" in css, "下拉占满控件条剩余宽度"
    # 原生 select 的三角钉死在右边框上,加宽/改 padding 都推不动;
    # 必须 appearance:none 自绘箭头,才能让箭头与右边框留出间距
    assert "appearance:none" in css and "background-position:right 6px center" in css, \
        "下拉须自绘箭头(appearance:none + background-position),否则三角贴右边框"


def test_action_bar_bottom_pill():
    """批量操作条为底部居中胶囊(终选 B 站 Next 设计)。

    出现时 .content 预留底部空间,不遮末排卡片;隐藏态用 transform 收起,
    不占布局。此前的「顶部条」回归点由终选设计替代,如需回改请连同本测试。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    # 静态规则必须落在响应式断点之前(桌面与窄屏同用底部胶囊)
    bar_at = css.index(".action-bar{")
    mq_at = css.find("@media (max-width")
    assert bar_at != -1 and (mq_at == -1 or bar_at < mq_at), \
        "操作条规则不应塞进响应式断点内"
    assert ".action-bar{position:fixed;left:50%;bottom:22px;" in css, \
        "操作条应固定在底部居中(胶囊)"
    assert ".action-bar.show{transform:translate(-50%,0);opacity:1;" in css, \
        "勾选后应以 .show 浮现"
    assert "body.action-bar-open .content{padding-bottom:150px}" in css, \
        "出现操作条时内容区应预留底部空间,不遮末排卡片"


# ---------- 合集/收藏夹自动下载订阅 ----------

def test_collection_add_baselines_existing_items(client, monkeypatch):
    """订阅合集时以当前全部条目为基线,之后只下新增的(不能把历史全下一遍)。"""
    import app as app_module
    captured = {}

    def fake_fetch(cid, **kw):
        captured["cid"] = cid
        return [{"bvid": "BV1", "title": "旧1", "url": "https://www.bilibili.com/video/BV1"},
                {"bvid": "BV2", "title": "旧2", "url": "https://www.bilibili.com/video/BV2"}]

    monkeypatch.setattr(app_module.bapi, "fetch_season_content", fake_fetch)
    monkeypatch.setattr(app_module.up_list, "add_collection",
                        lambda kind, cid, **kw: (captured.update(kw) or
                                                 {"kind": kind, "id": cid,
                                                  "quality": kw.get("quality", "")}))
    r = client.post("/api/collection/add",
                    json={"kind": "season", "id": 123, "title": "合集",
                          "quality": "1080P"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["baseline_count"] == 2, "基线应包含现有 2 条"
    assert captured["seen_bvids"] == ["BV1", "BV2"]
    assert captured["quality"] == "1080P"


def test_collection_add_fails_when_baseline_unreadable(client, monkeypatch):
    """读不到基线时明确失败:没有基线的订阅会把整个合集一次性下完。"""
    import app as app_module

    def boom(cid, **kw):
        raise RuntimeError("接口失败")

    monkeypatch.setattr(app_module.bapi, "fetch_fav_content", boom)
    r = client.post("/api/collection/add", json={"kind": "fav", "id": 456})
    assert r.status_code >= 400
    assert "未建立订阅" in (r.get_json() or {}).get("error", "")


def test_collection_routes_validate_kind(client):
    """kind 只接受 season/fav。"""
    r = client.post("/api/collection/add", json={"kind": "up", "id": 1})
    assert r.status_code == 400
    r = client.post("/api/collection/remove", json={"kind": "season"})
    assert r.status_code == 400


def test_up_list_returns_collections_without_seen_payload(client, monkeypatch):
    """订阅列表下发合集信息,但不下发可能几百条的 seen_bvids。"""
    import app as app_module
    monkeypatch.setattr(app_module.up_list, "list_ups", lambda: [])
    monkeypatch.setattr(app_module.up_list, "list_collections", lambda: [
        {"kind": "season", "id": 1, "title": "合集", "quality": "",
         "seen_bvids": ["BV1", "BV2", "BV3"]}])
    body = client.get("/api/up-list").get_json()
    assert body["collections"][0]["seen_count"] == 3
    assert "seen_bvids" not in body["collections"][0]


def test_frontend_collection_toggle_wired():
    """追更合集/我的收藏卡片上有自动下载开关,并接到合集订阅接口。"""
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    fav_js = (_js_dir() / "pages" / "fav.js").read_text(encoding="utf-8")
    assert "collectionAutoControl" in seasons_js
    assert "collectionAutoChanged" in seasons_js
    assert "/api/collection/add" in seasons_js
    assert "/api/collection/remove" in seasons_js
    assert "collectionAutoControl" in fav_js, "收藏夹也应复用同一开关"
    assert "stopPropagation" in seasons_js, "开关点击不得触发进入合集"


# ---------- 前端模块图完整性(防「测试全绿但页面白屏」) ----------

def test_collection_unsubscribed_control_has_one_selected_option():
    """未订阅时控件只能有一个 selected 项,否则「最高画质」永远订阅不上。

    回归:未订阅分支用 `_qualityOptionsHtml('')` 生成画质项,空串画质
    (最高画质)因此也被打上 selected,与「不下载」同时选中。浏览器只认
    最后一个,控件显示成「最高画质」而实际没有订阅;用户再选一次
    「最高画质」不产生 change 事件,`/api/collection/add` 永远不会被调用。
    """
    import re
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    ctrl = _js_func_body(seasons_js, "collectionAutoControl")
    # 未订阅分支必须传 null(明确「无当前档位」),不能传空串
    assert "_qualityOptionsHtml(null)" in ctrl, \
        "未订阅分支不得把空串当作已选档位"
    assert "_qualityOptionsHtml('')" not in ctrl, \
        "空串会让「最高画质」被误选中"
    # 选项生成器必须真的区分 null 与空串
    opts = _js_func_body(seasons_js, "_qualityOptionsHtml")
    assert "cur != null" in opts, "生成器要显式跳过 null 的选中标记"

    # 未订阅分支整段里 selected 只应出现一次(「不下载」)
    unsubscribed = ctrl[ctrl.index('class="uc-auto"'):]
    assert unsubscribed.count("selected") == 1, \
        f"未订阅控件出现多个 selected: {unsubscribed}"


def test_quality_select_puts_off_option_first():
    """「不下载」固定第一项:两个页面的开关排序必须一致。

    用户反馈追更合集的排序更好(「不下载」在最前),关注 UP 卡片要对齐。
    """
    import re
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    subs_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    flat_seasons = re.sub(r"\s+", " ", _js_func_body(seasons_js, "collectionAutoControl"))
    flat_subs = re.sub(r"\s+", " ", _js_func_body(subs_js, "upAutoControlHtml"))
    for flat, name in ((flat_seasons, "collectionAutoControl"),
                       (flat_subs, "upAutoControlHtml")):
        # 已订阅态:不下载项必须排在画质项之前
        assert re.search(r'__off">不下载</option>\s*\$\{', flat), \
            f"{name} 的已订阅态应把「不下载」放在画质项之前"
        # 文案不再说「末项」
        assert "末项" not in flat, f"{name} 的提示文案仍写着「末项」"


def test_frontend_module_urls_are_not_duplicated():
    """同一个模块文件不得以两个不同 URL 被导入。

    回归:app.js 用 `./pages/seasons.js?v=...`、fav.js 用 `./seasons.js`
    导入同一个文件。带查询串与不带查询串在浏览器里是两个不同资源,于是
    seasons.js 被实例化两次:`_collSubs` 状态分裂、`registerPage('seasons')`
    被重复注册,合集页的开关和列表会读到另一份内存状态。
    """
    import re
    import posixpath
    js_dir = _js_dir()
    refs: dict[str, set] = {}
    for f in js_dir.rglob("*.js"):
        src = f.read_text(encoding="utf-8")
        rel = f.relative_to(js_dir).as_posix()
        for spec in re.findall(r"from\s+['\"]([^'\"]+)['\"]", src):
            if not spec.startswith("."):
                continue
            path, _, query = spec.partition("?")
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(rel), path))
            refs.setdefault(target, set()).add(query)
    duplicated = {k: sorted(v) for k, v in refs.items() if len(v) > 1}
    assert not duplicated, f"同一模块存在多个导入 URL: {duplicated}"


def test_subs_merge_ups_orders_auto_first_then_time():
    """关注 UP 列表排序:自动下载优先,组内按关注时间倒序。

    用户明确要求恢复这个顺序:先按「是否自动下载」分组,同一组内再按
    关注时间排。卡片上的关注时间因此与列表顺序一致。
    同时列表要携带关注时间/最近投稿/签名三项卡片信息。
    """
    subs_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    body = _js_func_body(subs_js, "mergeUps")
    sort_body = _js_func_body(subs_js, "sortUps")
    assert ".auto" in sort_body, "排序第一关键字必须是「是否自动下载」"
    assert "followedAt" in sort_body, "组内第二关键字必须是关注时间"
    assert "sortUps(merged)" in body, "mergeUps 必须走统一排序"
    # 切换开关后要立刻归位:开启升到自动组,关闭落到不下载组
    auto_body = _js_func_body(subs_js, "upAutoChanged")
    assert auto_body.count("sortUps()") == 2, \
        "开启与关闭自动下载后都要立即重排"
    for key in ("followedAt", "lastPubAt", "sign"):
        assert key in body, f"mergeUps 应携带 {key}"

    card = _js_func_body(subs_js, "upCardHtml")
    for text in ("关注于", "加入于", "最近投稿", "尚未检查投稿", "暂无个性签名"):
        assert text in card, f"卡片缺少「{text}」"
    assert "uc-sign" in card and "uc-meta" in card, "卡片应有签名行与时间行"


def test_fav_folders_order_auto_first_then_subscription_time():
    """收藏夹与另外两页同一套排序:已订阅在前,组内按订阅时间倒序。

    收藏夹接口(created/list-all)不返回任何时间字段,唯一可用的时间键是
    本地订阅记录里的 added_at;未订阅的一组没有时间键,保持 B 站原顺序。
    """
    fav_js = (_js_dir() / "pages" / "fav.js").read_text(encoding="utf-8")
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    body = _js_func_body(fav_js, "favFolderList")
    assert "collectionSub(" in body, "排序第一关键字必须是「是否已订阅」"
    assert "added_at" in body, "组内第二关键字必须是订阅时间"
    render = _js_func_body(fav_js, "renderFolders")
    assert "favFolderList()" in render, "渲染必须使用排序后的列表"
    assert "_favFolders.length" not in render, \
        "统计口径应取排序后的全集,不能再用未排序的 _favFolders"
    # 订阅记录通过访问函数读取,收藏页不直接碰 seasons.js 的模块内 Map
    assert "export function collectionSub(" in seasons_js
    assert "collectionSub" in fav_js
    assert "_collSubs" not in fav_js, "收藏页不应直接访问 seasons.js 的内部 Map"


def test_seasons_overview_orders_auto_first_then_time():
    """追更合集与关注 UP 同一套排序:已订阅在前,组内按合集更新时间倒序。"""
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    body = _js_func_body(seasons_js, "overviewList")
    assert "_collSubs.has" in body, "已开自动下载的合集必须排在前面"
    assert "mtime" in body, "组内必须按合集更新时间倒序"
    render = _js_func_body(seasons_js, "renderOverview")
    assert "overviewList()" in render, "渲染必须使用排序后的列表"
    assert "_seasons.length" not in render, \
        "统计口径应取排序后的全集,不能再用未排序的 _seasons"

    import inspect
    import bilibili_api as bapi
    src = inspect.getsource(bapi.fetch_collected_seasons)
    assert '"mtime"' in src, "合集列表解析必须保留 mtime,否则组内无法按时间排序"


def test_up_card_poster_cover():
    """UP 卡为海报式:16:9 封面,关注/清晰度控件条压在封面底部。

    DOM 里 uc-actions 嵌在 uc-thumb 内;控件条必须绝对定位压底,否则会从
    52px 封面里溢出、竖着堆在头像下方(2026-11-10 真实页回归)。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    assert "--card-min:" in css and "--card-gap:" in css, \
        "卡片列宽下限/间距必须由 :root 令牌单点控制"
    grid_spec = "minmax(var(--card-min),1fr)"
    for sel in (".lib-grid{", ".up-grid{"):
        rules = [ln for ln in css.splitlines() if ln.replace(" ", "").startswith(sel)]
        assert rules and grid_spec in rules[0], \
            "所有内容网格必须共用同一弹性轨道:同一窗口跨页等宽,任何宽度不留空带"
    assert "minmax(min(var(--card-min),100%)" not in css, \
        "下限含百分比会让 auto-fill 退回 min-content 数列,列数塌缩成巨卡(2026-11-10 回归)"
    assert "--card-max" not in css, \
        "固定上限会被 auto-fill 按上限计列数,宽窗口留下接近一张卡宽的空带(2026-11-10 首页回归)"
    assert 'page[data-page="fav"] .lib-grid' not in css, \
        "收藏/合集总览已并入统一网格规格,不再需要独立覆盖"
    assert css.count("max-height:220px") >= 3, \
        "视频/UP/收藏封面限高必须统一为 220px"
    block = css[css.index(".up-card{"):css.index(".uc-text{")]
    assert "aspect-ratio:16/9" in block and "max-height:220px" in block, \
        "封面区应为 16:9 且限高,单卡全宽时不至于巨高"
    assert "position:absolute" in block, "控件条应绝对定位压在封面底部"
    assert ".up-card .uc-actions > button{flex:0 0 auto;width:64px;padding:0;justify-content:center;white-space:nowrap}" in css, \
        "关注/取消关注按钮必须等宽且单行"
    assert ".up-list .up-card{flex-direction:row" in css, "列表模式应为行式"


def test_badge_icon_svg_has_explicit_size():
    """「已下载」角标里的勾选图标必须有显式尺寸。

    回归（2026-11-10 关注动态页实拍）：icons.js 的 icon() 只输出带 viewBox 的
    <svg>，不带 width/height 属性；.dl-badge 是全项目唯一漏写尺寸规则的图标容器
    （button svg / .vi-stat svg / #qr-status svg 等都有），于是勾选图标退回
    "替换元素默认尺寸"（约 150px 方块），把角标撑成一个占满标题行的浅绿色大矩形，
    中文标题也被挤得逐字换行。修法两层：.dl-badge 里给显式 11px（与 .vi-stat svg
    同规格），基础层再给 svg 一个 1em 兜底，新容器漏写尺寸时不会重演。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    # 基础层兜底：任何新容器忘写尺寸都不会退回 150px 方块
    base_rule = [ln for ln in css.splitlines() if ln.replace(" ", "").startswith("svg{")]
    assert base_rule and "width:1em" in base_rule[0] and "height:1em" in base_rule[0], \
        "基础层要给 svg 一个 1em 兜底尺寸：icon() 的 svg 没有 width/height 属性"
    # 出事的那一处：显式尺寸
    badge_svg = css[css.index(".dl-badge svg{"):]
    badge_svg = badge_svg[:badge_svg.index("}")]
    assert "width:11px" in badge_svg and "height:11px" in badge_svg, \
        ".dl-badge svg 必须显式给尺寸，否则勾选图标会撑成一个大矩形盖住标题"
    # 根因是图标缺尺寸，不要再用改标题行布局的方式绕过（已用 A/B 复现页证伪）
    assert ":has(.dl-badge)" not in css and ":has(.rcmd-badge)" not in css, \
        "角标问题根因在图标尺寸，不应再改 .vi-title 的布局去绕过"


def test_downloaded_badge_sits_on_cover_corner():
    """「已下载」标记挂在封面右上角，与合集卡的「追更中」是同一枚角标。

    回归（2026-11-10 实拍）：它原先跟在标题行里（标题 + 「✓已下载」挤同一行，
    长标题和角标互相压），用户圈图要求「和追更中放一样的位置」。现在网格卡把它
    移进封面容器、复用 .uc-badge（.ok 变体 = 实底绿白字），行式列表的封面只有
    150px 宽、挂角标会挤，保留行内浅底小标签。
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    assert 'class="uc-badge ok"' in lib_js, \
        "「已下载」应复用封面角标 .uc-badge（与「追更中」同一枚）"
    lv_at = lib_js.index('<div class="video-item lv${checkedCls}">')
    grid_at = lib_js.index('<div class="video-item${checkedCls}">')
    assert lv_at < grid_at, "两个分支的顺序变了，下面切片的前提不成立"
    lv_card = lib_js[lv_at:grid_at]
    grid_card = lib_js[grid_at:lib_js.index("updateLibCount();", grid_at)]
    title_at = grid_card.index('<div class="vi-title"')
    assert "coverBadgeHtml" in grid_card[:title_at], "网格卡的角标必须落在封面容器内"
    assert "coverBadgeHtml" not in grid_card[title_at:], "标题行里不应再挂「已下载」"
    assert "badgeHtml" in lv_card, "行式列表保留行内「已下载」标签"
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    assert ".uc-badge svg{width:11px;height:11px}" in css, \
        "角标里的勾选图标必须显式给尺寸，否则退回替换元素默认尺寸（约 150px 方块）"
    assert ".uc-badge.ok{color:#fff;background:var(--green-solid)}" in css, \
        "已下载角标要用实底绿：浅底绿只有 9% 不透明度，压在封面上读不出来"
    assert css.count("--green-solid:") == 2, \
        "实底绿要有明暗两套令牌（对齐 --red-solid 的做法）"


def test_active_download_badge_on_cover_corner():
    """正在下载的视频，封面右上角也显示角标「下载中」。

    用户要求（2026-11-10）：「已下载或者有下载中 [都] 放一样的位置」。原先下载过程中
    卡片上没有任何标记——只有任务完成后才出现「已下载」，正在下的视频看起来和没下过
    一样。现在把进行中的任务键也算进卡片状态（与 doneKeySet 同粒度：(bv, 分P)），
    网格卡显示实底蓝「下载中」角标，行式列表显示行内蓝色小标签。
    """
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")
    assert "function activeKeySet(" in dl_js, "要有「进行中任务」键集合"
    assert "TASK_ACTIVE.includes(t.status)" in dl_js, "进行中集合按 TASK_ACTIVE 判状态"
    assert "function downloadState(" in dl_js, "要把状态判定暴露给卡片"
    assert "downloadState,\n  onChecksEmpty" in dl_js, "downloadState 要挂进 setItemExtras"
    badges = dl_js[dl_js.index("function applyTaskBadges("):]
    badges = badges[:badges.index("\n}")]
    assert "_activeKeys" in badges, "applyTaskBadges 要维护进行中集合"
    assert "||" in badges, \
        "进行中集合变化也要算 changed 并触发重绘，否则角标要等下次整页刷新才出现"
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    assert "downloadState: () => ''" in lib_js, "lib.js 要有缺省实现（未接线时不报错）"
    assert 'class="uc-badge dl"' in lib_js, "网格卡的「下载中」用同一枚封面角标"
    assert "dlState === 'active'" in lib_js, "已下载 / 下载中两态都要处理"
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    assert ".uc-badge.dl{color:#fff;background:var(--blue-solid)}" in css, \
        "「下载中」角标要用实底蓝：浅底蓝压在封面画面上读不出来"
    assert css.count("--blue-solid:") == 2, "实底蓝要有明暗两套令牌"
    assert ".dl-badge.active{color:var(--blue);background:var(--blue-soft)}" in css, \
        "行式列表的行内标签也要有「下载中」态"


def test_content_area_fills_width_without_cap():
    """内容区不许设宽度上限。

    回归:原来 `.content` 是 max-width: 1560px + margin: auto,1440 以上
    的屏两侧就各空出一条带(2560 屏每侧近 400px),卡墙被夹在中间、卡片被
    迫变小。卡墙是浏览型内容,多宽的屏只是多一列,不需要阅读宽度限制。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    content = [line for line in css.splitlines()
               if line.replace(" ", "").startswith(".content{")]
    assert content, "应存在 .content 规则"
    assert "max-width" not in content[0], \
        "内容区不该设宽度上限:宽屏两侧会留出空带,卡片也跟着变小"


def test_frontend_module_imports_all_resolve():
    """所有 ES module 的相对 import 都能在目标文件里找到对应导出。

    回归:页面拆分时 seasons.js 里的 collectionAutoControl 忘了加 export,
    fav.js 却 import 了它——浏览器端整个模块图加载失败,页面白屏,而当时
    344 个 pytest 全绿。这类断链只能在「导出/导入」层面静态校验。
    """
    import re
    js_dir = _js_dir()
    exports: dict[str, set] = {}
    for f in js_dir.rglob("*.js"):
        src = f.read_text(encoding="utf-8")
        names = set(re.findall(r"export (?:async )?function (\w+)", src))
        names |= set(re.findall(r"export (?:const|let) (\w+)", src))
        for group in re.findall(r"export \{([^}]+)\}", src):
            for part in group.split(","):
                part = part.strip()
                if " as " in part:
                    names.add(part.split(" as ")[1].strip())
                elif part:
                    names.add(part)
        exports[f.name] = names

    problems = []
    for f in js_dir.rglob("*.js"):
        src = f.read_text(encoding="utf-8")
        for blob, target in re.findall(r"import\s*\{([^}]+)\}\s*from\s*'([^']+)'", src):
            key = target.split("?")[0].lstrip("./").split("/")[-1]
            avail = exports.get(key)
            if avail is None:
                problems.append(f"{f.name}: 模块 {target} 不存在")
                continue
            for raw in blob.split(","):
                name = raw.strip().split(" as ")[0].strip()
                if name and name not in avail:
                    problems.append(f"{f.name}: 从 {target} 导入 {name} 未导出")
    assert not problems, "前端模块断链: " + "; ".join(problems)


def test_frontend_init_does_not_call_removed_functions():
    """app.js 初始化流程不得调用已删除的页面函数。

    回归:feed.syncSeg() 在页面拆分后已被删除,app.js 启动时仍调用它 →
    TypeError → 整个 init 中断,页面停在空白初始态。
    """
    import re
    app_js = (_js_dir() / "app.js").read_text(encoding="utf-8")
    init = app_js[app_js.find("async function init()"):]
    calls = re.findall(r"\b(feed|subs|lib|downloads|login|settings)\s*\.\s*(\w+)\s*\(", init)
    for alias, name in calls:
        target = {"feed": "pages/feed.js", "subs": "pages/subscriptions.js",
                  "lib": "lib.js", "downloads": "pages/downloads.js",
                  "login": "login.js", "settings": "settings.js"}[alias]
        src = (_js_dir() / target).read_text(encoding="utf-8")
        has = (f"function {name}(" in src or f"const {name}" in src
               or f"let {name}" in src)
        assert has, f"init() 调用了 {alias}.{name}，但 {target} 里没有该定义"


def test_feed_page_writes_items_into_state():
    """feed 页的 apply 必须把结果写进共享 state(否则列表永远空白)。

    回归:拆分时漏掉 replaceItems(b.items),数据拉回来了但从不渲染。
    """
    feed_js = (_js_dir() / "pages" / "feed.js").read_text(encoding="utf-8")
    apply_body = _js_func_body(feed_js, "loadFollowBili")
    assert "replaceItems(" in apply_body, "apply 必须调用 replaceItems 写入列表数据"


def test_collection_cards_are_not_buttons():
    """合集/收藏卡片容器不能用 <button>:内部嵌了清晰度 <select>。

    HTML 规范禁止交互元素嵌套,浏览器会忽略内层 select 的点击;
    回归:原实现是 <button class="fav-card"> 里塞 select。
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    for page in ("seasons.js", "fav.js"):
        src = (_js_dir() / "pages" / page).read_text(encoding="utf-8")
        for marker in ('<button type="button" class="fav-card"',
                       '<button type="button" class="fav-folder"'):
            assert marker not in src, f"{page} 仍用 button 包裹卡片: {marker}"
    # 真正的 DOM 在共享函数里:两种卡必须各自满足,不能用另一种卡的标记凑数。
    for function_name, class_name in (("collectionCard", "fav-card"),
                                      ("collectionRow", "fav-folder")):
        body = _js_func_body(lib_js, function_name)
        assert f'<div class="{class_name}" role="button" tabindex="0"' in body, \
            f"{function_name} 必须使用 div[role=button][tabindex=0],不能嵌套 select 进 button"


def test_collection_card_control_shares_title_row():
    """合集/收藏卡的画质控件与标题同一行,不再独占一行。

    回归一(2026-11-10 实拍打回):控件自占一行,前面还跟一枚类型小字
    (「追更中」/「收藏夹」),「小字标签 + 28px 描边下拉」两个不等重的块并排,
    整行像硬凑在一起。现在控件收进标题行并改浅底标签语言。
    回归二(同日):把「追更中」做成标题行内的小标签不是用户要的——他要的是
    封面右上角的角标,与关注 UP 卡的「已关注」同一套(.uc-badge.on)。
    """
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    fav_js = (_js_dir() / "pages" / "fav.js").read_text(encoding="utf-8")
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    for name, src in (("seasons.js", seasons_js), ("fav.js", fav_js)):
        assert "collectionCard" in src or "collectionRow" in src, \
            f"{name} 应复用 lib.js 的共享合集卡片"
        assert "collectionAutoControl" in src, f"{name} 的标题行应带画质控件"
    assert "fc-head" in lib_js, "共享卡片正文应为「标题 + 控件」一行"
    assert "fc-sub" not in lib_js, "共享卡片不应再有独占一行的控件行"
    # 「追更中」= 封面右上角角标,复用关注 UP 卡那一套(不是标题行内小标签)
    card = seasons_js[seasons_js.index("const seasonCard"):]
    card = card[:card.index("function seasonRow")]
    assert '<span class="uc-badge on">追更中</span>' in card, \
        "「追更中」应为封面角标,复用关注 UP 卡的 .uc-badge"
    cover = lib_js[lib_js.index('class="fc-cover"'):lib_js.index('class="fc-head"')]
    assert "${badge}" in cover, "共享卡片必须把状态角标插进封面容器"
    assert "fc-tag" not in seasons_js, "不要标题行内的「追更中」小标签"
    assert '<span class="fav-tag">追更中</span>' in seasons_js, \
        "行式列表没有封面可挂角标,保留原有的「追更中」小标签"
    assert "icon('folder') + '收藏夹'" not in fav_js, \
        "收藏页的类型小字保持移除(用户只点了「追更中」)"
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    assert ".fav-tag{" in css, "行式标签样式要保留"
    assert ".fc-tag{" not in css, "标题行标签样式应撤掉"
    assert ".uc-badge{position:absolute;top:8px;right:8px" in css, \
        "角标靠 .uc-badge 的绝对定位钉在右上角"
    # 卡内控件改标签语言:不再画 28px 描边下拉
    chip = css[css.index(".fc-head .uc-auto,"):]
    chip = chip[:chip.index("}")]
    assert "border:none" in chip and "height:20px" in chip, \
        "卡内画质控件应改浅底标签样式(无描边、20px)"
    assert "background-color:var(--bg-soft)" in chip, "静息态靠浅底表达,不靠描边"


def _css_rgb(block, token):
    """读取 CSS 块里的六位色值,供真实对比度断言使用。"""
    import re
    value = re.search(re.escape(token) + r":(#[0-9a-fA-F]{6})", block).group(1)
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _css_contrast(block, foreground, background):
    def luminance(color):
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                  for v in color]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    text_l = luminance(_css_rgb(block, foreground))
    bg_l = luminance(_css_rgb(block, background))
    return (max(text_l, bg_l) + 0.05) / (min(text_l, bg_l) + 0.05)


def test_dark_collection_quality_control_has_readable_contrast():
    """深色下「不下载」仍是可操作控件,不能退成难以辨认的装饰文字。

    这里直接按 CSS 实际引用计算 WCAG 对比度,而不是锁定某个变量名,
    允许以后换成其他同样可读的 Geist 色阶。
    """
    import re
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    dark = css.split("body.dark{", 1)[1].split("}", 1)[0]
    chip = css.split(".fc-head .uc-auto,.fav-folder .uc-auto{", 1)[1].split("}", 1)[0]
    color_name = re.search(r"(?<!-)color:var\((--[\w-]+)\)", chip).group(1)
    bg_name = re.search(r"background-color:var\((--[\w-]+)\)", chip).group(1)
    contrast = _css_contrast(dark, color_name, bg_name)
    assert contrast >= 4.5, \
        f"深色合集画质控件文字对比度仅 {contrast:.2f}:1({color_name} / {bg_name})"


def test_dark_muted_text_has_readable_contrast_on_all_surfaces():
    """三级文字仍承载 UP 签名、时间和任务 ETA,在所有深色表面都要可读。"""
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    dark = css.split("body.dark{", 1)[1].split("}", 1)[0]
    for surface in ("--bg", "--card", "--bg-soft"):
        contrast = _css_contrast(dark, "--text-mute", surface)
        assert contrast >= 4.5, \
            f"深色 --text-mute 在 {surface} 上仅 {contrast:.2f}:1"


# ---------- 复查补充:跨页状态与元素 id 一致性 ----------

def test_autorefresh_status_element_matches_consumers():
    """settings.js 读的状态元素必须真的存在于 index.html。

    回归:页面拆分时把 auto-refresh-feed-status 改名成 auto-refresh-subs-status,
    settings.js 仍按旧 id 查找 → 关注 UP 页的「检查状态」永远空白。
    """
    html = _tpl()
    settings_js = (_js_dir() / "settings.js").read_text(encoding="utf-8")
    import re
    ids = set(re.findall(r"getElementById\('(auto-refresh[^']*)'\)", settings_js))
    for element_id in ids:
        assert f'id="{element_id}"' in html, \
            f"settings.js 引用的 #{element_id} 在 index.html 中不存在"


def test_collection_subs_rerender_covers_fav_page():
    """合集订阅状态加载后要能刷新收藏页,而不是只重画合集页 DOM。

    回归:loadCollectionSubs 硬编码 renderOverview(),收藏页的开关永远停在
    「不下载」,与实际订阅状态不符。

    重画还必须是「只在收藏夹列表视图」:该回调是另一条异步请求的迟到回调,
    用户可能已经点进某个收藏夹,无条件下 renderFolders() 会把 meta-fav
    从「共 N 条」刷回「共 M 个收藏夹」。
    """
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    fav_js = (_js_dir() / "pages" / "fav.js").read_text(encoding="utf-8")
    assert "loadCollectionSubs(rerender)" in seasons_js, \
        "loadCollectionSubs 应接受调用方的重渲染回调"
    assert "typeof rerender === 'function'" in seasons_js, \
        "应按回调重渲染,缺省才回落合集页"
    assert "loadCollectionSubs(" in fav_js, \
        "收藏页应传入自己的重渲染函数"
    assert "state.mode === 'fav' && !_favCurrent" in fav_js, \
        "收藏页的重画回调必须限定在收藏夹列表视图,否则会覆盖夹内列表的 meta"


def test_collection_add_skips_rebaseline_when_already_subscribed():
    """已订阅时重复调用不再拉一次全集内容(省请求,也避免拉取失败误报 502)。"""
    import app as app_module
    src = (_js_dir().parent.parent / "app.py").read_text(encoding="utf-8")
    start = src.find("def collection_add():")
    body = src[start:src.find("@app.route(\"/api/collection/remove\"")]
    assert "existing is not None" in body, "应前置判断是否已订阅"
    assert '"already": True' in body, "已订阅应直接返回,不建立新基线"


def test_refresh_between_up_and_collection_has_interval():
    """UP 与第一个合集之间也要 sleep,否则最后一个 UP 后立刻打下一个请求。"""
    src = (_js_dir().parent.parent / "auto_refresh.py").read_text(encoding="utf-8")
    idx = src.find("# UP 与第一个合集之间也要保持间隔")
    assert idx > 0 and "time.sleep(UP_INTERVAL)" in src[idx:idx + 200]


# ---------- 复查第三轮:交互与错误处理 ----------

def test_collection_card_keydown_ignores_child_controls():
    """卡片是 div[role=button],内部有 select;键盘事件必须只认卡片自身。

    回归:select 聚焦时按 Enter 会冒泡到卡片 keydown → 直接进入合集,
    而不是打开下拉。
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    for function_name in ("collectionCard", "collectionRow"):
        body = _js_func_body(lib_js, function_name)
        assert 'onkeydown="' in body, f"{function_name} 丢失了键盘处理"
        handler = body.split('onkeydown="', 1)[1].split('"', 1)[0]
        assert handler.startswith("if(event.target!==this)return;"), \
            f"{function_name} 必须先隔离子控件事件"
        assert "event.key==='Enter'||event.key===' '" in handler, \
            f"{function_name} 必须同时支持 Enter 与 Space"
        assert "event.preventDefault()" in handler, \
            f"{function_name} 必须阻止 Space 滚动等默认行为"
        assert "${onEnter}(this.dataset.id)" in handler, \
            f"{function_name} 键盘事件必须进入对应详情"
    for page in ("seasons.js", "fav.js"):
        src = (_js_dir() / "pages" / page).read_text(encoding="utf-8")
        assert "collectionCard" in src or "collectionRow" in src, \
            f"{page} 应复用共享卡片的键盘处理"


def test_collection_add_does_not_swallow_rate_limit():
    """限流与认证变更必须原样透出,不能被包装成「读不到内容」。"""
    src = (_js_dir().parent.parent / "app.py").read_text(encoding="utf-8")
    start = src.find("def collection_add():")
    body = src[start:src.find('@app.route("/api/collection/remove"')]
    assert "except UpstreamRateLimitError:" in body, "限流应透传"
    assert "except auth.AuthContextChangedError:" in body, "认证变更应透传"
    assert "baseline_failed" in body, "真正的读取失败仍应明确报错"


def test_subscribe_success_reflected_even_if_quality_fails():
    """订阅成功但设清晰度失败时,界面仍须显示「已开启」而不是「不下载」。

    回归:先 add 后 set-quality,后者失败时直接抛错跳过状态更新,
    导致界面显示未订阅而实际已订阅。
    """
    subs_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    body = _js_func_body(subs_js, "upAutoChanged")
    patch_idx = body.find("patchUp(mid, { auto: true, quality: value });")
    quality_idx = body.find("'/api/up-list/set-quality'")
    assert patch_idx > 0, "订阅成功后应立即更新本地状态"
    assert patch_idx < quality_idx, "状态更新必须在设置清晰度之前(失败也要如实反映)"


def test_collection_items_parse_page_from_url():
    """合集条目入队时按 URL 解析分 P,不能硬编码 page=1。"""
    src = (_js_dir().parent.parent / "auto_refresh.py").read_text(encoding="utf-8")
    assert "page_from_url(it[\"url\"])" in src, "应按 URL 解析分 P"


def test_subs_extract_returns_array_for_sig():
    """订阅页的 extract 必须返回数组:sig() 对非数组一律返回 "0"。

    回归:extract 返回 {items, local_ups, total} 对象时,sig 新旧都是 "0",
    cachedLoad 永远判定「无变化」——加 UP 或订阅状态变化后页面不更新。
    """
    subs_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    body = _js_func_body(subs_js, "loadSubscriptions")
    assert "extract:" in body
    extract_part = body[body.find("extract:"):body.find("stillValid:")]
    # 只看 extract 的返回起始,不能拿数组元素的对象字面量误判
    assert "extract: (b) => [" in extract_part, \
        "extract 必须直接返回数组(供 sig 比对);返回对象会让 sig 恒为 \"0\""


def test_subs_refresh_bypasses_cache():
    """订阅页刷新按钮与加 UP 后都必须绕过缓存。

    回归:慢源数据被磁盘缓存与内存缓存挡住,用户点刷新看不到任何变化。
    """
    subs_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    html = _tpl()
    assert "force=1" in subs_js, "应支持 force 参数绕过缓存"
    assert "loadSubscriptions(true)" in subs_js, "加 UP 后应强制刷新"
    assert 'onclick="refreshSubs()"' in html, "订阅页刷新按钮应走强制刷新"
    assert 'onclick="refreshSubs()" title="刷新(绕过缓存)"' in html, \
        "按钮提示应明确这里会绕过缓存,不能只显示普通「刷新」"


def test_subs_force_refresh_applies_data_directly():
    """强制刷新必须走 fast 分支直接应用,不能只弹「获取到新内容」提示条。

    回归:cachedLoad 对慢源只在 sig 变化时弹提示,用户点刷新后列表纹丝不动。
    """
    subs_js = (_js_dir() / "pages" / "subscriptions.js").read_text(encoding="utf-8")
    body = _js_func_body(subs_js, "loadSubscriptions")
    assert "fast: forced" in body, "强制刷新应传 fast,让新数据直接应用"


def test_collection_baseline_over_limit_fails_loudly():
    """基线超限必须明确失败,不能静默截断。

    回归:add_collection 曾静默截断到上限,被裁掉的旧条目在后续检查里会被
    当成新增重复下载;而轮询侧同样超限是报错的,两侧行为不一致。
    """
    import up_list
    import pytest as _pytest
    with _pytest.raises(up_list.UpListCapacityError):
        up_list.add_collection(
            "fav", 42,
            seen_bvids=[f"BV{i}" for i in range(up_list.COLLECTION_SEEN_MAX + 1)])


def test_collection_reset_seen_route(client, monkeypatch):
    """合集基线可重置(超限或想重新对齐时用)。"""
    import app as app_module
    monkeypatch.setattr(app_module.up_list, "reset_collection_seen",
                        lambda kind, cid: True)
    r = client.post("/api/collection/reset-seen",
                    json={"kind": "season", "id": 123})
    assert r.status_code == 200 and r.get_json()["ok"] is True


# ---------- 2026-11-11 第三轮复查:显示层、反馈通道与数据口径 ----------

def test_fresh_hint_visible_state_overrides_default_hidden():
    """「获取到新内容」提示条必须能真的显示出来。

    回归:样式表里 .fresh-hint{display:none} 是静息态,而 showFreshHint 只把内联
    display 清空,等于回到 none —— 五个缓存优先页的提示条从未出现过。
    """
    html = _tpl()
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    assert ".fresh-hint{display:none" in html, "静息态仍隐藏,靠内联样式打开"
    assert "bar.style.display = 'flex'" in lib_js, \
        "显示时必须显式写 flex,置空会被静息态覆盖"


def test_save_flash_has_visible_state():
    """「已自动保存」轻提示必须有显示态样式。

    回归:settings.js 加的是 .show 类,样式表只有 opacity:0 的静息态,
    提示永远不可见。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    assert ".save-flash{display:inline-flex" in css
    assert ".save-flash.show{opacity:1}" in css, "缺显示态 → 提示永远不出现"


def test_confirm_dialog_is_centered_by_margin_auto():
    """确认弹窗必须居中:全局 *{margin:0} 覆盖了原生 dialog 的 UA margin:auto。

    回归:showModal() 弹窗贴到视口左上角(2026-11-11 实测截图)。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    block = css[css.index(".confirm-dialog{"):]
    block = block[:block.index("}")]
    assert "margin:auto" in block, "必须有 margin:auto,否则弹窗贴左上角"


def test_cover_boxes_declare_explicit_width():
    """16:9 封面容器必须显式 width:100%。

    回归:aspect-ratio 的传递尺寸在 max-height:220px 生效时会把宽度反压回
    220×16/9≈391px,列宽 >391px 的窗口(约 2 列宽)下封面右侧留白带。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    for sel in (".thumb-wrap{", ".fc-cover{", ".uc-thumb{"):
        rule = css[css.index(sel):]
        rule = rule[:rule.index("}")]
        assert "width:100%" in rule, f"{sel} 缺显式 width:100%(封面右侧会留白带)"


def test_up_list_controls_escape_avatar_clip():
    """行式 UP 卡的关注/清晰度控件必须可见,不能留在 52px 圆头像盒里被裁。

    回归:DOM 里 uc-actions 嵌在 uc-thumb 内,行式把控件设成 position:static,
    被圆盒 overflow:hidden 裁掉、再被绝对定位的头像图盖住,控件整列消失
    (2026-11-11 实测)。修法:行式让 uc-thumb 变 display:contents。
    """
    css = (_js_dir().parent / "css" / "main.css").read_text(encoding="utf-8")
    assert ".up-list .uc-thumb{display:contents}" in css, \
        "行式必须去掉 uc-thumb 自身盒子,子项才参与行的 flex 布局"
    cover = css[css.index(".up-list .uc-cover{"):]
    cover = cover[:cover.index("}")]
    assert "position:static" in cover and "52px" in cover, \
        "头像盒改由 uc-cover 承担(52px 圆形)"
    assert ".up-list .uc-actions{order:2" in css and ".up-list .uc-text{order:1" in css, \
        "顺序应为 头像 → 文本 → 控件"


def test_fav_row_shows_subscription_tag():
    """行式收藏夹行也必须能看出「追更中」。

    回归:网格卡有 .uc-badge.on、行式什么都不挂,同一页两视图订阅状态可见性
    不一致(2026-11-11 复查)。
    """
    fav_js = (_js_dir() / "pages" / "fav.js").read_text(encoding="utf-8")
    body = _js_func_body(fav_js, "folderRow")
    assert "collectionSub('fav', f.id)" in body, "行式标记应以订阅状态为条件"
    assert '<span class="fav-tag">追更中</span>' in body, \
        "订阅后应挂与合集行同款的「追更中」小标签"


def test_no_native_alert_left_in_frontend():
    """可见反馈统一走非阻塞 toast,不再用阻塞的原生 alert。

    2026-11-11 复查:35 处 alert 与自绘 UI 风格断裂,还与 announce 双通道。
    """
    html = _tpl()
    names = ["lib.js", "confirm.js", "settings.js", "login.js", "newdl.js",
             "theme.js", "drawer.js", "net.js", "util.js", "icons.js",
             "pages/home.js", "pages/downloads.js", "pages/seasons.js",
             "pages/subscriptions.js", "pages/fav.js", "pages/feed.js",
             "pages/history.js", "pages/later.js"]
    for name in names:
        src = (_js_dir() / name).read_text(encoding="utf-8")
        assert "alert(" not in src, f"{name} 仍有原生 alert 调用"
    assert "export function toast(" in html, "confirm.js 应提供 toast"
    assert ".toast{" in html and ".toast.show{" in html, "toast 样式应存在"


def test_overview_search_placeholder_matches_implementation():
    """总览搜索只按名称过滤,占位符不许承诺 UP 主/标题。"""
    html = _tpl()
    assert "搜索收藏夹名称" in html and "搜索合集名称" in html
    assert "搜索收藏夹 / 标题 / UP 主" not in html
    assert "搜索合集 / 标题 / UP 主" not in html
    fav_js = (_js_dir() / "pages" / "fav.js").read_text(encoding="utf-8")
    seasons_js = (_js_dir() / "pages" / "seasons.js").read_text(encoding="utf-8")
    assert "搜索收藏夹名称" in fav_js and "搜索合集名称" in seasons_js, \
        "进出详情时切换占位符的那份文案也要同步"
    assert "搜索标题 / UP 主" in fav_js and "搜索标题 / UP 主" in seasons_js, \
        "详情态仍应承诺标题/UP 主(那里确实按这两个字段过滤)"


def test_frontend_assets_bumped_for_fix_batch():
    """改过的静态资源版本号必须前进,避免浏览器吃旧副本。"""
    html = _tpl()
    assert "main.css?v=20261111b" in html and "app.js?v=20261111b" in html
    assert "20261111a" not in html, "旧版本号不应残留"


def test_downloads_head_has_single_new_download_entry():
    """下载记录页头不再重复一颗「新建下载」(顶栏已有唯一入口)。"""
    html = _tpl()
    page = html[html.index('data-page="downloads"'):html.index('data-page="history"')]
    assert "openDrawer('newdl')" not in page, "页头不应再有新建下载按钮"


def test_duration_seconds_parses_bilibili_length_string():
    """时长归一:数值 duration 优先,只有 length 时间串时解析它。

    回归:关注动态走 space 投稿 vlist,条目只有 length("MM:SS"),_video_item 只读
    duration → 整页没有时长角标(首页有、关注动态全没有)。
    """
    import bilibili_api as bapi
    assert bapi._duration_seconds({"duration": 300}) == 300
    assert bapi._duration_seconds({"duration": "300"}) == 300
    assert bapi._duration_seconds({"length": "12:34"}) == 754
    assert bapi._duration_seconds({"length": "1:02:03"}) == 3723
    assert bapi._duration_seconds({"length": 300}) == 300
    assert bapi._duration_seconds({"length": ""}) == 0
    assert bapi._duration_seconds({}) == 0
    assert "_duration_seconds(it)" in __import__("inspect").getsource(bapi._video_item), \
        "_video_item 的时长必须走统一归一"


# ---------- 本轮走查修复(2026-11-01):force 参数 / 二维码容量 / 空基线 / 401 ----------

def test_followings_accepts_force_1(client, monkeypatch):
    """订阅页刷新按钮发 ?force=1:_boolean 必须认 1/0,不能 400。

    回归:_boolean 只认 true/false,前端 force=1 直接 400 invalid_field,
    强制刷新失效且用户只看到「force 必须是 true 或 false」。
    """
    import app as app_module
    monkeypatch.setattr(app_module.bapi, "fetch_followings_all",
                        lambda **kw: {"items": [], "total": 0})
    r = client.get("/api/followings?force=1")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/api/followings?force=0").status_code == 200


def test_qrcode_image_rejects_oversized_url(client):
    """合法护照域名但超出二维码编码容量:必须 400,不能落成 500。

    qrcode 库对超长输入抛 ValueError,此前未被接住,走 api_errors 兜底
    变成 internal_error + 异常日志。
    """
    long_url = "https://passport.bilibili.com/" + "a" * 3000
    r = client.get("/api/login/qrcode-image?url=" + long_url)
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_url"


def test_collection_add_rejects_empty_baseline(client, monkeypatch):
    """订阅当时为空的合集必须拒绝:空基线会让之后新增的条目被当基线吞掉,
    追更静默失效且用户没有任何自救路径。"""
    import app as app_module
    monkeypatch.setattr(app_module.bapi, "fetch_season_content",
                        lambda cid, **kw: [])
    r = client.post("/api/collection/add",
                    json={"kind": "season", "id": 123, "title": "空合集"})
    body = r.get_json()
    assert r.status_code == 400 and body["code"] == "empty_collection"
    assert "未建立订阅" in body["error"] or "无法建立基线" in body["error"]


def test_not_logged_in_maps_to_401(client, monkeypatch):
    """未登录是预期态:401 auth_required,不是 500 internal_error。

    此前 RuntimeError("未登录…") 落进兜底 except Exception,前端提示
    「服务器处理失败,请查看应用日志」,离「请先登录」差很远。
    """
    import app as app_module

    def boom(**kw):
        raise app_module.bapi.NotLoggedInError("未登录,无法获取关注列表")

    monkeypatch.setattr(app_module.bapi, "fetch_followings_all", boom)
    r = client.get("/api/followings")
    assert r.status_code == 401
    body = r.get_json()
    assert body["code"] == "auth_required"
    assert "未登录" in body["error"]
