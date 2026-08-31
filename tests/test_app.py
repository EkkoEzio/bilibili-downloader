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
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen", lambda mid: None)
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
    auto_refresh 换成谓词桩,单独锁住**路由自己的分支** —— 它必须先问过
    is_running() 再决定要不要起线程,而不是无条件 start() 之后靠 refresh_once
    里那次非阻塞 acquire 兜底。差别是真实的:无条件起线程 = 每次点击都白起一个
    线程,响应还乐观地报 started=True。

    `is_running` 由任务 12.1 引入,现在还不存在,所以 setattr 用 raising=False:
    默认的 raising=True 会让 monkeypatch 自己抛 AttributeError,失败点变成
    「打桩失败」;而本条要暴露的是「路由的响应里没有 started」。用 raising=False
    打进去(未修复的路由压根不读它),失败就落在下面那条 started 断言上,
    反例一眼可读。

    Validates: Requirements 2.10
    """
    import app as app_module
    calls = []
    monkeypatch.setattr(app_module.auto_refresh, "is_running", lambda: True,
                        raising=False)
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
        "is_running() 为真时路由不该起后台线程、更不该调 refresh_once;"
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
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen", lambda mid: None)
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

    def blocking_refresh():
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

# ---------- 标准列表渲染:勾选下标必须是 state.items 的原始下标(缺陷 B1) ----------

def _js_func_body(src: str, name: str) -> str:
    """截出 JS 源码里 `function <name>(...) { ... }` 的函数体(含花括号)。

    整包子串断言看不出「谁在调用」——`visiblePairs(` 出现在拼接串里不等于
    renderDefaultList 真的用了它。这里按花括号配平把断言收窄到具体函数。
    目标函数体内的模板串只含配平的 `${...}`,计数法足够。"""
    i = src.find(f"function {name}(")
    assert i >= 0, f"lib.js 里找不到函数 {name}"
    start = src.index("{", i)
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"函数 {name} 花括号不配平")


def test_frontend_default_list_uses_original_index():
    """标准列表渲染发出的 data-libidx 必须是条目在 state.items 里的原始下标。

    Property 1: Bug Condition —— 勾选下标必须能定位回它所代表的条目。
    对应 isBugCondition_B1:搜索词非空且过滤确实改变了列表。

    静态推演(前端无 JS 运行时,行为侧证据靠读码给出;
    读 static/js/lib.js 的 renderDefaultList() 与
    static/js/pages/downloads.js 的 delSelected()):

    设下载记录页 state.items = [A, B, C, D],搜索词只命中 C、D。
      1. 输入搜索词 → applyLibFilter() → renderPage() → renderDefaultList()。
      2. renderDefaultList() 里 filtered = [C, D],source = filtered,
         `source.forEach((it, idx) => pairs.push({ it, idx }))`
         → pairs = [{it: C, idx: 0}, {it: D, idx: 1}]。
         forEach 的第二个参数是「第几个命中」,与 state.items 的下标无关。
      3. renderLibList() 把该 idx 当 origIdx 用,给 C 渲染出
         `data-libidx="0"` 和 `onchange="toggleLibCheck(0, this.checked)"`
         —— C 在 state.items 里其实是第 2 个(下标 2)。
      4. 勾选 C → toggleLibCheck(0, true) → state.checked.add(0)。
      5. 点「删除记录 + 文件」→ downloads.delSelected(true) 从 DOM 读
         `input[data-libidx]:checked`,再取
         `state.items[parseInt(cb.dataset.libidx)]` → 拿到 state.items[0] = A。
      6. POST /api/task/delete {ids: [A.task_id], delete_file: true}
         → taskstore.delete_tasks() 删掉 A 的记录,并对 _task_files(A) 整族
         unlink(视频 + 封面 + 字幕 + 弹幕)。C 完好无损,A 的文件不可恢复。

    同一条错位在其它页表现为:勾选第 2 个命中项 → libDownload() 提交
    state.items[1](另一个视频);先勾选 A(checked={0})再搜索命中 [C, D]
    → C 拿到 origIdx=0 → `state.checked.has(0)` 为真 → C 被高亮成「已勾选」,
    而真实选择是 A。

    无搜索词时 source 是 state.items.slice(0, shown),从 0 起的前缀切片,
    位置恰好等于原始下标 —— 巧合正确,这正是缺陷躲过既有用例的原因。

    修复方向:pairs 构造收口成 visiblePairs(),遍历 state.items 本身,
    idx 天然是原始下标(见 design.md「Fix Implementation → B1 携带原始下标」)。

    Validates: Requirements 2.1, 2.2, 2.3
    """
    html = _tpl()
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    rdl = _js_func_body(lib_js, "renderDefaultList")
    live = [ln.strip() for ln in rdl.splitlines() if ".forEach((it, idx)" in ln]

    # 1) pairs 构造必须收口成唯一来源 visiblePairs()
    assert "visiblePairs" in html, (
        "缺少 pairs 构造的唯一来源 visiblePairs();renderDefaultList 当前是:"
        + (live[0] if live else "(未找到 pairs 构造行)")
        + " —— 搜索态下发出的是命中序号,删除/下载会打到 state.items 里同下标的另一条")

    # 2) 唯一来源必须遍历 state.items 本身,idx 才是原始下标
    vp = _js_func_body(lib_js, "visiblePairs")
    assert "state.items.forEach((it, idx)" in vp, (
        "visiblePairs 必须遍历 state.items 本身;对过滤/切片后的数组取遍历位置"
        "得到的是「第几个命中」,不是原始下标")

    # 3) 对「过滤后 / 切片后的数组」取遍历位置当原始下标的写法必须绝迹
    assert "source.forEach((it, idx)" not in html, (
        "仍在对 `q ? filtered : slice(...)` 的结果取遍历位置当原始下标(缺陷 B1 现场)")
    assert "filtered.forEach" not in html, (
        "不得对过滤后的数组取遍历位置当原始下标")

    # 4) 两个渲染入口都走同一份 pairs 构造,不留第二份规则
    for fn in ("renderDefaultList", "advanceDefaultChunk"):
        assert "visiblePairs(" in _js_func_body(lib_js, fn), \
            f"{fn} 应复用 visiblePairs(),不自建 pairs"

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


def test_frontend_check_index_consumers_read_original_index():
    """勾选下标的消费方一直按「原始下标索引 state.items」工作,B1 修复不得改动。

    Property 7: Preservation —— 无搜索词时的勾选与批量操作。

    这几条是 B1 修复的**前提条件**而非修复内容:pairs 里的 idx 之所以必须是
    原始下标,正是因为下游一路都拿它当 state.items 的下标用。观察未修复代码
    (static/js/lib.js 的 renderLibList、static/js/pages/downloads.js 的
    delSelected / libDownload)确认这条链现状:

      renderLibList 把 pairs 的 idx 命名为 origIdx,写进 data-libidx、
      写进 onchange="toggleLibCheck(origIdx, ...)"、并用 state.checked.has(origIdx)
      决定高亮 → delSelected / libDownload 从 DOM 读回 :checked 复选框,
      再 state.items[parseInt(cb.dataset.libidx)] 取回条目。

    两个批量操作都从 **DOM** 读 :checked(而不是遍历 state.checked),
    因此「先勾选 A、再搜索只命中 C/D」时 A 不在 DOM 里、不会被误删/误提交 ——
    这正是 2.1 / 2.2 要求的「只作用于可见且勾选的条目」,修复前后一致。

    锁住:3.1(无搜索词时精确删除所勾选条目的记录与文件)
          3.2(无搜索词时批量下载提交所勾选的视频)

    Validates: Requirements 3.1, 3.2
    """
    import re
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")

    # 1) renderLibList 是 data-libidx 的唯一发出点,且发的是 pairs 的 origIdx
    rll = _js_func_body(lib_js, "renderLibList")
    assert 'data-libidx="${origIdx}"' in rll, "复选框的 data-libidx 应写入 pairs 的原始下标"
    assert "onchange=\"toggleLibCheck(${origIdx}, this.checked)\"" in rll, \
        "onchange 应把原始下标交给 toggleLibCheck"
    assert "state.checked.has(origIdx)" in rll, \
        "高亮判定应按原始下标查 state.checked(选择状态才不会转移到别的条目)"
    emitters = {p.relative_to(_js_dir()).as_posix()
                for p in sorted(_js_dir().rglob("*.js"))
                if 'data-libidx="' in p.read_text(encoding="utf-8")}
    assert emitters == {"lib.js"}, \
        f"data-libidx 应只由 lib.js 的 renderLibList 发出,现在还有:{emitters}"

    # 2) 「删除记录 + 文件」:从 DOM 读 :checked → 按原始下标取 state.items
    ds = _js_func_body(dl_js, "delSelected")
    assert "input[data-libidx]:checked" in ds, "应从 DOM 读勾选(而非遍历 state.checked)"
    assert "state.items[parseInt(cb.dataset.libidx)]" in ds, \
        "应按 data-libidx 当原始下标索引 state.items"
    assert ".task_id" in ds and "/api/task/delete" in ds, \
        "删除的目标是所勾选条目自己的 task_id"
    assert re.findall(r"state\.checked\.(\w+)", ds) == ["clear"], \
        "delSelected 只在删完后清空 state.checked,不得改成从 state.checked 取待删条目"

    # 3) 批量下载:同一取法
    ld = _js_func_body(dl_js, "libDownload")
    assert "input[data-libidx]:checked" in ld, "应从 DOM 读勾选"
    assert "state.items[parseInt(cb.dataset.libidx)]" in ld, \
        "应按 data-libidx 当原始下标索引 state.items"
    assert "state.checked" not in ld, \
        "批量下载不读 state.checked(它含被搜索隐藏的勾选,会提交不可见条目)"


def test_frontend_check_toggles_stay_dom_scoped():
    """勾选切换与「全选」一直只作用于当前可见条目、按原始下标写 state.checked。

    Property 7: Preservation —— 无搜索词时的勾选与批量操作。

    观察未修复代码(static/js/lib.js):
      - toggleLibCheck(origIdx, checked) 直接 state.checked.add/delete(origIdx),
        再按 data-libidx 反查该复选框所在卡片切 .checked 类,最后 updateLibCount()。
      - libToggleAll 遍历的是 `.page.active .video-item`(DOM),不是 state.items ——
        被搜索词过滤掉的条目压根不在 DOM 里,所以「全选」天然只覆盖可见条目。
        它从 cb.dataset.libidx 取原始下标写 state.checked。
      - 封面点选:委托到 document 的处理器先翻 cb.checked(让计数看到新状态),
        再 toggleLibCheck(parseInt(cb.dataset.libidx), cb.checked)。
      - updateLibCount 的计数来自 DOM `input[data-libidx]:checked`,不是
        state.checked.size —— 底部操作条显示的是「可见且勾选」的条数。

    B1 只改 pairs 构造,这四处一字不动。

    锁住:3.3(「全选」只作用于当前可见条目)
          3.4(点击封面图切换勾选并同步底部操作条计数)

    Validates: Requirements 3.3, 3.4
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")

    # 1) 单条切换:按原始下标进出 state.checked,并同步计数
    tlc = _js_func_body(lib_js, "toggleLibCheck")
    assert "state.checked.add(origIdx)" in tlc and "state.checked.delete(origIdx)" in tlc, \
        "勾选集应按原始下标增删"
    assert 'input[data-libidx="${origIdx}"]' in tlc, "应按原始下标反查该条目的复选框"
    assert "updateLibCount()" in tlc, "切换后应同步底部操作条计数"

    # 2) 「全选」:遍历 DOM 可见卡片,不遍历 state.items
    lta = _js_func_body(lib_js, "libToggleAll")
    assert ".page.active .video-item" in lta, "「全选」应遍历 DOM 里的可见卡片"
    assert "state.items" not in lta, \
        "「全选」不得遍历 state.items(那会把被搜索词过滤掉的条目也选上,破坏 3.3)"
    assert "parseInt(cb.dataset.libidx)" in lta, "应从 data-libidx 取原始下标"
    assert "state.checked.add(idx)" in lta and "state.checked.delete(idx)" in lta
    assert "updateLibCount()" in lta

    # 3) 封面点选:翻 DOM 再按原始下标切换
    thumb = _js_listener_body(lib_js, ".video-item .thumb-wrap")
    assert "input.vi-check" in thumb, "应定位卡片内的勾选框"
    assert "cb.checked = !cb.checked" in thumb, \
        "应先翻 DOM 再调 toggleLibCheck,否则 updateLibCount 从 DOM 数到的是旧状态"
    assert "toggleLibCheck(parseInt(cb.dataset.libidx), cb.checked)" in thumb, \
        "封面点选应把原始下标交给 toggleLibCheck"

    # 4) 计数口径:DOM 里「可见且勾选」的条数
    ulc = _js_func_body(lib_js, "updateLibCount")
    assert "input[data-libidx]:checked" in ulc, "计数应数 DOM 里的可见勾选"
    assert "state.checked" not in ulc, \
        "计数不得用 state.checked.size(它含被搜索隐藏的勾选,与操作条实际会处理的条数不符)"
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


def test_frontend_downloads_defers_reload_while_checked():
    """下载记录页在有勾选期间推迟整体重拉、清零后补上,B1 修复不得动这套机制。

    Property 7: Preservation —— 无搜索词时的勾选与批量操作。

    观察未修复代码(static/js/pages/downloads.js):
      - maybeReload():DOM 里还有 `input[data-libidx]:checked` 时只置
        `_dirty = true` 并 return,不调 loadDownloads() —— 重拉会重排 state.items,
        已发出的原始下标全部失效。SSE 进度到达终态与任务卡不在页面上时都走这里。
      - pollTasks()(30 秒兜底轮询):同一个 DOM 勾选判断,有勾选就不刷列表,
        只更新侧边栏角标。
      - setItemExtras 注入的 onChecksEmpty:_dirty 为真时补上 loadDownloads()。
      - lib.updateLibCount() 在计数归零时回调 onChecksEmpty —— 这是「补上」的触发点。

    锁住:3.6(有勾选期间收到 SSE 进度或轮询结果 → 推迟整体重拉,清零后补上)

    Validates: Requirements 3.6
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")

    # 1) SSE 触发的重拉:有勾选就只打脏标记
    mr = _js_func_body(dl_js, "maybeReload")
    assert "input[data-libidx]:checked" in mr, "重拉前应检查 DOM 里是否还有勾选"
    assert "_dirty = true" in mr, "有勾选时应只打脏标记"
    dirty_at, load_at = mr.index("_dirty = true"), mr.index("loadDownloads(")
    assert dirty_at < load_at, "脏标记分支必须在 loadDownloads() 之前 return,否则照样重排"
    assert "return;" in mr[dirty_at:load_at], "打完脏标记必须 return,不能穿到重拉"

    # 2) 30 秒兜底轮询:同一个勾选判断
    pt = _js_func_body(dl_js, "pollTasks")
    assert "!document.querySelectorAll('.page.active input[data-libidx]:checked').length" in pt, \
        "轮询刷新列表前应检查无勾选"
    assert "updateBadge(tasks)" in pt, "有勾选时仍应更新侧边栏角标(只是不刷列表)"

    # 3) 勾选清零 → 补上被推迟的重拉
    hook_at = dl_js.index("onChecksEmpty:")
    hook = dl_js[hook_at:dl_js.index("},", hook_at)]
    assert "_dirty" in hook and "loadDownloads()" in hook, \
        "onChecksEmpty 应在脏标记为真时补上被推迟的重拉"
    assert "_dirty = false" in hook, "补拉后应清掉脏标记"
    ulc = _js_func_body(lib_js, "updateLibCount")
    assert "n === 0 && _itemExtras.onChecksEmpty" in ulc, \
        "updateLibCount 应在计数归零时回调 onChecksEmpty(这是补拉的唯一触发点)"


def test_frontend_default_list_inits_shown_before_window():
    """非搜索态的可见窗口一直是 state.items 的 state.shown 前缀,且 shown 先于取窗初始化。

    Property 7: Preservation —— 无搜索词时的勾选与批量操作。

    观察未修复代码(static/js/lib.js 的 renderDefaultList):
        if (!state.shown) state.shown = curChunk();
        const source = q ? filtered : state.items.slice(0, state.shown);
        source.forEach((it, idx) => pairs.push({ it, idx }));
    非搜索态取的是从 0 起的前缀切片,遍历位置恰好等于原始下标 —— 巧合正确,
    这正是缺陷 B1 躲过既有 183 个用例的原因。这条前缀语义(以及它依赖的
    「state.shown 已初始化」)是修复后必须逐位不变的基线。

    本断言故意写成不认实现形态:前缀截断写成 slice(0, state.shown) 还是
    `idx < state.shown` 都接受,只要求
      (a) 存在按 state.shown 的前缀截断 —— 否则非搜索态会渲染全量或渲染为空;
      (b) state.shown 的初始化早于**读取它取窗**的那一步 —— design.md 与任务 3.2
          都点名了这个坑:改用 visiblePairs() 后若 shown 初始化仍留在取窗之后,
          首次渲染 state.shown 为 0、`idx < 0` 恒假,列表会空。
          注意基准点必须是「读 state.shown 取窗」而不是「forEach 建对」:
          未修复形态里取窗在 `const source = ...` 那行、建对在下一行,
          拿建对当基准会让「init 挪到取窗之后」这类回归漏过去。

    修好原始下标后的「visiblePairs('') 与前缀切片逐位等价」由
    test_frontend_visible_pairs_equals_prefix_slice 承接(函数存在后才可检查)。

    Validates: Requirements 3.1, 3.2, 3.3
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    rdl = _js_func_body(lib_js, "renderDefaultList")
    window_path = rdl
    if "function visiblePairs(" in lib_js:
        window_path += _js_func_body(lib_js, "visiblePairs")

    # (a) 非搜索态仍按 state.shown 截前缀
    bounds = [form for form in ("state.items.slice(0, state.shown)", "idx < state.shown")
              if form in window_path]
    assert bounds, ("非搜索态的可见窗口必须仍是 state.items 的 state.shown 前缀"
                    "(slice(0, state.shown) 或 idx < state.shown);"
                    "丢了这层截断,首屏会渲染全量或渲染为空")

    # (b) state.shown 初始化必须早于取窗
    shown_at = rdl.find("state.shown = curChunk()")
    assert shown_at >= 0, "renderDefaultList 应负责 state.shown 的首屏初始化"
    window_marks = [i for i in (rdl.find("visiblePairs("),
                                rdl.find("state.items.slice(0, state.shown)")) if i >= 0]
    assert window_marks, "renderDefaultList 里找不到取窗那一步(visiblePairs / 前缀切片)"
    assert shown_at < min(window_marks), \
        ("`if (!state.shown) state.shown = curChunk();` 必须在读 state.shown 取窗之前;"
         "留在之后则非搜索态首次渲染 state.shown 为 0,列表会空")


def test_frontend_visible_pairs_equals_prefix_slice():
    """visiblePairs('') 必须与 state.items.slice(0, state.shown) 的建对逐位等价。

    Property 7: Preservation —— 无搜索词时的勾选与批量操作。

    这是 B1 的结构性不变量:非搜索态下修复前后产出的 pairs 必须逐位相同
    (修复前是 `state.items.slice(0, state.shown).forEach((it, idx) => ...)`)。
    等价的充要条件是三点同时成立 ——
      1. 按原顺序遍历 state.items 本身(不排序、不反转、不先过滤再取位置);
      2. 非搜索态的入选条件是原始下标的前缀谓词 `idx < state.shown`;
      3. 入选时带的下标就是 forEach 给的那个 idx(不另起计数器)。
    三点齐备时,选中的恰是原始下标 0..state.shown-1 且顺序不变,
    与前缀切片的建对结果逐位相同。

    visiblePairs() 由任务 3.1 引入;函数还不存在时本条跳过(任务 2 的基线里
    只锁「当前已正确的行为」,不产出第二条预期失败的测试),任务 3.5
    重跑这一批时它会自动生效。

    Validates: Requirements 3.1, 3.2, 3.3
    """
    lib_js = (_js_dir() / "lib.js").read_text(encoding="utf-8")
    if "function visiblePairs(" not in lib_js:
        pytest.skip("visiblePairs() 尚未引入(任务 3.1),该结构性不变量由任务 3.5 验证")
    vp = _js_func_body(lib_js, "visiblePairs")

    # 1) 按原顺序遍历 state.items 本身
    assert "state.items.forEach((it, idx)" in vp, \
        "必须遍历 state.items 本身,idx 才是原始下标"
    for bad in (".sort(", ".reverse(", ".filter(", ".slice("):
        assert bad not in vp, f"visiblePairs 内不得 {bad} 重排/重新索引 state.items"

    # 2) 非搜索态的入选条件是原始下标的前缀谓词
    assert "idx < state.shown" in vp, \
        "非搜索态应按原始下标截前缀(idx < state.shown),这才等价于 slice(0, shown)"

    # 3) 带的下标就是 forEach 给的 idx
    assert "pairs.push({ it, idx })" in vp, "入选时应原样带上 forEach 的 idx"
    assert "pairs.length" not in vp, "不得用 pairs.length 之类的另起计数器当下标"

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
    P2–P5 全部命中 → 过滤后 items 为空 → 前端 `alert('所选项均已下载过')`,
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
    assert "_itemExtras.isDownloaded(it.url)" in lib_js, \
        "renderLibList 的「已下载」角标应走同一份判定"
    assert "state.mode !== 'downloads'" in lib_js, \
        "角标只在内容页挂;下载记录页本身不需要(那里渲染的是任务卡)"

    # 3) 批量提交过滤:两个入口都要「统计跳过数 + 过滤 + 全跳过时提示」
    for name, src, pred in (("newdl.js", newdl_js, "isUrlDownloaded"),
                            ("pages/downloads.js", dl_js, "isDownloaded")):
        assert f"items.filter(it => {pred}(it.url)).length" in src, \
            f"{name} 应统计被跳过的已下载条目数(用户要看到「跳过 N 个已下载」)"
        assert f"items.filter(it => !{pred}(it.url))" in src, \
            f"{name} 的批量提交必须过滤掉已下载条目(3.13),否则重复下载同一文件"
        assert "alert('所选项均已下载过')" in src, \
            f"{name} 应在全部条目都已下载时明确提示,而不是静默提交空列表"
        assert "跳过 ${skipped} 个已下载" in src, \
            f"{name} 应在成功提示里回报跳过数量"


# ---------- 提交下载时的 page 维度:两个批量入口都不许丢(缺陷 C2 / C3) ----------

def test_frontend_lib_download_carries_page():
    """libDownload 提交体里的 page 必须是条目真实分 P,不许硬编码 1(缺陷 C2)。

    Property 3 的延伸 —— 已下载判定加了 (bvid, page) 粒度,**提交**却仍丢 page。

    缺陷表现:在「下载记录」页勾一条 P3 的记录重新下载,实际下回来的是 P1。
    根因是两处配套的缺失,单看任一处都像没问题:

      1. downloads.js 的 loadDownloads() 用**字段白名单**构造 state.items,
         `page` 不在白名单里 → 任务记录的 `it.page` 是 undefined,权威值在
         /api/tasks 的响应里躺着却没被带出来;
      2. libDownload() 的提交体写死 `page: 1`。

      于是后端 build_filename(title, page) 按 P1 拼文件名、
      find_active_by_url(url, label, page) 也按错的 page 判去重 —— 全程不报错,
      用户只会发现「下载下来的不是我点的那一集」。

    libDownload 是六个内容页共用的批量提交口,修法必须同时照顾两类条目
    (断言 3 锁的就是这个 `||` 兜底):

      - **任务记录**(下载记录页):走 `it.page`。不能解析 `it.url` ——
        任务表里的 url 是用户粘贴的原始链接,常不带 `?p=`,解析它会把所有
        任务都算成 P1,与硬编码 1 等价,缺陷原地复发。
      - **接口条目**(收藏夹 / 稍后再看 / 观看历史 / 关注动态 / 追更合集):
        没有 page 字段 → 退到 `pageFromUrl(it.url)`。这五页的条目 url 由
        bilibili_api.py 拼成 `https://www.bilibili.com/video/{bvid}`,从不带
        `?p=` → 得 1,与修复前逐位相同,不引入回归。

    取 page 的方式沿用 B3 定下的不对称原则(任务侧读存储字段 / 查询侧解析 URL),
    键规则唯一来源仍是 util.js,这里只 import 不复制。

    Validates: Requirements 2.6, 2.7, 3.13
    """
    dl_js = (_js_dir() / "pages" / "downloads.js").read_text(encoding="utf-8")

    # 1) 白名单必须把 page 带出来 —— 少了这行,it.page 永远 undefined
    load_body = _js_func_body(dl_js, "loadDownloads")
    assert "page:" in load_body, (
        "loadDownloads 的字段白名单缺 page:任务记录的权威分 P 值(/api/tasks 已返回)"
        "没被带进 state.items,libDownload 拿到的 it.page 是 undefined")
    assert "page: t.page || 1," in load_body, (
        "白名单里的 page 应读 t.page(缺失兜底 1),这是 B3 定下的「任务侧读存储字段」原则;"
        "从 t.url 解析会把所有任务算成 P1")

    # 2) 提交体不许再硬编码 page: 1
    lib_body = _js_func_body(dl_js, "libDownload")
    assert "page: 1" not in lib_body, (
        "libDownload 仍硬编码 page: 1(缺陷 C2 现场):下载记录页重下一条 P3 记录时,"
        "后端会按 P1 拼文件名并按错的 page 判去重,实际下载的是 P1")

    # 3) 一个 || 兜底同时服务任务记录与接口条目
    assert "page: it.page || pageFromUrl(it.url)," in lib_body, (
        "libDownload 的 page 应取 `it.page || pageFromUrl(it.url)`:"
        "任务记录走 it.page(不能解析 it.url——它是用户粘贴的原始链接),"
        "其余五个内容页的接口条目无该字段、退到 pageFromUrl(其 url 不带 ?p= → 得 1,无回归)")

    # 4) 键规则仍只有一份:pageFromUrl 从 util.js 引入,不得在本文件复制
    assert "function pageFromUrl" not in dl_js, \
        "downloads.js 不得自建第二份 pageFromUrl,必须从 ../util.js 引入"


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
    feed_js = (_js_dir() / "pages" / "feed.js").read_text(encoding="utf-8")
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
