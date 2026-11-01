"""auto_refresh.py:自动下载(串行防风控 + 增量拉取 + 跳过已下载 + 限流不推进 + 直接入队)。"""
import threading
import time

import pytest

import auto_refresh
import downloader
import taskstore


def _vid(bvid, ts):
    return {"bvid": bvid, "title": f"视频{bvid}", "uploader": "UP", "thumbnail": "",
            "view_at": ts, "url": f"https://www.bilibili.com/video/{bvid}"}


def _patch_env(monkeypatch, ups, fetch_by_mid, last_seen=None,
               stub_is_downloaded=True, collections=None):
    """打桩:UP 清单 / 合集订阅 / arc/search / last_seen 读写 / 批量下载 / sleep。

    `stub_is_downloaded=True`(默认)沿用原行为:把 downloader.is_downloaded
    打成恒 False 的桩。这让上面每个用例都能只关心自己那条不变量,但副作用是
    自动下载这条路径**从不执行真实判定** —— 见文件末尾「真实已下载判定」一节。
    传 False 则不打这个桩,让 refresh_once 走 taskstore.is_downloaded 的真身。

    `collections` 默认空列表:`refresh_once` 也会处理合集订阅,不打桩就会去读
    (并请求)真实订阅数据,UP 之间的间隔断言也会被合集的间隔污染。
    """
    batches, marked, sleeps = [], [], []

    def fake_list_ups():
        return ups

    def fake_list_collections():
        return list(collections or [])

    def fake_fetch(mid, ps=10, pn=1):
        return fetch_by_mid.get(int(mid), [])

    def fake_start_batch(items, label, max_concurrent=None):
        batches.append(([v["bvid"] for v in items], label, max_concurrent))
        return ["t"] * len(items)

    monkeypatch.setattr(auto_refresh.up_list, "list_ups", fake_list_ups)
    monkeypatch.setattr(auto_refresh.up_list, "list_collections",
                        fake_list_collections)
    monkeypatch.setattr(auto_refresh.bapi, "fetch_up_videos", fake_fetch)
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen_all",
                        lambda: {str(k): v for k, v in (last_seen or {}).items()})
    monkeypatch.setattr(auto_refresh.up_list, "mark_seen",
                        lambda mid, bvid, ts, title="", fetched_at=None:
                            marked.append((int(mid), bvid, ts, fetched_at)))
    monkeypatch.setattr(auto_refresh.downloader, "start_batch", fake_start_batch)
    if stub_is_downloaded:
        monkeypatch.setattr(auto_refresh.downloader, "is_downloaded", lambda url: False)
    monkeypatch.setattr(auto_refresh.time, "sleep", lambda s: sleeps.append(s))
    return batches, marked, sleeps


def test_all_listed_ups_downloaded(monkeypatch):
    """加入清单 = 自动下载:清单里所有 UP 都检查并入队,不再看 auto 开关。"""
    ups = [{"mid": 1}, {"mid": 2, "auto": False}]  # 老数据的 auto 字段被无视
    batches, marked, _, = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BVnew1", 200)], 2: [_vid("BVnew2", 200)]})
    n = auto_refresh.refresh_once()
    assert n == 2
    assert batches == [(["BVnew1"], "", 1), (["BVnew2"], "", 1)], \
        "清单内 UP 全部入队,串行(并发=1),默认清晰度为空(自动)"
    assert [m[:3] for m in marked] == [(1, "BVnew1", 200), (2, "BVnew2", 200)]
    assert all(m[3] for m in marked), "mark_seen 应带拉取时间 fetched_at"


def test_quality_label_passed_to_batch(monkeypatch):
    """每个 UP 的默认清晰度配置要传给下载批次。"""
    ups = [{"mid": 1, "uname": "A", "quality": "720P"}, {"mid": 2, "uname": "B"}]
    batches, _, _ = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BV1", 100)], 2: [_vid("BV2", 100)]})
    auto_refresh.refresh_once()
    assert [b[1] for b in batches] == ["720P", ""], "按 UP 的 quality 传清晰度,未配置为自动"


def test_skips_seen_and_downloaded(monkeypatch):
    ups = [{"mid": 1}]
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
    assert batches == [(["BVnew"], "", 1)]
    assert marked[0][:3] == (1, "BVnew", 300), "mark_seen 用最新一条"


def test_rate_limited_up_skipped_and_not_marked(monkeypatch):
    ups = [{"mid": 1}]
    batches, marked, _ = _patch_env(monkeypatch, ups, {1: []})
    assert auto_refresh.refresh_once() == 0
    assert batches == []
    assert marked == [], "限流返回空不得推进 last_seen"


def test_serial_interval_between_ups(monkeypatch):
    ups = [{"mid": 1}, {"mid": 2}, {"mid": 3}]
    batches, _, sleeps, = _patch_env(
        monkeypatch, ups,
        {1: [_vid("BV1", 1)], 2: [], 3: [_vid("BV3", 3)]})
    auto_refresh.refresh_once()
    expected = [auto_refresh.UP_INTERVAL] * (len(ups) - 1)
    assert sleeps == expected, "UP 之间应有防风控间隔"
    assert auto_refresh.UP_INTERVAL >= 5.0, "合规要求:自动任务请求间隔不低于 5 秒"


def test_compliance_doc_matches_rate_limits():
    """COMPLIANCE.md 承诺的限频数字必须与代码常量一致。

    回归:文档长期停在「UP 之间 ≥3 秒」,代码早已收紧到 5.0——合规文档写的比
    实现松,对外承诺就失真了。两边任一处改动都应在这里对上。
    """
    import re
    from pathlib import Path
    import config
    doc = (Path(__file__).resolve().parent.parent / "docs" / "COMPLIANCE.md"
           ).read_text(encoding="utf-8")

    m = re.search(r"UP 之间请求间隔 \*\*≥ (\d+(?:\.\d+)?) 秒\*\*", doc)
    assert m, "COMPLIANCE.md 未找到 UP 间隔的承诺"
    assert float(m.group(1)) == auto_refresh.UP_INTERVAL, (
        f"文档写 ≥{m.group(1)}s,代码是 {auto_refresh.UP_INTERVAL}s")

    m2 = re.search(r"轮询间隔下限 \*\*(\d+) 分钟\*\*", doc)
    assert m2, "COMPLIANCE.md 未找到轮询下限的承诺"
    assert int(m2.group(1)) == config.AUTO_REFRESH_MIN, (
        f"文档写 {m2.group(1)} 分钟,代码是 {config.AUTO_REFRESH_MIN} 分钟")

    # v2.0 已取消「追更开关 + 待确认」,文档不该再提勾选式追更开关
    assert "自己主动勾选" not in doc, "文档仍在描述已废弃的追更勾选流程"


def test_first_run_downloads_everything(monkeypatch):
    """从未 mark_seen 过的 UP:全部视为新增。"""
    ups = [{"mid": 1}]
    batches, _, _ = _patch_env(monkeypatch, ups, {1: [_vid("BVa", 1), _vid("BVb", 2)]})
    n = auto_refresh.refresh_once()
    assert n == 2
    assert batches == [(["BVa", "BVb"], "", 1)]


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

    monkeypatch.setattr(auto_refresh.bapi, "fetch_up_videos", fake_fetch)
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
    """爆发更新会翻页；跨轮 offset 漂移也必须先闭合低页接缝再提交。"""
    burst = [_vid(f"BVb{i}", 500 + i) for i in range(5)]          # 第1页:5条全新
    older = [_vid("BVold", 100), _vid("BVolder", 50)]             # 第2页:含已见
    calls = _patch_fetch_calls(monkeypatch, {(1, 1): burst, (1, 2): older})
    last = {"bvid": "BVold", "ts": 100, "fetched_at": time.time() - 600}
    new, newest = auto_refresh.fetch_new(1, last)
    assert [(pn) for _, _, pn in calls] == [1, 2], "第1页整页新 → 应翻第2页"
    assert [v["bvid"] for v in new] == [f"BVb{i}" for i in range(5)], "已见的过滤掉"
    assert newest["bvid"] == "BVb0"

    # 已有 deep=4 的 continuation 遇到三页新投稿：首页漂移当轮读取 1,2,4，
    # 下一轮首页稳定也必须继续修复第 3 页，而不是直接跳到 1,5,6。
    ps = auto_refresh.RECENT_PS

    def full_page(prefix, start_ts):
        return [_vid(f"{prefix}{i}", start_ts - i) for i in range(ps)]

    moving_pages = {
        (2, 1): full_page("BVnew-head-", 1000),
        (2, 2): full_page("BVnew-page2-", 900),
        (2, 3): full_page("BVlow-gap-", 800),
        (2, 4): full_page("BVdeep-page4-", 700),
        (2, 5): [_vid("BVold", 100)],
    }
    moving_calls = _patch_fetch_calls(monkeypatch, moving_pages)
    continuation_last = {
        "bvid": "BVold",
        "ts": 100,
        "fetched_at": time.time() - 600,
        "scan": {
            "base_bvid": "BVold",
            "base_ts": 100,
            "ps": ps,
            "next_pn": 4,
            "deep_next_pn": 4,
            "head_next_pn": 4,
            "head_repair_until_pn": 4,
            "terminal_pn": None,
            "head_bvid": "BVbefore-head",
            "head_ts": 600,
            "items": full_page("BVbefore-", 600),
        },
    }

    first_new, first_newest = auto_refresh.fetch_new(2, continuation_last)
    assert [pn for _, _, pn in moving_calls] == [1, 2, 4]
    assert first_newest["_scan_complete"] is False, \
        "低页接缝未闭合时不得把 deep 结果当成全局完成"
    first_state = first_newest["_scan_state"]
    assert first_state["head_next_pn"] == 3
    assert first_state["head_repair_until_pn"] == 4
    assert first_state["deep_next_pn"] == 5
    assert first_state["terminal_pn"] is None
    assert continuation_last["bvid"] == "BVold", "未完成轮不得推进已提交游标"
    assert first_new

    moving_calls.clear()
    continuation_last["scan"] = first_state
    second_new, second_newest = auto_refresh.fetch_new(2, continuation_last)
    assert [pn for _, _, pn in moving_calls] == [1, 3, 5]
    assert any(v["bvid"] == "BVlow-gap-0" for v in second_new), \
        "跨轮修复前沿必须收集曾被 offset 漂移跳过的第 3 页"
    assert second_newest["_scan_complete"] is True
    assert "_scan_state" not in second_newest

    # deep 已先找到终止页时，终止证据必须跨轮保留；后续预算只连续补 head，
    # 直到冻结接缝闭合后才完成，且不得继续请求 terminal 之后的页。
    terminal_pages = {
        (3, 1): full_page("BVterminal-head-", 1200),
        (3, 2): full_page("BVterminal-page2-", 1100),
        (3, 3): full_page("BVterminal-page3-", 1000),
        (3, 4): full_page("BVterminal-page4-", 900),
        (3, 5): full_page("BVterminal-page5-", 800),
        (3, 6): [_vid("BVold", 100)],
    }
    terminal_calls = _patch_fetch_calls(monkeypatch, terminal_pages)
    terminal_last = {
        "bvid": "BVold",
        "ts": 100,
        "fetched_at": time.time() - 600,
        "scan": {
            "base_bvid": "BVold",
            "base_ts": 100,
            "ps": ps,
            "next_pn": 6,
            "deep_next_pn": 6,
            "head_next_pn": 6,
            "head_repair_until_pn": 6,
            "terminal_pn": None,
            "head_bvid": "BVterminal-before-head",
            "head_ts": 600,
            "items": full_page("BVterminal-before-", 600),
        },
    }

    terminal_first_new, terminal_first_newest = auto_refresh.fetch_new(3, terminal_last)
    assert [pn for _, _, pn in terminal_calls] == [1, 2, 6]
    assert terminal_first_new
    assert terminal_first_newest["_scan_complete"] is False
    terminal_first_state = terminal_first_newest["_scan_state"]
    assert terminal_first_state["head_next_pn"] == 3
    assert terminal_first_state["head_repair_until_pn"] == 6
    assert terminal_first_state["deep_next_pn"] == 7
    assert terminal_first_state["terminal_pn"] == 6, \
        "deep 的终止证据必须先持久化，不能因低页接缝未闭合而丢失"
    assert terminal_last["bvid"] == "BVold"

    terminal_calls.clear()
    terminal_last["scan"] = terminal_first_state
    terminal_second_new, terminal_second_newest = auto_refresh.fetch_new(3, terminal_last)
    assert [pn for _, _, pn in terminal_calls] == [1, 3, 4], \
        "已有 terminal 时只能连续修复 head，不得再推进 deep"
    assert terminal_second_newest["_scan_complete"] is False
    terminal_second_state = terminal_second_newest["_scan_state"]
    assert terminal_second_state["head_next_pn"] == 5
    assert terminal_second_state["head_repair_until_pn"] == 6
    assert terminal_second_state["deep_next_pn"] == 7
    assert terminal_second_state["terminal_pn"] == 6
    assert any(v["bvid"] == "BVterminal-page3-0" for v in terminal_second_new)
    assert any(v["bvid"] == "BVterminal-page4-0" for v in terminal_second_new)

    terminal_calls.clear()
    terminal_last["scan"] = terminal_second_state
    terminal_third_new, terminal_third_newest = auto_refresh.fetch_new(3, terminal_last)
    assert [pn for _, _, pn in terminal_calls] == [1, 5]
    assert any(v["bvid"] == "BVterminal-page5-0" for v in terminal_third_new)
    assert terminal_third_newest["_scan_complete"] is True
    assert "_scan_state" not in terminal_third_newest
    assert all(pn <= 6 for _, _, pn in terminal_calls), \
        "持久化 terminal 已足够证明 deep 终止，不得请求第 7 页"


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


# ---------- 名称回填 ----------

def test_refresh_once_backfills_missing_uname(monkeypatch):
    """存量 UP 没有 uname:检查拉到视频后自动回填名称。"""
    ups = [{"mid": 1, "uname": "", "face": ""}]
    renamed = []
    batches, marked, _ = _patch_env(
        monkeypatch, ups, {1: [_vid("BVn", 100)]})
    monkeypatch.setattr(auto_refresh.up_list, "set_uname",
                        lambda mid, uname: renamed.append((int(mid), uname)) or True)
    auto_refresh.refresh_once()
    assert renamed == [(1, "UP")], "uname 为空时用视频作者名回填"


def test_refresh_once_no_uname_backfill_when_named(monkeypatch):
    ups = [{"mid": 1, "uname": "已有名", "face": ""}]
    renamed = []
    batches, marked, _ = _patch_env(
        monkeypatch, ups, {1: [_vid("BVn", 100)]})
    monkeypatch.setattr(auto_refresh.up_list, "set_uname",
                        lambda mid, uname: renamed.append((int(mid), uname)) or True)
    auto_refresh.refresh_once()
    assert renamed == []


# ---------- 并发控制:进行中再触发必须跳过而不是排队(缺陷 B4) ----------
# refresh_once 用阻塞的 `with _lock:` —— 锁保证了「不并发」,却没保证「不排队」:
# 后来者卡在锁上,前一轮释放后完整再跑一轮(每个 UP 一次 arc/search、UP 之间
# sleep 5 秒)。连点 N 次「立即检查新视频」= 串行跑 N 轮;30 个 UP 的清单点 5 次
# 就是 150 次接口调用、约 12 分钟连续请求 —— 正是 docs/COMPLIANCE.md 承诺要避免的
# 账号级风控场景。需求 2.9 要的是「跳过」,不是「排队后补跑」。
#
# 这一批用例都必须自己把锁收干净:auto_refresh._lock 是模块级全局,任何一条忘了
# release,后面每一条碰锁的用例都会死等 —— 整包挂住,而不是一条变红。

def _count_round_calls(monkeypatch):
    """给「会打 B 站接口」的两个入口装计数桩,并把一轮检查彻底断网。

    返回 calls 字典。清单里放了 1 个 UP,所以 refresh_once 真跑起来时
    list_ups 与 fetch 必然各 +1 —— 「两个计数都为 0」就是「本次未发起任何
    B 站请求」的证据(2.9),而不是「反正也没什么可请求的」。
    """
    calls = {"list_ups": 0, "fetch": 0}

    def fake_list_ups():
        calls["list_ups"] += 1
        return [{"mid": 1, "uname": "UP"}]

    def fake_fetch(mid, ps=10, pn=1):
        calls["fetch"] += 1
        return []          # 空页 = 限流形态:不入队、不推进 last_seen,一轮秒结束

    monkeypatch.setattr(auto_refresh.up_list, "list_ups", fake_list_ups)
    monkeypatch.setattr(auto_refresh.up_list, "get_last_seen_all", lambda: {})
    monkeypatch.setattr(auto_refresh.bapi, "fetch_up_videos", fake_fetch)
    monkeypatch.setattr(auto_refresh.time, "sleep", lambda s: None)
    return calls


def test_refresh_once_skips_when_already_running(monkeypatch):
    """一轮正在跑时再调 refresh_once():必须立即返回 0,且零 B 站请求。

    Property 4: Bug Condition —— 后来者跳过而非排队。
    对应 isBugCondition_B4:已有一轮 refresh 正在运行。

    为什么后来者必须跑在子线程里:未修复代码是阻塞的 `with _lock:`,主线程持锁时
    直接在主线程调 refresh_once() 就是自我死锁 —— 用例会永久挂住而不是失败,
    整个测试套件跟着卡死。放到子线程后,`join(timeout)` 把「排队」翻译成可断言的事实:

      未修复:线程卡在 `_lock.acquire()` 上 → join 超时 → 仍 is_alive() → FAIL。
              反例就是「后来者在排队等锁」;主线程一释放,它会完整再跑一轮
              并真的发起 B 站请求。
      修复后:`_lock.acquire(blocking=False)` 拿不到 → 记一行日志 → return 0,秒退。

    Validates: Requirements 2.9
    """
    calls = _count_round_calls(monkeypatch)
    result = []
    lock = auto_refresh._lock
    assert lock.acquire(timeout=5), "取不到 auto_refresh._lock(疑有用例泄漏了锁)"
    later = threading.Thread(target=lambda: result.append(auto_refresh.refresh_once()),
                             daemon=True, name="bd-test-latecomer")
    try:
        later.start()
        later.join(timeout=2)
        assert not later.is_alive(), (
            "后来者应立即跳过,而不是排队阻塞在 _lock 上 —— 未修复的 `with _lock:` "
            "会让它等前一轮跑完再完整跑一遍(每个 UP 一次 arc/search、间隔 5 秒)")
        assert result == [0], f"跳过时应返回 0(本轮入队 0 条),实际 {result}"
        assert calls == {"list_ups": 0, "fetch": 0}, (
            f"跳过的这一次不得发起任何 B 站请求,实际 {calls}")
    finally:
        # 先释放、再 join:顺序反了就是死等(未修复时它正卡在这把锁上)。
        # 桩还在,所以它补跑的那一轮全程离线且瞬间结束,不会打真实接口。
        lock.release()
        later.join(timeout=5)


def test_is_running_reflects_lock(monkeypatch):
    """is_running() 必须如实反映「有没有一轮在跑」:未持锁 False、持锁 True、释放后 False。

    Property 4: Bug Condition —— 后来者跳过而非排队(的可观测面)。

    路由要如实回报 started(2.10),就得有一个能问「现在在跑吗」的谓词。未修复代码
    只有一把私有 `_lock`、没有任何对外谓词 —— 于是 /api/up-list/check 无条件返回
    {"ok": True},前端只能靠固定 30 秒的假禁用窗口猜。

    守卫写在测试体内,而不是模块顶层 `from auto_refresh import is_running`:
    属性还不存在时那样写会让整个文件收集失败,把「一条 FAIL」放大成「整包 error」。

    Validates: Requirements 2.10
    """
    assert hasattr(auto_refresh, "is_running"), (
        "auto_refresh 缺少 is_running() 谓词(缺陷 B4 现场):"
        "路由无从判断「已有一轮在跑」,只能无条件回报成功")
    lock = auto_refresh._lock
    assert auto_refresh.is_running() is False, "没人在跑时应为 False"
    assert lock.acquire(timeout=5), "取不到 auto_refresh._lock(疑有用例泄漏了锁)"
    try:
        assert auto_refresh.is_running() is True, "一轮在跑(锁被持有)时应为 True"
    finally:
        lock.release()
    assert auto_refresh.is_running() is False, "释放后应回到 False"

    # 包装器在调用零参数模块符号时必须已经持有预约，且忙时不污染上一轮状态。
    with auto_refresh._status_lock:
        saved_status = dict(auto_refresh._status)
    calls = []

    def fake_refresh():
        assert auto_refresh.is_running() is True, \
            "run_refresh_with_status 调 refresh_once 前必须已原子预约"
        calls.append(1)
        return 3

    monkeypatch.setattr(auto_refresh, "refresh_once", fake_refresh)
    try:
        assert auto_refresh.run_refresh_with_status("manual") == 3
        assert calls == [1]
        assert auto_refresh.is_running() is False, "终态提交后必须释放预约"

        assert lock.acquire(timeout=5)
        try:
            with auto_refresh._status_lock:
                before_busy = dict(auto_refresh._status)
            assert auto_refresh.run_refresh_with_status("scheduled") == 0
            with auto_refresh._status_lock:
                after_busy = dict(auto_refresh._status)
            assert after_busy == before_busy, \
                "忙时包装器不得改写 last_trigger/last_started/last_error 等上一轮状态"
            assert calls == [1], "忙时不得调用 monkeypatch 的 refresh_once"
        finally:
            lock.release()
    finally:
        with auto_refresh._status_lock:
            auto_refresh._status.clear()
            auto_refresh._status.update(saved_status)


def test_refresh_once_releases_lock_on_exception(monkeypatch):
    """一轮检查抛异常也必须释放锁,否则一次异常永久卡死后续所有检查。

    Property 4: Bug Condition —— 后来者跳过而非排队。

    任务 12.1 把 `with _lock:` 换成 `acquire(blocking=False)` + `try/finally:
    release()` 时,最容易漏的就是那个 finally:`with` 语句自带异常安全,手写
    acquire 不带。漏了的后果比缺陷本身更糟 —— 锁永远握在一个已经返回的调用手里,
    is_running() 恒为 True,之后每一次检查(手动点击 + 后台 30 分钟轮询)都被判成
    「已有一轮在跑」而跳过,自动下载彻底停摆,且不报任何错。

    这里让 `up_list.list_ups` 抛异常(它在 refresh_once 里位于锁内、for 循环的
    try/except 之外,所以会一路向上传播),再检查锁已放开。

    Validates: Requirements 2.9
    """
    assert hasattr(auto_refresh, "is_running"), (
        "auto_refresh 缺少 is_running() 谓词(缺陷 B4 现场)")

    def boom():
        raise RuntimeError("拉 UP 清单炸了")

    monkeypatch.setattr(auto_refresh.up_list, "list_ups", boom)
    monkeypatch.setattr(auto_refresh.bapi, "fetch_up_videos",
                        lambda *a, **k: pytest.fail("清单都没拉到,不该发 B 站请求"))
    try:
        with pytest.raises(RuntimeError):
            auto_refresh.refresh_once()
        assert auto_refresh.is_running() is False, (
            "异常传播后锁必须已释放(finally: _lock.release());否则 is_running() 恒 True,"
            "后续所有检查都被误判成「已有一轮在跑」而跳过,自动下载彻底停摆")
    finally:
        # 防污染:万一 finally: release() 真漏了,这里把锁收干净 ——
        # 让本条单独变红,而不是拖着后面每条碰锁的用例一起死等。
        if auto_refresh._lock.locked():
            auto_refresh._lock.release()


# ---------- 单轮/轮询的执行语义:B4 只换锁的获取方式,这些必须逐位不变 ----------
# Property 10: Preservation —— 单轮检查的执行语义。
# 任务 12.1 只把 refresh_once 的 `with _lock:` 换成 `acquire(blocking=False)` +
# try/finally,**函数体一字不改**,start_loop 完全不动。所以下面这些行为在修复
# 前后都必须成立,它们是「改锁没顺手改坏别的」的基线。
#
# 上面已有的用例覆盖了其中三条,不重复造:
#   3.17 UP 之间 ≥5 秒间隔     → test_serial_interval_between_ups
#                               + test_compliance_doc_matches_rate_limits
#   3.18 拉取返回空不推进标记   → test_rate_limited_up_skipped_and_not_marked
#                               + test_fetch_new_rate_limited
#   返回值 = 入队条数          → test_all_listed_ups_downloaded / _skips_seen_and_downloaded
# 下面补的是原先没有任何用例碰过的两条:3.20(单 UP 异常不中断整轮)与
# 3.19(start_loop 的轮询间隔与线程属性)。


def test_refresh_once_continues_after_single_up_error(monkeypatch):
    """一轮里某个 UP 抛异常:只记日志,后续 UP 照常处理,整轮不中断。

    Property 10: Preservation —— 单轮检查的执行语义。

    refresh_once 的 for 循环体内包着 `try/except Exception: log.exception(...)`,
    这个兜底是自动下载能长期无人值守的前提:一个 UP 被风控/改私密/返回脏数据,
    不该让同一轮里排在它后面的 UP 全部失去这轮机会(下一轮要等 30 分钟)。

    原先没有任何用例验过「第一个 UP 炸了、第二个 UP 仍被处理」——
    `test_refresh_once_releases_lock_on_exception` 验的是 `list_ups` 抛异常
    (位置在 for 循环之外,语义相反:那条要求异常向上传播)。

    「记录日志」用桩记 log.exception 的调用而不是 caplog:桩不依赖 root logger
    的等级/handler 配置(basicConfig 只在 import app 时跑过),断言更硬。

    Validates: Requirements 3.20
    """
    ups = [{"mid": 1, "uname": "炸的"}, {"mid": 2, "uname": "好的"}]
    batches, marked, sleeps = _patch_env(monkeypatch, ups, {})
    logged = []

    def boom_for_first(mid, ps=10, pn=1):
        if int(mid) == 1:
            raise RuntimeError("UP 1 拉取炸了(限流/私密/脏数据)")
        return [_vid("BV2", 200)]

    monkeypatch.setattr(auto_refresh.bapi, "fetch_up_videos", boom_for_first)
    monkeypatch.setattr(auto_refresh.log, "exception",
                        lambda msg, *a, **k: logged.append(msg % a if a else msg))

    n = auto_refresh.refresh_once()

    assert logged, "单 UP 异常必须落一条日志(否则问题静默消失,无人值守时无从排查)"
    assert n == 1, f"整轮不该被第一个 UP 的异常带走,UP 2 的 1 条仍应入队,实际 {n}"
    assert batches == [(["BV2"], "", 1)], \
        f"异常之后的 UP 必须照常入队,实际 {batches}"
    assert [m[:3] for m in marked] == [(2, "BV2", 200)], \
        "炸掉的 UP 不推进 last_seen(留给下一轮重试),正常的照常推进"
    assert sleeps == [auto_refresh.UP_INTERVAL], \
        "异常也不豁免防风控间隔:UP 1 处理完(炸了)之后仍要等满间隔再动 UP 2"


# ---------- 后台轮询线程(3.19):start_loop 原先零覆盖 ----------

class _StopLoop(BaseException):
    """掐断 start_loop 的 `while True` 用的哨兵异常。

    刻意继承 BaseException 而不是 Exception。当前实现里 `time.sleep(...)` 在
    `try:` 之外,抛什么都能逃出 loop() 把线程结束掉;但只要哪天有人把 sleep 挪进
    那个 try 块,`except Exception:` 就会把哨兵吞掉 —— 循环立刻退化成
    「sleep 抛 → 吞掉 → 再 sleep 抛」的无 IO 死循环,这条测试会从「变红」升级成
    「整包挂死」。BaseException 绕过 `except Exception`,任何重构下线程都必然结束。
    """


def _run_poll_loop(monkeypatch, stop_after_sleeps: int):
    """真跑一遍 start_loop 的守护循环,在第 N 次 sleep 时掐断。

    返回 (thread, sleeps, refresh_calls)。绝不真的睡:sleep 被换成记录桩,
    所以整个用例是毫秒级的,不存在「等 30 分钟」或挂死的可能。
    """
    sleeps, refresh_calls = [], []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= stop_after_sleeps:
            raise _StopLoop

    monkeypatch.setattr(auto_refresh.time, "sleep", fake_sleep)
    monkeypatch.setattr(auto_refresh, "refresh_once",
                        lambda: refresh_calls.append(1) or 0)
    # 哨兵逃出线程时 threading 会调 excepthook(pytest 装的那个会转成警告);
    # 换成空实现,输出干净。monkeypatch 在用例结束时还原,而线程已 join 完毕。
    monkeypatch.setattr(threading, "excepthook", lambda args: None)

    th = auto_refresh.start_loop()
    th.join(timeout=5)
    assert not th.is_alive(), \
        "轮询线程没能在 5 秒内结束 —— 掐断机制失效,后续用例有挂死风险"
    return th, sleeps, refresh_calls


def test_start_loop_polls_periodically_in_daemon_thread(monkeypatch):
    """后台轮询:守护线程里「睡够间隔 → 跑一轮 → 再睡」地周期触发。

    Property 10: Preservation —— 单轮检查的执行语义(轮询侧)。

    start_loop 原先零测试覆盖。它是自动下载的唯一驱动源,任务 12.1 又明确
    「两个调用方都不需要改」(跳过时 refresh_once 仍返回 int 0,`if n:` 不打日志),
    这条就把那个前提钉住:轮询照旧调 refresh_once、照旧不因返回 0 而中断循环。

    停在第 2 次 sleep 而不是第 1 次:第 2 次 sleep 发生只可能是「循环转回了
    while 顶部」,这才是「周期性」的证据 —— 只验一次 sleep 无法区分
    「循环」和「睡一次就退出」。

    daemon=True 也一并锁住:非守护线程会让 Ctrl+C 之后进程挂着不退。

    Validates: Requirements 3.19
    """
    th, sleeps, refresh_calls = _run_poll_loop(monkeypatch, stop_after_sleeps=2)

    assert th.daemon is True, "轮询线程必须是守护线程,否则进程退不掉"
    assert th.name == "bd-auto-refresh", "线程名是排查现场的抓手,不要改"
    assert len(sleeps) == 2, \
        f"应「睡→跑→睡」地循环,第 2 次 sleep 证明转回了 while 顶部,实际 {sleeps}"
    assert refresh_calls == [1], \
        f"每个间隔恰好触发一轮 refresh_once,实际调用 {len(refresh_calls)} 次"
    assert sleeps[0] == max(60, auto_refresh.config.AUTO_REFRESH_MINUTES * 60), \
        "间隔取 max(60, config.AUTO_REFRESH_MINUTES * 60),运行时读配置(实时可调)"
    assert auto_refresh.config.AUTO_REFRESH_MIN >= 10, \
        "轮询间隔的配置下限不得放松(docs/COMPLIANCE.md 的对外承诺)"


@pytest.mark.parametrize("minutes,expected_sec", [
    (30, 1800),   # 默认值
    (10, 600),    # config.AUTO_REFRESH_MIN,用户能配到的最小值
    (360, 21600),  # config.AUTO_REFRESH_MAX
    (1, 60),      # 越过 UI 校验写进 config.json 也被 max(60, ...) 兜住
    (0, 60),      # 同上,0 不会退化成「不睡的忙循环」
])
def test_start_loop_interval_follows_config_with_floor(monkeypatch, minutes,
                                                       expected_sec):
    """轮询间隔 = max(60 秒, 配置分钟数);配置被写坏也不会退化成忙循环。

    Property 10: Preservation —— 单轮检查的执行语义(轮询侧)。

    间隔在**循环体内**读 config(不是启动时取快照),所以设置页改完立刻生效;
    那个 `max(60, ...)` 是最后一道防线:config.json 被手改成 0 / 负数时,
    没有它就是不带间隔地连续打 B 站接口 —— 正是 B4 要收紧的风控场景。

    Validates: Requirements 3.19
    """
    monkeypatch.setattr(auto_refresh.config, "AUTO_REFRESH_MINUTES", minutes)
    _, sleeps, _ = _run_poll_loop(monkeypatch, stop_after_sleeps=1)
    assert sleeps == [expected_sec], \
        f"AUTO_REFRESH_MINUTES={minutes} 应睡 {expected_sec} 秒,实际 {sleeps}"


# ---------- 自动下载路径上的真实「已下载」判定(覆盖盲区) ----------
# Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)(自动下载侧)。
#
# 上面 6 个用例全部经 _patch_env,而 _patch_env 默认把 downloader.is_downloaded
# 打成恒 False 的桩(唯一例外 test_skips_seen_and_downloaded 也只是换成
# `lambda url: "BVdl" in url`,仍是桩)。于是自动下载这条路径**从不执行真实判定**。
# B3 刚把已下载判定从「只看 BV」改成 (bvid, page) 粒度,而自动下载正是需求 2.8
# 的主场景 —— 那个改动在这条路径上原先零覆盖。
#
# 这一节改用 stub_is_downloaded=False:不打桩,直接往真实任务表塞任务,让
# refresh_once 调 taskstore.is_downloaded 的真身,端到端地验「跳过/入队」的选择。
#
# 为什么「打桩点是 downloader.is_downloaded、塞数据点是 taskstore._tasks」能对上:
# `def is_downloaded` 只定义在 taskstore.py,downloader.py 顶部的
# `from taskstore import (... is_downloaded ...)` 只是把同一个函数对象再导出一个
# 名字 —— 它的全局作用域仍是 taskstore 模块,读的仍是 taskstore._tasks 那个 dict。
# _seed_task 里有一条 `is` 断言把这个前提钉死:哪天有人在 downloader.py 里另写一份
# 实现(判定就此分叉成两套),这一节会立刻以「不是同一个函数对象」失败,而不是
# 悄悄退化成「测了一个没人调用的函数」。
#
# 安全性:tests/conftest.py 的 autouse fixture _isolate_taskstore 把
# taskstore.TASKS_FILE 指向 tmp_path,并在每个用例前清空 _tasks、结束后原样还回;
# 且本节的 start_batch / mark_seen 仍是桩,不触发任何 _persist()。

_BV1 = "BV1xx411c7mD"          # 塞进任务表的那个 BV
_BV2 = "BV2yy411c7mD"          # 任务表里没有的 BV,做正向对照


def _seed_task(tid, bvid, page, status="done"):
    """往**真实**任务表塞一条任务,供未打桩的 is_downloaded 读取。

    字段只取判定真正会读的三项(status / url / page),其余沿用
    tests/test_downloader.py `_mk_task` 的结构以便对照。注意 url 刻意**不带
    `?p=`**:任务表存的是用户粘贴的原始链接,分 P 的权威值在 `page` 字段 ——
    这正是 is_downloaded 两侧取 page 方式不对称的原因。
    """
    assert downloader.is_downloaded is taskstore.is_downloaded, (
        "downloader.is_downloaded 应是 taskstore.is_downloaded 的再导出(同一函数对象)。"
        "不成立说明判定已分叉成两份实现:塞 taskstore._tasks 不再被自动下载路径看见,"
        "本节测的就不是真实调用的那一份了")
    taskstore._tasks[tid] = {
        "task_id": tid, "status": status, "progress": 100 if status == "done" else 0,
        "speed": 0, "eta": 0, "title": f"视频{bvid}",
        "url": f"https://www.bilibili.com/video/{bvid}",
        "label": "", "page": page, "error": "", "cancel": False,
    }


def test_auto_download_skips_same_page_end_to_end(monkeypatch):
    """自动下载执行**真实** is_downloaded:同 (BV, 分P) 已完成 → 跳过,其余照常入队。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)(自动下载侧)。

    锁的不变量:自动下载的跳过决策来自真实的 taskstore.is_downloaded,而不是
    「测试里那个恒 False 的桩」。这是自动下载路径上首次真实执行该判定 ——
    原先 6 个用例全打桩,`auto_refresh.py` 里
    `to_dl = [v for v in new if not downloader.is_downloaded(v["url"])]`
    这一行在测试中从未与真实任务表打过交道。

    BV2 的入队是必要的正向对照:只断言「BV1 被跳过」的话,一个恒 True 的坏判定
    (或者干脆不调 is_downloaded 而清空 to_dl)也能让断言通过。

    Validates: Requirements 2.6, 2.7
    """
    _seed_task("auto_done_p1", _BV1, page=1)          # 真实任务表:(BV1, P1) 已完成
    ups = [{"mid": 1, "uname": "UP名"}]
    batches, marked, _ = _patch_env(
        monkeypatch, ups,
        {1: [_vid(_BV1, 300), _vid(_BV2, 200)]},
        stub_is_downloaded=False)

    n = auto_refresh.refresh_once()

    assert n == 1, f"BV1 已下载应被跳过、BV2 应入队 → 恰好 1 条,实际 {n}"
    assert batches == [([_BV2], "", 1)], (
        f"只有 BV2 入队;BV1 由真实 is_downloaded 判为已下载,实际 {batches}")
    assert marked[0][:3] == (1, _BV1, 300), \
        "跳过下载不影响 last_seen 推进(最新一条仍是 BV1)"


def test_auto_download_enqueues_when_done_page_differs(monkeypatch):
    """已完成任务是该 BV 的 **P3**、自动检查解析出 P1 → 键不同 → 必须入队。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)(自动下载侧)。

    这条就是 B3 让需求 2.8 得以(部分)成立的那一半,也是本节唯一能在自动下载
    路径上区分「修复形态」与「缺陷形态」的用例:

      缺陷形态(只比 bvid):BV1 有 done 任务 → 判已下载 → **跳过**,用户永远拿不到
                            这个 BV 的新内容(他只是曾经手动下过其中的 P3)。
      修复形态((bvid, page)):`bilibili_api.py:55` 造的条目 url 恒不带 `?p=`
                            → page_from_url = 1 → 查询键 `BV1#p1`;
                            任务键 `BV1#p3`(读 t["page"],不解析 t["url"])
                            → 键不等 → 判未下载 → **入队**。

    变异检验(把 taskstore.is_downloaded 退回只比 BV 的形态)时,这条必然翻红。

    Validates: Requirements 2.8
    """
    _seed_task("auto_done_p3", _BV1, page=3)          # 用户曾用分 P 选择器下过 P3
    ups = [{"mid": 1, "uname": "UP名"}]
    batches, marked, _ = _patch_env(
        monkeypatch, ups, {1: [_vid(_BV1, 300)]},
        stub_is_downloaded=False)

    n = auto_refresh.refresh_once()

    # 端到端断言放在最前:变异检验时要的反例是「自动下载**跳过了本该入队的视频**」,
    # 而不是「is_downloaded 单独问答不对」(后者 tests/test_downloader.py 已覆盖)。
    assert n == 1, f"P3 已下载不代表这条(解析为 P1)已下载,应入队,实际 {n}"
    assert batches == [([_BV1], "", 1)], (
        f"BV1 必须入队;缺陷形态下会被判已下载而 batches == [],实际 {batches}")
    assert marked[0][:3] == (1, _BV1, 300)

    # 追加诊断:把上面那个结论的**理由**钉在键规则上。端到端断言告诉你「坏了」,
    # 这一条告诉你「坏在判定键丢了 page 维度」,而不是别处(比如 fetch_new 过滤)。
    url = f"https://www.bilibili.com/video/{_BV1}"
    assert "?p=" not in url and "&p=" not in url, "arc/search 条目 url 恒不带分 P"
    assert downloader.is_downloaded(url) is False, (
        "查询键 BV1#p1 ≠ 任务键 BV1#p3 → 未下载;"
        "这里返回 True 说明判定退回了「只比 BV」的缺陷形态")


def test_auto_download_ignores_non_done_tasks(monkeypatch):
    """任务表里只有非 `done` 状态(failed / interrupted)→ 仍算未下载,照常入队。

    Property 9: Preservation —— 非 done 状态不计入「已下载」(自动下载侧)。

    3.15 在自动下载路径上原先零覆盖。它是「失败/中断能被下一轮自动重试」的
    前提:上一轮下崩了(failed)、或 app 重启把进行中任务改成 interrupted
    (`_load_persisted` 的正常语义),这些视频必须还能被下一轮重新入队 ——
    若判成已下载,一次偶发失败就等于永久放弃该视频,且不报任何错。

    一条 failed + 一条 interrupted 覆盖两类真实来源,而不是只挑一个状态。

    Validates: Requirements 3.15
    """
    _seed_task("auto_failed", _BV1, page=1, status="failed")
    _seed_task("auto_interrupted", _BV2, page=1, status="interrupted")
    ups = [{"mid": 1, "uname": "UP名"}]
    batches, _, _ = _patch_env(
        monkeypatch, ups,
        {1: [_vid(_BV1, 300), _vid(_BV2, 200)]},
        stub_is_downloaded=False)

    n = auto_refresh.refresh_once()

    assert n == 2, f"failed / interrupted 都不算已下载,两条都该入队,实际 {n}"
    assert batches == [([_BV1, _BV2], "", 1)], (
        f"非 done 任务不得让自动下载跳过对应视频,实际 {batches}")


def test_auto_download_known_gap_new_page_of_same_bv(monkeypatch):
    """【已知缺口 · 表征测试】同 BV 新增分 P 时自动检查察觉不到,会跳过。

    Property 3 的**残余缺口**,不是 Property 3 的实现。

    ⚠️ 这条断言的**不是期望行为**,而是 2026-08 全站修复轮的既定决策:
    「明确不实现」清单第 1 条,「B3 → 7. 需求 2.8 的可达性」。需求 2.8
    因此只是**部分成立**:
      - 已完成任务是该 BV 的**非 1 分 P** → 键不等 → 入队,2.8 成立
        (即上一条 test_auto_download_enqueues_when_done_page_differs);
      - 已完成任务是该 BV 的 **P1**、而「新分 P」是同 BV 下新增的 P2 → 2.8 不成立,
        就是本条。

    缺口的根因**不在判定键,而在数据源**:`arc/search` 只报 BV、不带任何分 P 信息,
    自动检查手上永远只有一个不带 `?p=` 的 url,解析出的 page 恒为 1。所以下面这两件
    事同时为真,构成缺口的精确刻画:
      - 直接拿 `?p=2` 去问,判定**答得出**「P2 未下载」(判定键没错);
      - 自动下载路径**问不出**这个问题(它构造不出 `?p=2` 的 url)→ 跳过。

    要覆盖它,必须给每个新 BV 额外调一次 `x/player/pagelist` 枚举分 P,等于给自动
    检查增加「1 次接口调用 / 视频」—— 直接冲撞 B4 刚收紧的风控边界与
    `docs/COMPLIANCE.md` 的对外承诺,属于扩范围,列入待决策项。

    最刺痛的一点由最后一条断言锁住:这一轮仍被算作「处理成功」,last_seen 照常
    推进到 BV1 —— 缺口不是「这轮漏了、下轮会补」,而是**永久错过**:下一轮
    fetch_new 会因 ts 不大于 last_seen 而把 BV1 整条过滤掉,连再判一次的机会都没有。

    本条的作用是把缺口变成可执行的知识。将来真去补齐(加 pagelist 枚举)时,它会
    以「预期失败」的形式拦住改动者,提醒同步更新这里与上述 spec 记录。

    Validates: Requirements 2.8(记录不成立的那一半)
    """
    _seed_task("auto_done_p1", _BV1, page=1)          # 已下过 P1
    ups = [{"mid": 1, "uname": "UP名"}]
    # 现实场景:UP 给 BV1 加了 P2,BV1 因此重新出现在 arc/search 顶部(view_at 变新)
    batches, marked, _ = _patch_env(
        monkeypatch, ups, {1: [_vid(_BV1, 999)]},
        stub_is_downloaded=False)

    # 缺口的前半句:判定键本身是对的 —— 显式问 P2,它答得出「未下载」。
    # 这一条必须在 refresh_once 之前:它描述的是「自动下载**本应**能问出什么」,
    # 是后面「却问不出」的对照面。变异检验下它也会红,理由见其失败信息。
    url = f"https://www.bilibili.com/video/{_BV1}"
    assert downloader.is_downloaded(url + "?p=2") is False, (
        "判定键本身是对的:显式问 P2 能答出「未下载」。"
        "这里返回 True 说明判定退回了「只比 BV」的缺陷形态,缺口的性质就变了 ——"
        "从「数据源缺分 P 信息」变成「判定键丢了 page 维度」")

    n = auto_refresh.refresh_once()

    # 缺口的后半句:自动下载路径构造不出 `?p=2` 的 url,于是问不出那个问题。
    assert n == 0, (
        f"【已知缺口】arc/search 不带分 P 信息 → 解析为 P1 → 命中已完成的 P1 → 跳过,"
        f"新增的 P2 抓不到。实际 {n};若变成 1,说明有人加了分 P 枚举(见 docstring)")
    assert batches == [], "【已知缺口】不入队,P2 就此漏下"
    assert marked[0][:3] == (1, _BV1, 999), (
        "【已知缺口】这一轮仍算处理成功,last_seen 推进到 BV1 —— 下一轮 BV1 会被"
        "「已见」过滤掉,P2 是永久错过而非延后重试")


# ---------- 合集 / 收藏夹订阅(自动下载新入库视频) ----------
# 这条链路 2026-10-31 新增,refresh_once 里排在 UP 之后串行处理。
# 关键不变量:基线只建一次、只下新增、失败不漏稿、条目超限明确失败。

def _coll(kind="season", cid=6365894, seen=None, quality=""):
    return {"kind": kind, "id": cid, "title": "某合集", "quality": quality,
            "seen_bvids": list(seen or [])}


def _patch_collection_env(monkeypatch, collections, fetch_items,
                          seen_writes=None, downloaded=None, tasks=None):
    """合集订阅专用打桩:清单/合集内容/已见提交/已下载判定/任务史/sleep。"""
    batches, sleeps = [], []
    seen_writes = seen_writes if seen_writes is not None else []

    def fake_start_batch(items, label, max_concurrent=None):
        batches.append(([v["bvid"] for v in items], label, max_concurrent))
        return ["t"] * len(items)

    monkeypatch.setattr(auto_refresh.up_list, "list_ups", lambda: [])
    monkeypatch.setattr(auto_refresh.up_list, "list_collections",
                        lambda: list(collections))
    monkeypatch.setattr(auto_refresh.bapi, "fetch_season_content",
                        lambda cid, **kw: fetch_items(cid))
    monkeypatch.setattr(auto_refresh.bapi, "fetch_fav_content",
                        lambda cid, **kw: fetch_items(cid))
    monkeypatch.setattr(auto_refresh.up_list, "commit_collection_seen",
                        lambda kind, cid, bvids:
                            seen_writes.append((kind, cid, list(bvids))))
    monkeypatch.setattr(auto_refresh.downloader, "start_batch", fake_start_batch)
    monkeypatch.setattr(auto_refresh.downloader, "is_downloaded",
                        lambda url: bool((downloaded or {}).get(url)))
    monkeypatch.setattr(auto_refresh.downloader, "list_tasks",
                        lambda: list(tasks or []))
    monkeypatch.setattr(auto_refresh.time, "sleep", lambda s: sleeps.append(s))
    return batches, seen_writes, sleeps


def test_collection_first_round_builds_baseline_without_downloading(monkeypatch):
    """首次订阅:以当前全部条目为基线,一条都不下(否则会一次性下完整个合集)。"""
    items = [dict(_vid("BVc1", 10)), dict(_vid("BVc2", 20))]
    for it in items:
        it["url"] = it["url"]
    batches, seen_writes, _ = _patch_collection_env(
        monkeypatch, [_coll(seen=[])], lambda cid: items)

    n = auto_refresh.refresh_once()

    assert n == 0, "建立基线的那一轮不得入队"
    assert batches == [], f"基线轮不该下载,实际 {batches}"
    assert seen_writes == [("season", 6365894, ["BVc1", "BVc2"])], \
        f"基线应把当前全部 bvid 记入已见,实际 {seen_writes}"


def test_collection_downloads_only_new_bvids(monkeypatch):
    """已有基线:只入队新出现的条目,已见的不重复下载。"""
    items = [_vid("BVold1", 10), _vid("BVnew", 30), _vid("BVold2", 20)]
    batches, seen_writes, _ = _patch_collection_env(
        monkeypatch, [_coll(seen=["BVold1", "BVold2"])], lambda cid: items)

    n = auto_refresh.refresh_once()

    assert n == 1, f"只应入队 1 条新条目,实际 {n}"
    assert batches == [(["BVnew"], "", 1)], f"实际 {batches}"
    assert seen_writes == [("season", 6365894, ["BVnew"])], \
        "新条目要并入已见,否则下一轮会重复入队"


def test_collection_skips_already_downloaded_new_items(monkeypatch):
    """新增条目若已下载过(比如手动下过),不重复入队,但仍标已见。"""
    new_url = "https://www.bilibili.com/video/BVdl"
    items = [{"bvid": "BVdl", "title": "已下过", "uploader": "UP", "thumbnail": "",
              "duration": 0, "url": new_url}]
    batches, seen_writes, _ = _patch_collection_env(
        monkeypatch, [_coll(seen=["BVx"])], lambda cid: items,
        downloaded={new_url: True})

    n = auto_refresh.refresh_once()

    assert n == 0 and batches == [], "已下载的条目不该再入队"
    assert seen_writes == [("season", 6365894, ["BVdl"])], \
        "已下载的条目也要标已见,否则每轮都要重新判定一次"


def test_collection_retries_failed_task_from_task_history(monkeypatch):
    """下载失败过的合集条目:下一轮必须从任务史恢复重试,不能永久漏稿。

    回归:合集没有时间游标,已见集合一旦推进就不再回头。若失败后不重试,
    该条目的 seen 已落盘,就再也没有任何路径会重新入队——永久漏稿。
    """
    url = "https://www.bilibili.com/video/BVfail"
    items = [{"bvid": "BVfail", "title": "失败的", "uploader": "UP",
              "thumbnail": "", "duration": 0, "url": url}]
    failed_task = {
        "task_id": "old", "url": url, "page": 1, "bvid": "BVfail",
        "source": "auto_refresh", "status": "failed", "auto_attempt": 0,
        "created_at": 1, "finished_at": 1, "uploader_mid": 0,
    }
    batches, seen_writes, _ = _patch_collection_env(
        monkeypatch, [_coll(seen=["BVfail"])], lambda cid: items,
        tasks=[failed_task])

    n = auto_refresh.refresh_once()

    assert batches == [(["BVfail"], "", 1)], \
        f"失败任务必须从任务史重试,实际 {batches}"
    assert n == 1
    assert seen_writes == [("season", 6365894, [])], \
        "重试不改已见集合(该条目本来就在 seen 里)"


def test_collection_too_many_items_fails_loudly_and_keeps_going(monkeypatch):
    """条目数超过去重上限:明确失败且不影响后续合集(不静默截断)。"""
    limit = auto_refresh.up_list.COLLECTION_SEEN_MAX
    huge = [dict(_vid(f"BVh{i}", i)) for i in range(limit + 1)]
    small = [_vid("BVok", 5)]
    calls = []

    def fetch_items(cid):
        calls.append(cid)
        return huge if cid == 6365894 else small

    collections = [_coll(cid=6365894, seen=["BVx"]), _coll(cid=813161, seen=["BVx"])]
    batches, _, sleeps = _patch_collection_env(monkeypatch, collections, fetch_items)

    logged = []
    monkeypatch.setattr(auto_refresh.log, "exception",
                        lambda msg, *a, **k: logged.append(msg % a if a else msg))

    n = auto_refresh.refresh_once()

    assert logged, "超限必须落日志,不能静默吞掉"
    assert calls == [6365894, 813161], "第一个合集失败不得中断整轮"
    assert batches == [(["BVok"], "", 1)], f"第二个合集照常处理,实际 {batches}"
    assert n == 1


def test_collection_gap_sleep_between_ups_and_collections(monkeypatch):
    """最后一个 UP 与第一个合集之间也要睡满防风控间隔。"""
    ups = [{"mid": 1, "uname": "UP1"}]
    batches, _, sleeps = _patch_env(
        monkeypatch, ups, {1: [_vid("BV1", 100)]},
        collections=[_coll(cid=6365894, seen=["BVx"]), _coll(cid=813161, seen=["BVx"])])
    monkeypatch.setattr(auto_refresh.bapi, "fetch_season_content",
                        lambda cid, **kw: [_vid("BVnew", 200)])

    auto_refresh.refresh_once()

    # 1 个 UP(与合集之间 1 次) + 2 个合集之间 1 次 = 2 次
    assert sleeps == [auto_refresh.UP_INTERVAL] * 2, f"实际 {sleeps}"


# ---------- 重试放弃阈值(2026-11-01) ----------

def test_retry_gives_up_after_max_attempts(monkeypatch):
    """永久失败的视频重试到上限后必须放弃。

    退避到顶后每 ~21h 仍新建一条 failed 任务,auto_attempt 无限 +1;
    任务史按 MAX_TOTAL_TASKS 封顶,涨满后连手动下载都被容量检查挡住。
    """
    url = "https://www.bilibili.com/video/BVdead"
    items = [{"bvid": "BVdead", "title": "死稿", "uploader": "UP",
              "thumbnail": "", "duration": 0, "url": url}]
    base = {"task_id": "old", "url": url, "page": 1, "bvid": "BVdead",
            "source": "auto_refresh", "status": "failed",
            "created_at": 1, "finished_at": 1, "uploader_mid": 0}

    at_limit = dict(base, auto_attempt=auto_refresh.AUTO_RETRY_MAX_ATTEMPTS)
    batches, _, _ = _patch_collection_env(
        monkeypatch, [_coll(seen=["BVdead"])], lambda cid: items,
        tasks=[at_limit])
    n = auto_refresh.refresh_once()
    assert batches == [] and n == 0, "达到放弃阈值后不再自动重试"

    below = dict(base, auto_attempt=auto_refresh.AUTO_RETRY_MAX_ATTEMPTS - 1)
    batches2, _, _ = _patch_collection_env(
        monkeypatch, [_coll(seen=["BVdead"])], lambda cid: items,
        tasks=[below])
    n2 = auto_refresh.refresh_once()
    assert n2 == 1, "阈值之下仍要重试,不能提前放弃"


def test_up_retry_items_gives_up_too(monkeypatch):
    """UP 订阅的重试路径同样受放弃阈值约束。"""
    monkeypatch.setattr(auto_refresh.downloader, "is_downloaded",
                        lambda url: False)
    task = {"task_id": "t", "url": "https://www.bilibili.com/video/BVx",
            "page": 1, "bvid": "BVx", "source": "auto_refresh",
            "status": "failed",
            "auto_attempt": auto_refresh.AUTO_RETRY_MAX_ATTEMPTS,
            "created_at": 1, "finished_at": 1}
    out = auto_refresh._retry_items(42, now=10 ** 9, tasks_snapshot=[task])
    assert out == []
