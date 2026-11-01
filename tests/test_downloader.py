"""B1 重试逻辑的结构性测试。

不依赖 yt-dlp mock(易死锁),改用源码静态分析 + 简单业务测试。
"""
import ast
import inspect
from pathlib import Path

import pytest
import yt_dlp

import downloader
import extras
import taskstore


def _mk_task(tid, **kw):
    t = {
        "task_id": tid, "status": "downloading", "progress": 0,
        "speed": 0, "eta": 0, "title": "测试视频", "url": "https://b23.tv/x",
        "label": "", "page": 1, "error": "", "cancel": False,
    }
    t.update(kw)
    downloader._tasks[tid] = t
    return t


def test_retry_loop_present():
    """验证 _run_download 有重试循环。"""
    src = inspect.getsource(downloader._run_download)
    assert "for attempt in range(" in src, "缺少重试循环"
    assert "max_retries" in src, "缺少 max_retries 配置"
    assert "time.sleep" in src, "缺少退避 sleep"
    assert 't.get("cancel")' in src, "缺少取消标志检测"
    assert 't["status"] = "retrying"' in src, "缺少 retrying 状态标记"
    assert 't["error"] = str(last_err)' in src, "缺少最终 error 记录"


def test_cancel_exits_immediately():
    """验证取消标志在循环开头就检测,确保取消的任务及时退出。"""
    src = inspect.getsource(downloader._run_download)
    # 取消检查 `t.get("cancel")` 应在循环内第一次出现
    for_pos = src.find("for attempt in range(")
    cancel_pos = src.find('t.get("cancel")', for_pos)
    assert for_pos > 0 and cancel_pos > for_pos, "循环内应紧跟 cancel 检查"
    # cancel 检查之后应有 return
    return_pos = src.find("return", cancel_pos)
    assert return_pos > 0, "取消检查后应有提前 return"
    # 同时 sleep 后还有第二次 cancel 检查(用户在等待退避时取消)
    assert src.count('t.get("cancel")') >= 2, "sleep 后还应有二次 cancel 检查"


def test_retry_state_renders_in_frontend():
    """前端 statusCN 包含 retrying。"""
    base = Path(__file__).resolve().parent.parent
    html = "\n".join(p.read_text(encoding="utf-8") for p in [
        base / "templates" / "index.html",
        base / "static" / "css" / "main.css",
        *sorted((base / "static" / "js").rglob("*.js"))])
    assert "retrying" in html, "前端缺少 retrying 状态处理"
    # 验证 statusCN 中有中文标签
    assert "重试中" in html


def test_task_state_sets_single_source():
    """活跃/终态集合前后端必须一致,且两者不重叠、并集覆盖全部状态。

    回归:后端曾有三份手写副本(_load_persisted / find_active_by_url / _ACTIVE)
    都漏了 "retrying",而前端 TASK_ACTIVE 有——重试窗口内重复下载拦截失效、
    「清除全部记录」会删掉在跑任务的记录、重启后 retrying 成僵尸态。
    """
    import re
    base = Path(__file__).resolve().parent.parent
    js = (base / "static" / "js" / "pages" / "downloads.js").read_text(encoding="utf-8")

    m = re.search(r"const TASK_ACTIVE = \[([^\]]*)\]", js)
    assert m, "前端未找到 TASK_ACTIVE 定义"
    front_active = set(re.findall(r"'(\w+)'", m.group(1)))
    assert front_active == set(taskstore.ACTIVE), (
        f"活跃状态集合不一致:后端 {sorted(taskstore.ACTIVE)} / 前端 {sorted(front_active)}")

    # 聚合 SSE 的终态集合是前端状态归类与重绘策略的唯一来源；
    # 单个任务进入终态时不能关闭承载所有任务的共享 EventSource。
    terminal_match = re.search(r"const TASK_TERMINAL = \[([^\]]*)\]", js)
    assert terminal_match, "前端未找到 TASK_TERMINAL 定义"
    front_terminal = set(re.findall(r"'(\w+)'", terminal_match.group(1)))
    assert front_terminal == set(taskstore.TERMINAL), (
        f"终态集合不一致:后端 {sorted(taskstore.TERMINAL)} / 前端 {sorted(front_terminal)}")

    assert not set(taskstore.ACTIVE) & set(taskstore.TERMINAL), "活跃态与终态不得重叠"

    # 有中文文案的状态都应被两个集合覆盖(防新增状态漏归类)
    known = set(taskstore.ACTIVE) | set(taskstore.TERMINAL)
    cn_block = re.search(r"const TASK_STATUS_CN = \{(.*?)\};", js, re.S)
    assert cn_block, "前端未找到 TASK_STATUS_CN 定义"
    declared = set(re.findall(r"(\w+):", cn_block.group(1)))
    assert declared <= known, f"前端有未归类的状态:{sorted(declared - known)}"
    # done 只在 TERMINAL 里,没有中文文案(完成态渲染成普通视频卡),单独确认
    assert "done" in taskstore.TERMINAL


def test_sse_stops_on_all_terminal_states():
    """SSE 必须在任何终态收流,而不是只判 done/failed(取消/中断会一直推)。"""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "taskstore.TERMINAL" in src, "SSE 应复用 taskstore.TERMINAL,不要手写状态"
    assert 'in ("done", "failed")' not in src, "不应再有只判 done/failed 的收流条件"


# ---------- 取消立即生效 / persist 节流 / 读取拷贝 ----------

def test_progress_hook_raises_when_cancel_flag_set():
    """取消标志置位后,进度 hook 必须抛 DownloadCancelled 让 yt-dlp 立即中止。

    回归:曾经 hook 只更新状态不检查 cancel,下载会跑完才被标成已取消。
    """
    _mk_task("c1", cancel=True)
    try:
        hook = downloader._progress_hook("c1")
        with pytest.raises(yt_dlp.utils.DownloadCancelled):
            hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
    finally:
        downloader._tasks.pop("c1", None)


def test_progress_hook_raises_when_task_deleted():
    """任务记录被删(删除任务)后,hook 应中止孤儿下载而不是静默继续。"""
    hook = downloader._progress_hook("ghost")
    with pytest.raises(yt_dlp.utils.DownloadCancelled):
        hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})


def test_run_download_cancel_mid_attempt(monkeypatch, tmp_path):
    """下载进行中用户点取消:hook 抛 DownloadCancelled,任务落为 cancelled 且不记 finished_at。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))

    class _CancelMidDownloadYDL:
        def __init__(self, opts):
            self._hooks = opts.get("progress_hooks", [])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            assert downloader.cancel_download("mid1") is True  # 走真实 durable-first 入口
            for h in self._hooks:
                h({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
            return "ok"  # 若 hook 未中止,视为下载完整跑完

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _CancelMidDownloadYDL)
    t = _mk_task("mid1")
    try:
        downloader._run_download(
            "mid1", t["url"], "",
            [{"format_id": "1", "label": "720P", "kind": "av", "tbr": 1, "height": 720}], 1)
        assert t["status"] == "cancelled"
        assert "finished_at" not in t, "取消的任务不应像完成一样记录 finished_at"
    finally:
        downloader._tasks.pop("mid1", None)


def test_progress_hook_persists_once_per_5pct_bucket(monkeypatch):
    """同一 5% 进度桶内的多次回调只落盘一次,跨桶才再落盘。"""
    _mk_task("p1")
    persists = []
    monkeypatch.setattr(downloader, "_persist", lambda: persists.append(1))
    try:
        hook = downloader._progress_hook("p1")
        total = 1000
        for pct in (20.1, 20.4, 20.9):  # 都在 20% 这一格内
            hook({"status": "downloading",
                  "downloaded_bytes": total * pct / 100, "total_bytes": total})
        assert len(persists) == 1, "同桶内重复落盘"
        hook({"status": "downloading", "downloaded_bytes": 255, "total_bytes": total})  # 进 25% 桶
        assert len(persists) == 2, "跨桶应再落盘一次"
    finally:
        downloader._tasks.pop("p1", None)


def test_get_task_returns_copy():
    """get_task 返回拷贝:SSE 序列化时不会被下载线程的并发写影响。"""
    _mk_task("k1", status="downloading")
    try:
        t = downloader.get_task("k1")
        t["status"] = "mutated"
        assert downloader._tasks["k1"]["status"] == "downloading"
    finally:
        downloader._tasks.pop("k1", None)


def test_list_tasks_returns_copies():
    _mk_task("k2")
    try:
        ts = downloader.list_tasks()
        mine = next(t for t in ts if t["task_id"] == "k2")
        mine["status"] = "mutated"
        assert downloader._tasks["k2"]["status"] == "downloading"
    finally:
        downloader._tasks.pop("k2", None)


# ---------- 已下载判定 / 下载 opts / 通知与弹幕钩子(2026-08-15 批次) ----------

def test_is_downloaded():
    """(BV 号, 分 P) 命中 done 任务才算已下载;进行中/失败的不算。

    旧断言曾期望 `.../BV1xx411c7mD?p=3` 为 True —— 任务 dd1 是 (BV1, page=1)、
    查询是 (BV1, page=3),它能判 True 全靠「只比 BV 号」。**那是缺陷 B3 的表现,
    这条断言本身把缺陷当成了规格**:合集下完 P1 后,其余分 P 全被挂上「已下载」
    角标并在批量提交时被跳过(1.6 / 1.7),自动下载也漏下新分 P(1.8)。

    现改为断言 `?p=3` 为 False(B3 的 Fix Checking,需求 2.6 / 2.7),并补一行
    不带 `?p=` 的 True 覆盖 3.12(单 P 视频 URL + 任务 page=1 仍判已下载)。

    Validates: Requirements 2.6, 2.7, 3.12, 3.15
    """
    _mk_task("dd1", status="done", url="https://www.bilibili.com/video/BV1xx411c7mD")  # page=1
    _mk_task("dd2", status="downloading", url="https://www.bilibili.com/video/BV2xx411c7mD")
    try:
        # 判定粒度是 (BV, 分P):同 BV 的其他分 P 不算已下载
        assert downloader.is_downloaded("https://www.bilibili.com/video/BV1xx411c7mD?p=3") is False
        assert downloader.is_downloaded("https://www.bilibili.com/video/BV1xx411c7mD") is True  # 3.12
        assert downloader.is_downloaded("https://www.bilibili.com/video/BV2xx411c7mD") is False
        assert downloader.is_downloaded("https://www.bilibili.com/video/BV3xx411c7mD") is False
    finally:
        downloader._tasks.pop("dd1", None)
        downloader._tasks.pop("dd2", None)


# ---------- 已下载判定的粒度是 (bvid, page)(缺陷 B3) ----------

def test_is_downloaded_is_per_page():
    """同一 BV 下,只有已完成的那个分 P 算已下载,其余分 P 判未下载。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)。
    对应 isBugCondition_B3:同 BV 存在 done 任务但分 P 不同,且被判定的那个
    分 P 自己没有 done 任务。

    未修复现场(taskstore.is_downloaded):
        if t.get("status") == "done" and bvid_from_url(t.get("url") or "") == bv:
            return True
    任务表里 `page` 字段一直存在(start_download / start_batch 都写),URL 里
    `?p=` 一直存在(parse_part 生成、resolve_playlist 产出),但判定把这个维度
    整个丢掉了 —— 于是 (BV1, page=1) 一条 done 任务就把 BV1 的全部分 P 判成
    已下载,反例即下面第一条断言(`?p=2` 未修复时返回 True)。

    Validates: Requirements 2.6, 2.7, 3.12, 3.13
    """
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    _mk_task("pp1", status="done", url=url, page=1)
    try:
        assert downloader.is_downloaded(url) is True, "不带 ?p= 视为 P1,与任务 page=1 相同(3.12)"
        assert downloader.is_downloaded(url + "?p=1") is True, "显式 ?p=1 与任务 page=1 相同(3.13)"
        assert downloader.is_downloaded(url + "?p=2") is False, \
            "P1 已下载不代表 P2 已下载(未修复时这里返回 True —— B3 的反例)"
        assert downloader.is_downloaded(url + "?p=3") is False

        # 再补一条 (BV1, page=3) 的 done 任务:此后只有 P3 跟着变成已下载
        _mk_task("pp3", status="done", url=url, page=3)
        assert downloader.is_downloaded(url + "?p=3") is True, "(BV, 分P) 完全相同 → 已下载(3.13)"
        assert downloader.is_downloaded(url + "?p=2") is False, "P2 仍无 done 任务"
        assert downloader.is_downloaded(url) is True, "P1 的 done 任务仍在"
    finally:
        downloader._tasks.pop("pp1", None)
        downloader._tasks.pop("pp3", None)


def test_is_downloaded_uses_task_page_field_not_task_url():
    """任务侧的分 P 必须读 `t["page"]`,不能解析 `t["url"]` —— 两侧取法刻意不对称。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)。

    这条锁的是实现里最容易搞错的一处:`start_download(url, label, page=3)` 存进
    任务表的 `url` 是用户粘贴的原始链接(**通常不带 `?p=`**),`page` 才是权威值。
    若任务侧也从 URL 解析,这条任务会被算成 P1 —— 判定照样全错,只是错的方向反过来
    (P3 判未下载、P1 判已下载)。而查询侧只能从 URL 解析,因为 is_downloaded(url)
    的入参只有 URL。

    因此本用例故意让「任务 url 不带 ?p= 但 page=3」:
      - 查询 `?p=3` 必须为 True  → 证明任务侧读了 t["page"]
      - 查询不带 `?p=` 必须为 False → 证明任务侧没拿 t["url"] 当 P1 用
    两条断言必须同时成立,任一侧写反都会挂一条。

    Validates: Requirements 2.6, 2.7, 2.8
    """
    url = "https://www.bilibili.com/video/BV1xx411c7mD"  # 任务侧 url 不带 ?p=
    _mk_task("np3", status="done", url=url, page=3)
    try:
        assert downloader.is_downloaded(url + "?p=3") is True, \
            "任务侧应读 t['page'](=3);若解析 t['url'] 会得到 1,这条会挂"
        assert downloader.is_downloaded(url) is False, \
            ("查询侧无 ?p= 视为 P1,与任务的 P3 不同 → 未下载"
             "(未修复时返回 True —— B3 的反例)")
    finally:
        downloader._tasks.pop("np3", None)


@pytest.mark.parametrize("task_page,url_suffix", [
    (1, ""),        # 单 P 视频:两侧都是 P1 → 已下载(3.12)
    (1, "?p=1"),    # 显式 ?p=1 与 page=1 同键 → 已下载(3.13)
    (1, "?p=2"),    # P1 已下载,查 P2 → 未下载(2.6 / 2.7)
    (1, "?p=10"),   # 多位数分 P,同样不该被 P1 顶掉
    (2, ""),        # 任务是 P2,查询无 ?p=(视为 P1)→ 未下载
    (2, "?p=1"),
    (2, "?p=2"),    # 同键 → 已下载
    (3, "?p=3"),    # 同键 → 已下载
    (3, "?p=2"),
    (10, "?p=10"),  # 多位数同键 → 已下载
])
def test_is_downloaded_matches_dl_key_equality(task_page, url_suffix):
    """遍历 (任务 page, URL 是否带 ?p=, URL 的 page) 的完整等价类:
    is_downloaded 的结果必须恰好等于两侧 dl_key 的相等性。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)。
    Property 9: Preservation —— 单 P 与同分 P 的已下载判定。

    这是 Property 3 / 9 的等价类遍历(项目不引入 PBT 框架,见 design.md
    「Property-Based Tests」):键相等的组合必须判已下载(Preservation,含 3.12 的
    「不带 ?p= 的单 P URL + 任务 page=1」与 3.13 的「(BV, 分P) 完全相同」),
    键不等的组合必须判未下载(Fix Checking)。断言的右边直接由 extras.dl_key /
    extras.page_from_url 算出 —— 判定与键规则由同一份实现驱动,任何一侧偷偷换
    口径(例如任务侧改成解析 t["url"])都会在某个组合上露出来。

    键规则(extras.DL_KEY_SEP / page_from_url / dl_key)由任务 9.1 引入;
    在它落地前本用例的守卫断言先挂,失败信息即「键规则尚未引入」。
    守卫写在**测试体内**(而不是模块顶层 import 具名函数),否则收集阶段
    就 AttributeError,会把整个文件的其它用例一起带挂。

    Validates: Requirements 2.6, 2.7, 3.12, 3.13
    """
    for name in ("dl_key", "page_from_url"):
        assert hasattr(extras, name), (
            f"extras.{name} 尚未引入(任务 9.1):已下载判定的键规则唯一来源缺失,"
            "后端判定与前端集合会各写一份且不会报错")

    bv = "BV1xx411c7mD"
    url = f"https://www.bilibili.com/video/{bv}"
    _mk_task("eq1", status="done", url=url, page=task_page)
    try:
        query = url + url_suffix
        task_key = extras.dl_key(bv, task_page)
        query_key = extras.dl_key(bv, extras.page_from_url(query))
        expected = task_key == query_key
        assert downloader.is_downloaded(query) is expected, (
            f"任务键 {task_key} / 查询键 {query_key} → 期望 {expected};"
            "is_downloaded 的判定必须与 dl_key 的相等性一致")
    finally:
        downloader._tasks.pop("eq1", None)


# ---------- 已下载判定的保持基线(缺陷 B3 · Property 9) ----------
#
# 与上一节的分工:上一节(任务 7)锁的是「修好之后才成立」的行为,它们在未修复
# 代码上是红的,因此当不了基线;而且等价类遍历依赖任务 9.1 才引入的 extras.dl_key,
# 键规则落地前连守卫都过不去。下面这批相反 —— 它们在**未修复**代码上就全绿,
# 记录的是 B3 修复**不许动**的那部分语义(3.12–3.15)。修复后必须逐条仍绿。
#
# 断言里刻意不出现 dl_key / page_from_url:基线要能在旧实现上独立成立,一旦引用
# 新键规则就变成了「用新实现证明新实现」,失去保持测试的意义。

# 非 done 的全部状态,直接从状态机的唯一来源取(taskstore.ACTIVE / TERMINAL),
# 不在测试里抄一份字面量:将来新增状态会自动纳入覆盖。缺陷 B5 的根因正是「硬编码
# 的终态副本漏了 interrupted」,测试自己再抄一份就是同一个坑。
_NON_DONE_STATUSES = tuple(s for s in taskstore.ACTIVE + taskstore.TERMINAL
                           if s != "done")


def test_non_done_statuses_derived_from_state_machine():
    """守卫:参数化用的状态集合真的是「状态机全集减去 done」。

    parametrize 的参数列表若意外算成空,pytest 不报错、只会收集 0 个用例——
    3.15 的覆盖就静悄悄消失了。这条守卫把那种失效变成显式失败,同时点名核对
    3.15 列举的五类状态(排队 / 下载中 / 失败 / 取消 / 中断)确实都在集合里。

    Validates: Requirements 3.15
    """
    assert "done" in taskstore.TERMINAL, "done 应是终态之一"
    assert "done" not in _NON_DONE_STATUSES, "基线遍历的是 done 之外的状态"
    assert set(_NON_DONE_STATUSES) == (
        set(taskstore.ACTIVE + taskstore.TERMINAL) - {"done"}), \
        "状态集合必须由 taskstore.ACTIVE / TERMINAL 推导,不得手写副本"
    for s in ("queued", "downloading", "failed", "cancelled", "interrupted"):
        assert s in _NON_DONE_STATUSES, f"3.15 点名的状态 {s} 未被覆盖"


def test_is_downloaded_single_page_url_preserved():
    """3.12 保持基线:不带 `?p=` 的单 P URL,其 BV 已有 done 任务 → 仍判已下载。

    Property 9: Preservation —— 单 P 与同分 P 的已下载判定。

    这是「已下载」判定覆盖面最大的一条:绝大多数视频是单 P,列表页给出的 URL
    不带 `?p=`,任务表里对应的 `page` 就是 1。B3 的修复给判定加了 page 维度,
    最容易踩的坑是把「无 `?p=`」当成「分 P 未知」而判未下载 —— 那会让所有单 P
    视频的角标一夜消失、批量下载重复入队。归一规则必须是「无 `?p=` 即 P1」,
    与任务侧 page 缺省 1 对齐,两边落到同一个键。

    带尾斜杠、带其它查询参数(无 `p=`)是列表页真实产出的 URL 形态,一并锁住。

    Validates: Requirements 3.12, 3.13
    """
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    _mk_task("pv1", status="done", url=url, page=1)
    try:
        assert downloader.is_downloaded(url) is True, "无 ?p= 的单 P URL 应视为 P1(3.12)"
        assert downloader.is_downloaded(url + "/") is True, "尾斜杠形态同样命中"
        assert downloader.is_downloaded(url + "?spm_id_from=333.999") is True, \
            "带其它查询参数但无 p= → 仍是 P1,不得因为「有查询串」就判未下载"
        assert downloader.is_downloaded(url + "?p=1") is True, \
            "显式 ?p=1 与任务 page=1 是同一分 P(3.13)"
    finally:
        downloader._tasks.pop("pv1", None)


@pytest.mark.parametrize("task_url_has_page", [False, True])
@pytest.mark.parametrize("page", [1, 3, 12])
def test_is_downloaded_same_page_preserved(task_url_has_page, page):
    """3.13 保持基线:(BV 号, 分 P) 与某个 done 任务完全相同 → 仍判已下载。

    Property 9: Preservation —— 单 P 与同分 P 的已下载判定。

    任务表里 `url` 有两种真实形态,判定对两者都必须成立:
      - 不带 `?p=`:`start_download(url, label, page=N)` 存的是用户粘贴的原始
        链接,分 P 只体现在 `page` 字段;
      - 带 `?p=N`:`start_batch` 存的是 `resolve_playlist` 产出的条目 URL,
        `page` 与 URL 里的序号一致。
    参数化把两种形态 × 单 P / 多 P / 多位数分 P 全跑一遍,任务侧无论从哪儿取
    page,同分 P 的查询都必须判已下载 —— 这条一挂就说明修复把「已经下过的那一 P」
    也判成了未下载,用户会重复下载同一个文件。

    Validates: Requirements 3.13
    """
    bv = "BV1xx411c7mD"
    base = f"https://www.bilibili.com/video/{bv}"
    task_url = f"{base}?p={page}" if task_url_has_page else base
    _mk_task("sp1", status="done", url=task_url, page=page)
    try:
        assert downloader.is_downloaded(f"{base}?p={page}") is True, \
            f"任务 (BV, P{page}) 已完成,查询同一分 P 必须判已下载(任务 url={task_url})"
    finally:
        downloader._tasks.pop("sp1", None)


@pytest.mark.parametrize("query", [
    "https://b23.tv/abc123",                    # 短链未展开(项目不做展开,原样入库)
    "https://b23.tv/x",
    "https://www.bilibili.com/video/av170001",  # 老 av 号
    "https://www.bilibili.com/video/AV170001",
    "https://example.com/nothing",              # 压根不是 B 站链接
    "",
    None,                                       # 调用方可能传 t.get("url")
])
def test_is_downloaded_without_bvid_preserved(query):
    """3.14 保持基线:URL 里提取不到 BV(短链 / av 号)→ 判未下载,且不抛异常。

    Property 9: Preservation —— 单 P 与同分 P 的已下载判定。

    既有 tests/test_extras.py::test_bvid_from_url 只测了 `bvid_from_url` 本身
    返回 None,没有任何用例验过 `is_downloaded` 在这种输入下的行为 —— 3.14 此前
    是纯盲区。这里补上,并特意让任务表里**存在**一条正常的 done 任务:判定必须
    因为「查询侧取不到 BV」而返回 False,不能因为「表里有 done 任务」就误判已下载,
    也不能因为 BV 为 None 就在拼键 / 比较时炸掉(短链是用户最常粘贴的形态,
    一炸就是整个列表页角标渲染失败)。

    Validates: Requirements 3.14
    """
    _mk_task("nb1", status="done",
             url="https://www.bilibili.com/video/BV1xx411c7mD", page=1)
    try:
        assert downloader.is_downloaded(query) is False, \
            f"{query!r} 取不到 BV,应判未下载(且不得抛异常)"
    finally:
        downloader._tasks.pop("nb1", None)


def test_is_downloaded_task_without_bvid_never_matches():
    """3.14 的镜像面:done 任务的 url 取不到 BV 时,它不归属任何 BV。

    Property 9: Preservation —— 单 P 与同分 P 的已下载判定。

    这是真实场景:用户粘贴 b23.tv 短链下载,任务表里存的就是那条短链(项目不做
    短链展开)。这样一条 done 任务不能被算到任意一个 BV 头上 —— 否则「有过一次
    短链下载」就会把别的视频标成已下载。未修复实现靠 `bvid_from_url(t["url"])`
    返回 None 与查询 BV 不相等来排除它,修复后同样必须排除。

    Validates: Requirements 3.14
    """
    _mk_task("tb1", status="done", url="https://b23.tv/abc123", page=1)
    try:
        assert downloader.is_downloaded(
            "https://www.bilibili.com/video/BV1xx411c7mD") is False, \
            "任务 url 取不到 BV → 不归属任何 BV,不得把别的视频标成已下载"
    finally:
        downloader._tasks.pop("tb1", None)


@pytest.mark.parametrize("status", _NON_DONE_STATUSES)
def test_is_downloaded_non_done_status_preserved(status):
    """3.15 保持基线:只有 done 才算已下载,其余状态一概不计入。

    Property 9: Preservation —— 单 P 与同分 P 的已下载判定。

    既有 test_is_downloaded 只造过一条 `downloading` 任务(且那条断言在未修复
    代码上根本执行不到 —— 测试在它前面的 `?p=3` Fix Checking 断言处就挂了),
    `queued` / `merging` / `cancelling` / `retrying` / `failed` / `cancelled` /
    `interrupted` 全无覆盖。状态从 taskstore.ACTIVE / TERMINAL 推导,将来新增
    状态自动纳入。

    每个状态先断言未下载,再把**同一条任务**改成 done 断言已下载:这个翻转是防
    「假绿」的锚点 —— 只断言 False 的话,URL 写错、BV 拼错、任务没进表都会让它
    照样通过,反而把 3.15 的覆盖变成空气。

    Validates: Requirements 3.15
    """
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    _mk_task("ns1", status=status, url=url, page=1)
    try:
        assert downloader.is_downloaded(url) is False, \
            f"{status} 不是完成态,不得计入已下载(3.15)"
        downloader._tasks["ns1"]["status"] = "done"
        assert downloader.is_downloaded(url) is True, \
            "同一条任务改成 done 后必须判已下载 —— 否则上一条 False 是假绿"
    finally:
        downloader._tasks.pop("ns1", None)


# ---------- 终态任务不可被拉回活跃态(缺陷 B5 · Property 5) ----------
#
# B5 的完整后果链 —— 这一节每条断言都盯着链上的一环:
#
#   1. 服务重启 → `_load_persisted()` 把上次进程被杀时仍处于活跃态的任务统一
#      标记为 `interrupted`(这是正常语义,不是缺陷)。
#   2. 对这条 `interrupted` 任务调 `/api/cancel/<id>`。任务卡因 `interrupted ∉
#      TASK_ACTIVE` 不显示「取消」按钮,但接口本身可达:SSE 重连、脚本、旧页面
#      缓存都能打到它。
#   3. `cancel_download` 的硬编码终态副本 `("done", "failed", "cancelled")` 漏了
#      `interrupted`,于是判它「还没结束」→ 置 `cancel` 标志、状态改成 `cancelling`。
#   4. `cancelling ∈ ACTIVE`,可是此刻压根没有下载线程在跑,没有任何人会来收尾
#      → **永久僵尸态**。
#   5. 僵尸态同时引爆两个下游后果(需求 1.12):
#        a. `find_active_by_url` 认为同 URL + 清晰度 + 分 P「正在下载中」→ 新的
#           下载请求被 409 拦截,这个视频从此下不了;
#        b. `clear_tasks` 只清 `status not in _ACTIVE` 的记录 → 这条记录也删不掉。
#      用户既下不了这个视频、也删不掉这条记录,唯一出路是手改 data/tasks.json。
#
# 根因与 v2.1 修过的那批完全同源:状态集合有硬编码副本。当时收敛了三处**活跃态**
# 副本(都漏 `retrying`),`downloader.py` 里两处**终态**副本没被纳入排查范围。
#
# 全状态遍历用的集合从 taskstore.ACTIVE / TERMINAL 推导(与上面 _NON_DONE_STATUSES
# 同一条纪律):测试自己抄一份字面量,等于把 B5 的根因原封不动搬进测试里。
_ALL_STATUSES = tuple(taskstore.ACTIVE) + tuple(taskstore.TERMINAL)

# 旧硬编码副本的历史原文。这一份**故意不从 TERMINAL 推导** —— 它记录的是「当年
# 写下的那三个字面量」这一历史事实,推导反而会让下面的守卫自我印证、失去意义。
# 守卫负责把它与当前 TERMINAL 绑起来(差集必须恰好是 interrupted)。
_LEGACY_TERMINAL = ("done", "failed", "cancelled")


def _string_collection_lines(src: str, values) -> list[int]:
    """AST 扫描:返回源码里「元素全为字符串、取值集合恰等于 values」的字面量行号。

    不用 `in` 做字符串包含判断,原因有两条:
    - 逗号后空格、单双引号、末尾多余逗号、写成 list/set —— 同一份硬编码副本有
      多种写法,字符串匹配只能抓到一种;
    - 修复后的 docstring 会引用旧写法来解释缺陷(design.md 的 Fix Implementation
      就这么写的),字符串匹配会把那段说明误判成第三份副本。
    AST 只看真正会被执行的字面量,两个问题一并消掉。
    """
    want = set(values)
    lines = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            continue
        strs = [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if len(strs) == len(node.elts) and set(strs) == want:
            lines.append(node.lineno)
    return lines


def test_all_statuses_derived_from_state_machine():
    """守卫:全状态遍历的集合真是 ACTIVE ∪ TERMINAL,且旧副本的差集恰是 interrupted。

    parametrize 的参数列表若意外算成空,pytest 不报错、只收集 0 个用例 —— Property 5
    的穷举覆盖就静悄悄消失了。这条守卫把那种失效变成显式失败。

    后半段把 `_LEGACY_TERMINAL`(旧硬编码副本的历史原文)与当前 `taskstore.TERMINAL`
    绑起来,等价于把 `isBugCondition_B5` 写成可执行断言:差集恰好是 `interrupted`,
    也就是「旧副本漏掉的那一个状态」。将来给 TERMINAL 加了新终态而没同步收敛,这条
    会立刻报出来。

    Validates: Requirements 2.11, 2.12
    """
    assert _ALL_STATUSES, "状态全集为空:遍历会收集 0 个用例,覆盖形同虚设"
    assert set(_ALL_STATUSES) == set(taskstore.ACTIVE) | set(taskstore.TERMINAL), \
        "状态全集必须由 taskstore.ACTIVE / TERMINAL 推导,不得手写副本"
    assert len(_ALL_STATUSES) == len(set(_ALL_STATUSES)), \
        f"活跃态与终态不得重叠:{sorted(set(taskstore.ACTIVE) & set(taskstore.TERMINAL))}"

    assert set(_LEGACY_TERMINAL) < set(taskstore.TERMINAL), \
        "旧硬编码副本应是 TERMINAL 的真子集(这正是它「漏了一个」的含义)"
    assert set(taskstore.TERMINAL) - set(_LEGACY_TERMINAL) == {"interrupted"}, \
        ("isBugCondition_B5 的输入域应恰为 {'interrupted'};若不是,说明 TERMINAL "
         "又加了新终态,两处硬编码副本会再漏一次")


def test_cancel_rejects_terminal_states():
    """已处于终态的任务,取消必须被拒且状态一字不变 —— 含 `interrupted`。

    Property 5: Bug Condition —— 终态任务不可被拉回活跃态。

    对应 `isBugCondition_B5`:状态 ∈ TERMINAL 但 ∉ 旧硬编码副本
    `("done", "failed", "cancelled")`,也就是 `interrupted`。

    未修复现场(downloader.py:379,cancel_download):
        if not t or t["status"] in ("done", "failed", "cancelled"):
            return False
    `interrupted` 不在这个三元组里 → 判它「还没结束」→ 置 `cancel`、改成
    `cancelling` 并返回 True。**反例即 interrupted 那一轮**:未修复时返回 `True`、
    状态变成 `cancelling`,而没有任何下载线程会来收尾(`interrupted` 是重启后的
    历史记录,对应线程早就没了)→ 永久僵尸态。

    `done` / `failed` / `cancelled` 三轮在未修复代码上就是绿的,它们是需求 3.22 的
    保持基线,修复后必须逐条仍绿。状态集合直接取 `taskstore.TERMINAL`,新增终态
    自动纳入覆盖。

    顺带断言 `cancel` 标志未被置位:光看 `status` 不够 —— 标志一旦置上,后续任何
    走到 `_progress_hook` / `_run_download` 的路径都会把这条任务当「用户已取消」。

    Validates: Requirements 2.11, 2.12, 3.22
    """
    assert "interrupted" in taskstore.TERMINAL, \
        "interrupted 必须是终态之一,否则本用例没在测 B5"
    ids = {s: f"ct_{s}" for s in taskstore.TERMINAL}
    try:
        for status, tid in ids.items():
            _mk_task(tid, status=status)
        for status, tid in ids.items():
            assert downloader.cancel_download(tid) is False, \
                (f"{status} 已是终态,取消必须返回 False"
                 f"{'(未修复时返回 True —— B5 的反例)' if status == 'interrupted' else ''}")
            assert downloader._tasks[tid]["status"] == status, \
                (f"{status} 的状态被取消操作改写成 "
                 f"{downloader._tasks[tid]['status']!r} —— 终态任务不得被拉回活跃态")
            assert downloader._tasks[tid]["cancel"] is False, \
                f"{status} 已是终态,不该被置 cancel 标志"
    finally:
        for tid in ids.values():
            downloader._tasks.pop(tid, None)


def test_interrupted_task_not_blocking_and_clearable():
    """`interrupted` 任务:取消被拒之后,既不拦截同 URL 新下载,也能被清除。

    Property 5: Bug Condition —— 终态任务不可被拉回活跃态。

    这条走的是需求 2.12 的**完整后果链**,不是只看 `cancel_download` 的返回值:
    取消前 `interrupted ∉ ACTIVE`,拦截与清除本来就把它当已结束(那是既有的正确
    行为,先断言下来当锚点);一旦 `cancel_download` 把它改成 `cancelling`(活跃态),
    同一份数据上两处判断立刻反转 —— 这才是用户真正踩到的坑(需求 1.12)。

    未修复时的反例链:
        cancel_download("zb1") → True,状态 interrupted → cancelling
        → find_active_by_url(url, label, page) 返回 "zb1"(而不是 None)
          → /api/download 同 URL + 清晰度 + 分 P 被 409 拦截,视频从此下不了
        → clear_tasks(delete_file=False) 返回 0(而不是 1)
          → 「清除全部记录」把它当活跃任务跳过,这条记录也删不掉
    第一条断言就会挂在 `find_active_by_url` 上。

    隔离说明:`clear_tasks` 会遍历整张 `_tasks` 表并删掉所有非活跃记录。
    tests/conftest.py 的 autouse fixture 给每个用例一张干净的任务表(结束后原样
    还回去),`_persist()` 落盘也被重定向到 tmp_path —— 所以这里的 `== 1` 是精确的,
    也不会碰到用户真实的下载史。

    Validates: Requirements 2.11, 2.12
    """
    url = "https://www.bilibili.com/video/BV1xx411c7mD"
    label, page = "1080P 高码率", 2
    _mk_task("zb1", status="interrupted", url=url, label=label, page=page,
             error="服务重启,任务中断")
    try:
        # 锚点:取消之前,两处判断本来就把 interrupted 当已结束
        assert downloader.find_active_by_url(url, label, page) is None, \
            "interrupted ∉ ACTIVE,取消前就不该拦截 —— 这条挂说明前提假设已变"

        # 2.11:取消必须被拒,状态保持 interrupted
        assert downloader.cancel_download("zb1") is False, \
            "interrupted 已是终态,取消必须被拒(未修复时返回 True —— B5 的反例)"
        assert downloader._tasks["zb1"]["status"] == "interrupted", \
            (f"状态被改成 {downloader._tasks['zb1']['status']!r};"
             "未修复时是 cancelling —— 活跃态却无人收尾,即永久僵尸态")

        # 2.12 后果 a:不拦截同 URL + 清晰度 + 分 P 的新下载
        assert downloader.find_active_by_url(url, label, page) is None, \
            "僵尸 cancelling 任务把同 URL 的新下载 409 拦掉了(1.12)"

        # 2.12 后果 b:记录可被「清除全部记录」清掉
        assert downloader.clear_tasks(delete_file=False) == 1, \
            "僵尸 cancelling 任务被当成活跃任务,清除全部记录清不掉它(1.12)"
        assert "zb1" not in downloader._tasks, "记录应已从任务表移除"
    finally:
        downloader._tasks.pop("zb1", None)


@pytest.mark.parametrize("status", _ALL_STATUSES)
def test_cancel_result_matches_terminal_membership(status):
    """遍历 ACTIVE ∪ TERMINAL 的每个状态:`cancel_download` 的返回值必须恰好等于
    `status not in TERMINAL`,且终态任务的状态不被改写。

    Property 5: Bug Condition —— 终态任务不可被拉回活跃态。
    Property 11: Preservation —— 活跃态与其余终态的取消语义。

    这是 Property 5 + 11 的**穷举形式**(项目不引入 PBT 框架,见 design.md
    「Property-Based Tests」:B5 的输入域就是状态机的有限全集,遍历即穷举,比随机
    生成更强)。期望值不写死,而是由 `taskstore.TERMINAL` 现算 —— 判定与状态机由
    同一份唯一来源驱动,将来新增状态会自动被覆盖,不需要回来补用例。

    分支断言:
    - 终态(`done` / `failed` / `cancelled` / `interrupted`):返回 False、`status`
      一字不变、`cancel` 标志不置位。`interrupted` 这一轮在未修复代码上是红的
      (返回 True 且状态变 `cancelling`),就是 B5 的反例;其余三轮是 3.22 的基线。
    - 活跃态(`queued` / `downloading` / `merging` / `cancelling` / `retrying`):
      返回 True、状态变 `cancelling`、`cancel` 置位,由下载线程收尾为 `cancelled`
      (3.21)。这五轮在未修复代码上全绿,修复后必须逐条仍绿 —— 万一把判断写成
      `not in ACTIVE` 之类的反向条件,这五轮会集体报警。

    Validates: Requirements 2.11, 2.12, 3.21, 3.22
    """
    t = _mk_task("sw1", status=status)
    try:
        terminal = status in taskstore.TERMINAL
        assert downloader.cancel_download("sw1") is (not terminal), \
            (f"{status}:取消返回值应为 {not terminal}"
             f"({'终态须拒绝' if terminal else '活跃态须接受'})")
        if terminal:
            assert t["status"] == status, \
                f"{status} 被取消操作改写成 {t['status']!r} —— 终态不得被拉回活跃态"
            assert t["cancel"] is False, f"{status} 已终结,不该被置 cancel 标志"
        else:
            assert t["status"] == "cancelling", \
                f"{status} 取消后应置 cancelling,实际 {t['status']!r}"
            assert t["cancel"] is True, f"{status} 取消后应置 cancel 标志(3.21)"
    finally:
        downloader._tasks.pop("sw1", None)


def test_terminal_checks_use_single_source():
    """单一来源纪律:`downloader.py` 不得再有硬编码终态副本,两处判断都走 TERMINAL。

    Property 5: Bug Condition —— 终态任务不可被拉回活跃态。

    行为断言(上面三条)证明「现在是对的」,这条证明「不会再错第二次」:B5 的根因
    不是某个 `if` 写错,而是**终态集合有第二、第三份手写副本**。v2.1 收敛活跃态副本
    时正是因为只盯着行为、没锁住来源,才留下 downloader.py 里这两处终态副本
    (`_finalize_cancelled` / `cancel_download` 各一份,都漏 `interrupted`)。
    只要副本还在,下次给 TERMINAL 加终态就会再漏一次,而且不会有任何报错。

    断言写法的选择(先 grep 过现场):字面元组 `("done", "failed", "cancelled")` 在
    `downloader.py` 里恰好出现 2 次,全在这两个目标函数内 —— 因此断言范围取
    **整个文件**而不是只取这两个函数的源码,这样第三处副本(比如将来在
    `_run_download` 里新加一处)也会被抓到。判定用 AST 而不是字符串包含,理由见
    `_string_collection_lines`;顺带避开了 `_run_download` 里那行
    `t["status"] = "cancelled" if t.get("cancel") else "done"` —— 它是三元表达式,
    不是元组字面量,不该被误伤。

    Validates: Requirements 2.11, 2.12
    """
    path = Path(inspect.getsourcefile(downloader))
    hits = _string_collection_lines(path.read_text(encoding="utf-8"), _LEGACY_TERMINAL)
    assert not hits, (
        f"{path.name} 仍有硬编码终态副本(行 {hits}):"
        f"{_LEGACY_TERMINAL} 漏了 {sorted(set(taskstore.TERMINAL) - set(_LEGACY_TERMINAL))};"
        "应改用 taskstore.TERMINAL 唯一来源")

    cancel_src = inspect.getsource(downloader.cancel_download)
    store_src = inspect.getsource(taskstore.request_task_cancellation)
    assert "request_task_cancellation" in cancel_src, \
        "下载层取消必须委托给 durable-before-publish 的存储事务"
    assert "TERMINAL" in store_src, \
        "存储层取消事务的终态判断应复用 taskstore.TERMINAL 唯一来源"
    src = inspect.getsource(downloader._finalize_cancelled)
    assert "TERMINAL" in src, \
        "_finalize_cancelled 的终态判断应复用 taskstore.TERMINAL,不要手写状态列表"


# ---------- 取消语义的保持基线(缺陷 B5 · Property 11) ----------
#
# 与上一节的分工:上一节(任务 13)锁的是「修好之后才成立」的行为 —— 它包含
# `interrupted` 那一轮,在未修复代码上是红的,因此当不了基线。下面这批相反:
# 它们在**未修复**代码上就全绿,记录的是 B5 修复**不许动**的那部分语义
# (3.21–3.23 的非缺陷输入面)。`("done","failed","cancelled")` 是 TERMINAL 的
# 真子集,唯一差集是 `interrupted`,所以这些输入的新旧结果必然逐位相同。
#
# 3.21–3.24 在本文件里的覆盖归属(核对过,避免重复造第二份断言):
#
#   3.21 活跃态取消 → cancelling,并由下载线程收尾为 cancelled
#        前半句:test_cancel_result_matches_terminal_membership 的 5 个活跃态轮次
#        后半句:**此前无覆盖** → test_cancel_preserves_active_states(本节新增,
#                把 cancel_download → _finalize_cancelled 的完整转移链跑通)
#   3.22 done / failed / cancelled 取消 → False 且状态不变
#        test_cancel_result_matches_terminal_membership 的 3 个终态轮次(已绿)
#        + test_cancel_rejects_terminal_states 的同名三轮(该用例整体因
#        interrupted 轮次为红,故只把参数化那三条当基线)
#   3.23 收尾时只在尚未终结时才落为 cancelled,已终结状态不被覆盖
#        **此前无任何行为测试**(`_finalize_cancelled` 全项目零直接覆盖,只有
#        test_terminal_checks_use_single_source 从来源侧静态锁过它)→ 本节新增
#        test_finalize_cancelled_preserves_other_terminal_states(不覆盖面)
#        与 test_cancel_preserves_active_states(该覆盖面)
#   3.24 前端状态列表与后端活跃态集合一致
#        test_task_state_sets_single_source(已绿)。本次 B5 只改 downloader.py 的
#        两处终态判断,不碰前端、不改 taskstore.ACTIVE / TERMINAL 的取值,
#        故该用例不受影响,无需新增。
#
# 3.23 的两面必须分清(design.md「B5 → 需求 3.23 一致性确认」):
#   - 对 `done` / `failed` / `cancelled` 不覆盖 = **Preservation**,未修复代码上就绿
#     → 本节锁住。
#   - 对 `interrupted` 不覆盖 = **Fix Checking**,未修复代码上是红的:旧三元组
#     `("done","failed","cancelled")` 不含 interrupted,会把一个已终结的
#     `interrupted` 覆盖成 `cancelled` —— 旧写法反而违反 3.23。
#     这一面**不在本节**:design.md 已论证该分支运行时不可达(`interrupted` 只由
#     `_load_persisted()` 在启动时写入,此时无对应下载线程;修复后 cancel_download
#     也不再给它置 cancel 标志),改动在行为上等价,价值是消灭第二份硬编码副本 ——
#     属于来源侧的事,已由任务 13 的 test_terminal_checks_use_single_source 锁住
#     (它断言 downloader.py 无 legacy 三元组、且 _finalize_cancelled 源码出现
#     TERMINAL),15.1 之后自然转绿。


def test_active_statuses_cover_requirement_3_21():
    """守卫:参数化用的活跃态集合非空,且 3.21 点名的四个状态确实都在里面。

    parametrize 的参数列表若意外算成空,pytest 不报错、只收集 0 个用例 —— 3.21 的
    覆盖就静悄悄消失了。这条把那种失效变成显式失败(与本文件既有的两条守卫
    test_non_done_statuses_derived_from_state_machine /
    test_all_statuses_derived_from_state_machine 同一模式)。

    下面的用例直接遍历 `taskstore.ACTIVE`(5 个)而不是抄 3.21 字面列举的 4 个:
    单一来源纪律 —— B5 的根因正是「状态集合有手写副本」,测试自己再抄一份就是同一个
    坑。多出来的那个是 `cancelling`(对已在取消中的任务重复点取消),它的语义与其余
    活跃态一致(幂等:仍返回 True、仍是 cancelling、标志仍置位),纳入覆盖是加强。

    Validates: Requirements 3.21
    """
    assert taskstore.ACTIVE, "活跃态集合为空:遍历会收集 0 个用例,覆盖形同虚设"
    for s in ("queued", "downloading", "merging", "retrying"):
        assert s in taskstore.ACTIVE, f"3.21 点名的状态 {s} 不在 taskstore.ACTIVE 里"
    assert "cancelling" in taskstore.ACTIVE, \
        "cancelling 应是活跃态(它是取消流程的中间态,由下载线程收尾)"


@pytest.mark.parametrize("status", tuple(taskstore.ACTIVE))
def test_cancel_preserves_active_states(status):
    """活跃态任务的取消语义:置 `cancelling` + 标志,再由收尾落为 `cancelled`。

    Property 11: Preservation —— 活跃态与其余终态的取消语义。

    这条走的是 3.21 的**完整两句**,而不是只看 `cancel_download` 的返回值:

        cancel_download  → 返回 True、status = cancelling、cancel = True
        _finalize_cancelled → status = cancelled

    第二句(「并由下载线程收尾为 `cancelled`」)此前无任何覆盖:
    `test_cancel_result_matches_terminal_membership` 停在 `cancelling`,
    `test_run_download_cancel_mid_attempt` 走的是进度 hook 抛
    `DownloadCancelled` 那条路径,而 `_finalize_cancelled`(取消发生在下载回调
    **之外**时的兜底,`_run_download` 里有三处调用)整个项目零直接测试。B5 要改的
    正是它的终态判断,改之前必须先把它现在的正确行为钉下来。

    为什么这条在未修复代码上就绿:活跃态五个状态都不在旧硬编码三元组
    `("done","failed","cancelled")` 里,也都不在 `TERMINAL` 里 —— 两套判断对它们
    给出完全相同的答案。修复后必须逐条仍绿;万一把条件写反(比如
    `not in ACTIVE`),这五轮会集体报警。

    `cancel` 标志单独断言:光看 status 不够 —— 标志不置位的话,正在跑的 yt-dlp
    线程不会在下个分片边界停下,任务会一路下载完成,「取消」变成了空操作。

    Validates: Requirements 3.21, 3.23
    """
    t = _mk_task("pa1", status=status)
    try:
        assert downloader.cancel_download("pa1") is True, \
            f"{status} 是活跃态,取消必须被接受(3.21)"
        assert t["status"] == "cancelling", \
            f"{status} 取消后应置 cancelling,实际 {t['status']!r}"
        assert t["cancel"] is True, \
            f"{status} 取消后必须置 cancel 标志,否则 yt-dlp 线程不会停(3.21)"

        # 3.21 后半句 / 3.23 的该覆盖面:尚未终结 → 收尾落为 cancelled
        downloader._finalize_cancelled("pa1")
        assert t["status"] == "cancelled", \
            (f"{status} 经取消 → 收尾后应为 cancelled,实际 {t['status']!r};"
             "这一步不落地,任务会永久停在 cancelling(活跃态)成僵尸")
        assert "finished_at" not in t, "取消的任务不应像完成一样记录 finished_at"
    finally:
        downloader._tasks.pop("pa1", None)


@pytest.mark.parametrize("status", _LEGACY_TERMINAL)
def test_finalize_cancelled_preserves_other_terminal_states(status):
    """收尾不得覆盖已终结的任务状态 —— `done` / `failed` / `cancelled` 一字不变。

    Property 11: Preservation —— 活跃态与其余终态的取消语义。

    3.23 的**不覆盖面**。真实场景:下载已经成功落为 `done`(或已 `failed`)之后,
    `_run_download` 的收尾路径或迟到的取消请求又调了一次 `_finalize_cancelled` ——
    它必须认出这条任务已经结束,不能把一个成功的下载改写成「已取消」。用户看到的
    差别是:记录页上一条明明下载好了的视频显示成已取消,而文件其实在磁盘上。

    参数化只遍历 `_LEGACY_TERMINAL`(旧硬编码副本的历史原文,= TERMINAL 减去
    `interrupted`),**这是刻意的**,不是漏了一个:
      - 这三个状态在旧三元组与 `TERMINAL` 里都在 → 新旧行为逐位相同 → Preservation,
        未修复代码上就该绿(本用例);
      - `interrupted` 只在 `TERMINAL` 里 → 未修复代码会把它覆盖成 `cancelled`
        (违反 3.23)→ 那是 **Fix Checking**,未修复代码上必红,归属见本节顶部注释。
    把两面混进同一个用例会让它整体变红,基线就没了 —— 这正是
    `test_cancel_rejects_terminal_states` 现在的处境。

    末尾的翻转是防「假绿」的锚点:只断言「状态没变」的话,任务没进表、task_id 拼错、
    `_finalize_cancelled` 整个是空实现,都会让它照样通过。把同一条任务改成活跃态后
    再调一次,必须变成 `cancelled` —— 证明这个函数真的会写状态,前面那条「没变」
    才有意义。

    Validates: Requirements 3.22, 3.23
    """
    assert status in taskstore.TERMINAL, f"{status} 应是终态之一,否则本用例没在测 3.23"
    t = _mk_task("fz1", status=status)
    try:
        downloader._finalize_cancelled("fz1")
        assert t["status"] == status, \
            (f"{status} 已终结,收尾不得把它改写成 {t['status']!r} —— "
             "已终结的任务状态不被覆盖(3.23)")

        # 翻转锚点:同一条任务改成活跃态后,收尾必须真的写状态
        t["status"] = "cancelling"
        downloader._finalize_cancelled("fz1")
        assert t["status"] == "cancelled", \
            "改成活跃态后收尾仍未落为 cancelled —— 上一条「状态没变」是假绿"
    finally:
        downloader._tasks.pop("fz1", None)


def test_finalize_cancelled_tolerates_missing_task():
    """收尾一条已被删除的任务:静默返回,不抛异常。

    Property 11: Preservation —— 活跃态与其余终态的取消语义。

    `_run_download` 里有三处调用 `_finalize_cancelled`,都在锁外;这期间用户完全
    可能点了「删除任务」把记录从 `_tasks` 里摘掉(`_progress_hook` 正是靠「任务
    记录不存在」来中止孤儿下载的,见 test_progress_hook_raises_when_task_deleted)。
    此时收尾拿到的是 `None`,必须走 `if t and ...` 的短路而不是 KeyError ——
    一旦抛异常,下载线程会带着未清理的状态退出。

    这条与 B5 的判断条件无关(`t` 为 None 时压根不比状态),因此修复前后都绿;
    锁下来是因为 `if t and t["status"] not in (...)` 这个 `t and` 短路很容易在
    改写终态判断时被顺手弄掉。

    Validates: Requirements 3.23
    """
    assert "ghost_fz" not in downloader._tasks
    downloader._finalize_cancelled("ghost_fz")  # 不抛异常即通过
    assert "ghost_fz" not in downloader._tasks, "收尾不得为不存在的任务凭空建记录"


def _reset_dl_config(monkeypatch):
    monkeypatch.setattr(downloader.config, "SORT_BY_UP", False)
    monkeypatch.setattr(downloader.config, "SAVE_COVER", False)
    monkeypatch.setattr(downloader.config, "SAVE_SUBTITLE", False)
    monkeypatch.setattr(downloader.config, "SAVE_DANMAKU", False)
    monkeypatch.setattr(downloader.config, "NOTIFY", False)


def test_base_opts_audio_single_track_no_merge(monkeypatch):
    """仅音频单轨不需要 merge_output_format;组合轨/best 仍合并为 mp4。"""
    _reset_dl_config(monkeypatch)
    o = downloader._base_opts("30280", 1, "t")
    assert "merge_output_format" not in o
    for fmt in ("30016+30280", "best"):
        assert downloader._base_opts(fmt, 1, "t")["merge_output_format"] == "mp4"


def test_base_opts_sort_by_up_and_extras(monkeypatch):
    """按 UP 归档时 outtmpl 带清洗后的 UP 子目录 + 字面标题名(文件名对齐 A.2)。"""
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(downloader.config, "SORT_BY_UP", True)
    monkeypatch.setattr(downloader.config, "SAVE_COVER", True)
    monkeypatch.setattr(downloader.config, "SAVE_SUBTITLE", True)
    _mk_task("bo1", title="多P 视频", uploader="某UP", page=2)
    try:
        o = downloader._base_opts("30016+30280", 2, "bo1")
        assert o["outtmpl"].endswith("某UP/多P 视频 - P2.%(ext)s")
        assert o["writethumbnail"] is True
        assert o["writesubtitles"] is True
        assert o["subtitleslangs"]
    finally:
        downloader._tasks.pop("bo1", None)


def test_run_download_success_notifies_and_saves_danmaku(monkeypatch, tmp_path):
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(downloader.config, "SAVE_DANMAKU", True)
    monkeypatch.setattr(downloader.config, "NOTIFY", True)

    notified, saved, downloads = [], [], []

    def fail_after_capture(task_id, title, path):
        notified.append((task_id, title, path))
        raise RuntimeError("模拟通知失败")

    monkeypatch.setattr(downloader, "_notify_done", fail_after_capture)
    monkeypatch.setattr(downloader.extras, "save_danmaku_for",
                        lambda url, p, page=1: saved.append((url, str(p))))

    (tmp_path / "测试视频.mp4").write_bytes(b"x" * 10)

    class _OkYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            downloads.append(tuple(urls))
            return "ok"

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _OkYDL)
    t = _mk_task("ok1", title="测试视频", url="https://www.bilibili.com/video/BV1xx411c7mD")
    try:
        downloader._run_download(
            "ok1", t["url"], "",
            [{"format_id": "1", "label": "720P", "kind": "av", "tbr": 1, "height": 720}], 1)
        assert t["status"] == "done"
        assert "finished_at" in t
        assert downloads == [(t["url"],)]
        assert notified == [(
            "ok1", "测试视频", str((tmp_path / "测试视频.mp4").resolve())
        )]
        assert saved and saved[0][0] == t["url"] and saved[0][1].endswith("测试视频.mp4")
    finally:
        downloader._tasks.pop("ok1", None)


def test_run_download_success_notify_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(downloader.config, "NOTIFY", False)

    notified = []
    monkeypatch.setattr(
        downloader, "_notify_done",
        lambda task_id, title, path: notified.append((task_id, title, path)),
    )

    class _OkYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            return "ok"

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _OkYDL)
    t = _mk_task("ok2", title="x", url="https://www.bilibili.com/video/BV1zz411c7mD")
    try:
        downloader._run_download(
            "ok2", t["url"], "",
            [{"format_id": "1", "label": "720P", "kind": "av", "tbr": 1, "height": 720}], 1)
        assert t["status"] == "done"
        assert notified == []
    finally:
        downloader._tasks.pop("ok2", None)

# ---------- 清理失败/失效记录(2026-08-18) ----------

def test_task_files_scans_up_subdirectory(monkeypatch, tmp_path):
    """按 UP 归档(SORT_BY_UP)时文件在「UP名/」子目录,也要能找到。"""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    up_dir = tmp_path / "远古野"
    up_dir.mkdir()
    (up_dir / "视频标题 part1.mp4").write_bytes(b"x")
    files = downloader._task_files({"title": "视频标题"})
    assert len(files) == 1 and files[0].parent == up_dir


def test_clean_invalid_records(monkeypatch, tmp_path):
    """失败 + 文件已不在本地的 done 记录删除;文件还在的 done、进行中的保留。"""
    persists = []
    monkeypatch.setattr(taskstore, "_persist", lambda: persists.append(1))
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    (tmp_path / "还在的完成视频.mp4").write_bytes(b"x")

    # 隔离:模块加载时从真实 tasks.json 恢复过存量任务,测试内只用自己的
    saved = dict(downloader._tasks)
    downloader._tasks.clear()
    _mk_task("f1", status="failed", title="失败任务", error="boom")
    _mk_task("d1", status="done", title="还在的完成视频")
    _mk_task("d2", status="done", title="文件被删的视频")
    _mk_task("g1", status="downloading", title="进行中")
    try:
        r = downloader.clean_invalid_records()
        assert r == {"failed": 1, "invalid": 1, "removed": 2}
        assert set(downloader._tasks) == {"d1", "g1"}, "只留下文件还在的 done 和进行中"
        assert (tmp_path / "还在的完成视频.mp4").exists(), "清理记录不得删文件"
    finally:
        downloader._tasks.clear()
        downloader._tasks.update(saved)


# ---------- 任务史持久化:并发安全 + 原子写 + 损坏备份(2026-08-19 评审 P0) ----------

def test_persist_concurrent_writes_keep_valid_json(monkeypatch, tmp_path):
    """多线程并发 _persist 后 tasks.json 必须仍是合法 JSON(独立写锁+原子替换),
    且无 .tmp 残留——tasks.json 是下载史唯一载体,写坏即丢史。"""
    import json as _json
    import threading as _th
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    saved = dict(downloader._tasks)
    downloader._tasks.clear()
    try:
        for i in range(5):
            _mk_task(f"c{i}", status="done", title=f"视频{i}", progress=100)
        threads = [_th.Thread(target=taskstore._persist) for _ in range(16)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        data = _json.loads(taskstore.TASKS_FILE.read_text(encoding="utf-8"))
        assert len(data) == 5, "并发写盘后历史应完整"
        assert not list(tmp_path.glob("*.tmp")), "不应残留临时文件"
    finally:
        downloader._tasks.clear()
        downloader._tasks.update(saved)


def test_persist_write_is_atomic_via_tmp_rename(monkeypatch, tmp_path):
    """取消使用统一原子写，并且只有 durable 成功后才向 worker 发布。"""
    import inspect
    import threading as _th

    src = inspect.getsource(taskstore._persist)
    cancel_src = inspect.getsource(taskstore.request_task_cancellation)
    assert "storage.write_json" in src and "storage.write_json" in cancel_src, \
        "普通提交与 durable-first 取消都必须走统一原子持久化层"
    assert taskstore._persist_lock is not taskstore._tasks_lock, \
        "写盘锁应独立于任务锁(慢 IO 不阻塞进度观察)"

    task = _mk_task("cancel-durable", status="downloading")
    identity = id(task)
    entered, release = _th.Event(), _th.Event()
    written = []
    outcome = {}

    def blocked_write(path, payload, *args, **kwargs):
        written.append(payload)
        entered.set()
        assert release.wait(5), "测试写盘闸门未放行"
        return True

    def invoke_cancel():
        try:
            outcome["value"] = downloader.cancel_download("cancel-durable")
        except BaseException as exc:  # 线程异常必须回传主线程断言
            outcome["error"] = exc

    monkeypatch.setattr(taskstore.storage, "write_json", blocked_write)
    worker = _th.Thread(target=invoke_cancel, daemon=True)
    try:
        worker.start()
        assert entered.wait(2), "取消应先进入 tasks.json 原子写"
        assert id(downloader._tasks["cancel-durable"]) == identity
        assert task["status"] == "downloading" and task["cancel"] is False, \
            "写盘成功前 worker 不得观察到 cancelling/cancel"
        assert written[0][0]["status"] == "cancelling", \
            "候选快照必须先包含取消事实"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcome == {"value": True}
    assert id(downloader._tasks["cancel-durable"]) == identity, \
        "发布取消不得替换既有任务字典"
    assert task["status"] == "cancelling" and task["cancel"] is True

    # 若首个写盘期间 worker 已先终结，取消事务不得复活它；持久化锁释放前
    # 必须再写一份当前全表，覆盖短暂出现的旧 cancelling 候选。
    task["status"] = "downloading"
    task["cancel"] = False
    race_entered, race_release = _th.Event(), _th.Event()
    race_writes = []
    race_outcome = {}

    def racing_write(path, payload, *args, **kwargs):
        race_writes.append(payload)
        if len(race_writes) == 1:
            race_entered.set()
            assert race_release.wait(5)
        return True

    def invoke_racing_cancel():
        race_outcome["value"] = downloader.cancel_download("cancel-durable")

    monkeypatch.setattr(taskstore.storage, "write_json", racing_write)
    racing_worker = _th.Thread(target=invoke_racing_cancel, daemon=True)
    try:
        racing_worker.start()
        assert race_entered.wait(2)
        with taskstore._tasks_lock:
            task["status"] = "done"
            task["progress"] = 100
    finally:
        race_release.set()
        racing_worker.join(5)
    assert race_outcome == {"value": False}
    assert task["status"] == "done" and task["cancel"] is False
    assert len(race_writes) == 2
    assert race_writes[0][0]["status"] == "cancelling"
    assert race_writes[1][0]["status"] == "done", \
        "IO 期间已终结时必须用当前终态校正磁盘，不能复活取消中任务"

    # 写失败不能靠回滚补救：共享取消标志从未发布，只有持久化健康变差。
    task["status"] = "downloading"
    task["progress"] = 0
    task["cancel"] = False
    monkeypatch.setattr(taskstore.storage, "write_json", lambda *a, **k: False)
    with pytest.raises(taskstore.TaskStoreWriteError):
        downloader.cancel_download("cancel-durable")
    assert id(downloader._tasks["cancel-durable"]) == identity
    assert task["status"] == "downloading" and task["cancel"] is False
    assert task["durable"] is False and task.get("persistence_error"), \
        "失败只允许暴露持久化健康，不得发布不可逆取消副作用"


def test_load_persisted_backs_up_corrupt_not_silent_wipe(monkeypatch, tmp_path):
    """tasks.json 损坏:改名备份 .corrupt-* 并记日志,绝不静默当空历史
    (旧行为:解析失败直接 return,下次写盘把整份下载史覆盖清零)。"""
    f = tmp_path / "tasks.json"
    f.write_text('{"broken": [', encoding="utf-8")
    monkeypatch.setattr(taskstore, "TASKS_FILE", f)
    saved = dict(downloader._tasks)
    downloader._tasks.clear()
    try:
        taskstore._load_persisted()
        assert not downloader._tasks, "损坏文件不应加载出任务"
        backups = list(tmp_path.glob("tasks.json.corrupt-*"))
        assert backups, "损坏的 tasks.json 应留备份供手工找回"
        assert not f.exists(), "原路径损坏文件应移走(下次写盘全新开始)"
    finally:
        downloader._tasks.clear()
        downloader._tasks.update(saved)


# ---------- 统计面板 ----------

def test_disk_bytes_counts_subdirs(monkeypatch, tmp_path):
    """磁盘用量要递归统计:开了「按 UP 建子目录」后文件全在子目录里。

    回归:旧实现只 iterdir() 扫顶层,归档模式下磁盘用量恒为 0,
    历史任务缺 downloaded_bytes 时总大小也会被兜底成 0。
    """
    monkeypatch.setattr(taskstore.config, "DOWNLOAD_DIR", tmp_path)
    (tmp_path / "顶层.mp4").write_bytes(b"a" * 100)
    up_dir = tmp_path / "某UP主"
    up_dir.mkdir()
    (up_dir / "归档.mp4").write_bytes(b"b" * 250)

    stats = taskstore.compute_stats()
    assert stats["disk_bytes"] == 350, "子目录文件应计入磁盘用量"


def test_disk_bytes_survives_missing_dir(monkeypatch, tmp_path):
    """下载目录不存在时统计不炸,返回 0。"""
    monkeypatch.setattr(taskstore.config, "DOWNLOAD_DIR", tmp_path / "并不存在")
    assert taskstore.compute_stats()["disk_bytes"] == 0


# ---------- DASH 多流进度聚合 / 排队任务删除 ----------

def test_progress_hook_aggregates_dash_streams_monotonically(monkeypatch):
    """DASH 组合轨两流依次下载:进度不得回跳,全部流完成前不得报 100%。

    回归:旧实现直接取最近一次回调的流内百分比——视频流下完 100% 后,
    音轨从 0% 重新爬,页面进度条回跳;且第一条流 finished 就把状态置成
    merging,随后又被 downloading 覆盖,状态来回闪。

    上一版只按字节总和聚合,但「视频流独自占满分母」仍会先到 100%,
    第二条流一出现分母变大就掉回去,所以这里逐步骤断言单调性。
    """
    _mk_task("agg1")
    persists = []
    monkeypatch.setattr(downloader, "_persist", lambda: persists.append(1))
    try:
        hook = downloader._progress_hook("agg1", 2)
        seen = []

        def step(**payload):
            hook(payload)
            seen.append(downloader._tasks["agg1"]["progress"])

        # 视频流: 0→100%(总量 800)
        for dl in (0, 400, 800):
            step(status="downloading", downloaded_bytes=dl, total_bytes=800,
                 info_dict={"format_id": "303"})
        assert downloader._tasks["agg1"]["status"] == "downloading"
        assert seen[-1] < 100.0, f"两流只完成一条就报 100%: {seen}"
        # 视频流 finished:仍是两流之一,不得进入 merging
        hook({"status": "finished", "info_dict": {"format_id": "303"}})
        assert downloader._tasks["agg1"]["status"] == "downloading", \
            "第一条流完成就置 merging 会导致状态闪烁"
        # 音轨开始并推进:进度只能前进,不得回跳
        step(status="downloading", downloaded_bytes=0, total_bytes=200,
             info_dict={"format_id": "302"})
        step(status="downloading", downloaded_bytes=100, total_bytes=200,
             info_dict={"format_id": "302"})
        assert all(b >= a for a, b in zip(seen, seen[1:])), \
            f"进度出现回跳: {seen}"
        # 音轨 finished:两流全部完成,才进入 merging/100%
        hook({"status": "finished", "info_dict": {"format_id": "302"}})
        assert downloader._tasks["agg1"]["status"] == "merging"
        assert downloader._tasks["agg1"]["progress"] == 100
    finally:
        downloader._tasks.pop("agg1", None)


def test_progress_hook_single_stream_keeps_old_semantics(monkeypatch):
    """单流下载(仅音频/单体格式)行为不变:finished 即 merging/100%。"""
    _mk_task("s1")
    try:
        hook = downloader._progress_hook("s1", 1)
        hook({"status": "downloading", "downloaded_bytes": 5,
              "total_bytes": 10, "info_dict": {"format_id": "302"}})
        assert downloader._tasks["s1"]["progress"] == 50.0
        hook({"status": "finished", "info_dict": {"format_id": "302"}})
        assert downloader._tasks["s1"]["status"] == "merging"
        assert downloader._tasks["s1"]["progress"] == 100
    finally:
        downloader._tasks.pop("s1", None)


def test_progress_hook_fallback_without_format_id(monkeypatch):
    """回调缺 format_id 时退化为单桶记账,等价旧行为。"""
    _mk_task("nf1")
    try:
        hook = downloader._progress_hook("nf1", 2)
        hook({"status": "downloading", "downloaded_bytes": 8, "total_bytes": 10})
        assert downloader._tasks["nf1"]["progress"] == 80.0
        # 第一条流 finished(无 format_id,与 downloading 同桶)
        hook({"status": "finished"})
        # 第二条流 finished:达到 expected=2,进入 merging
        hook({"status": "finished"})
        assert downloader._tasks["nf1"]["status"] == "merging"
    finally:
        downloader._tasks.pop("nf1", None)


def test_base_opts_passes_stream_count_to_progress_hook(monkeypatch, tmp_path):
    """组合轨 format 的进度钩子要拿到正确的流数。"""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    _mk_task("bo1", title="标题", status="queued")
    try:
        opts = downloader._base_opts("303+302", 1, "bo1")
        hooks = opts["progress_hooks"]
        assert len(hooks) == 1
        # 通过行为验证流数:两次 finished 才 merging
        hooks[0]({"status": "finished", "info_dict": {"format_id": "303"}})
        assert downloader._tasks["bo1"]["status"] != "merging"
        hooks[0]({"status": "finished", "info_dict": {"format_id": "302"}})
        assert downloader._tasks["bo1"]["status"] == "merging"
    finally:
        downloader._tasks.pop("bo1", None)


def test_base_opts_treats_best_as_two_streams(monkeypatch, tmp_path):
    """``format=best`` 必须按两流计:它可能解析成 bestvideo+bestaudio。

    回归:流数原来只按 ``fmt_str.count("+") + 1`` 算,"best" 得到 1。而
    ``_base_opts`` 对 "best" 恰恰设置了 ``merge_output_format``(说明它预期
    可能是组合轨)。按单流记账时,第一条流 finished 就把状态置成
    merging/100%,第二条流一开始又退回 downloading,进度卡在 100% 直到
    音轨下完——正是多流聚合要消灭的回跳与状态闪烁。
    """
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    _mk_task("bo2", title="标题", status="queued")
    try:
        opts = downloader._base_opts("best", 1, "bo2")
        hooks = opts["progress_hooks"]
        assert len(hooks) == 1
        # 第一条流下完:仍是两流之一,不得报 100%/merging
        hooks[0]({"status": "finished", "info_dict": {"format_id": "bv"}})
        assert downloader._tasks["bo2"]["status"] != "merging", \
            '"best" 被当成单流:第一条流完成就误报合并中'
        assert downloader._tasks["bo2"]["progress"] < 100
        # 第二条流(音轨)下完才进入 merging
        hooks[0]({"status": "finished", "info_dict": {"format_id": "ba"}})
        assert downloader._tasks["bo2"]["status"] == "merging"
        assert downloader._tasks["bo2"]["progress"] == 100
    finally:
        downloader._tasks.pop("bo2", None)


def test_delete_queued_task_does_not_wait_for_busy_workers(monkeypatch, tmp_path):
    """删除仍在队列、从未开始执行的任务:不等 worker 静默,立即删成功。

    回归:旧实现把「排队中」任务也送进 10 秒 quiesce 等待——worker 全忙
    时该作业迟迟轮不到执行,删除永远超时失败,任务卡在「取消中」。
    """
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)

    # 占住全部 worker 槽位的慢下载:确保目标任务一直轮不到执行。
    import threading as _threading
    release_slow = _threading.Event()

    def _slow_run_download(*args, **kwargs):
        release_slow.wait(timeout=30)

    task = _mk_task("q1", status="queued", cancel=False)
    task["artifact_root"] = str(tmp_path)
    task["manifest"] = {"version": 1, "artifacts": []}
    slow = downloader._RuntimeJob(("slow0", "u", "l", [], 1))
    try:
        with downloader._runtime_jobs_lock:
            downloader._runtime_jobs["slow0"] = slow
        # 目标任务:已注册 runtime 但从未 started。
        job = downloader._RuntimeJob(("q1", "u", "l", [], 1))
        with downloader._runtime_jobs_lock:
            downloader._runtime_jobs["q1"] = job

        result = downloader.delete_tasks_detailed(
            ["q1"], delete_file=False, timeout=0.3)
        assert result["count"] == 1, \
            "排队中(未开始)任务的删除不应等待 worker 静默"
        assert downloader._tasks.get("q1") is None or \
            downloader._tasks["q1"].get("status") in taskstore.TERMINAL
        # 取消意图已同步落成 cancelled 终态(若记录仍在)。
        if "q1" in downloader._tasks:
            assert downloader._tasks["q1"]["status"] == "cancelled"
    finally:
        release_slow.set()
        with downloader._runtime_jobs_lock:
            downloader._runtime_jobs.pop("q1", None)
            downloader._runtime_jobs.pop("slow0", None)
        downloader._tasks.pop("q1", None)
        downloader._tasks.pop("slow0", None)


def test_delete_running_task_still_waits_for_quiesce(monkeypatch, tmp_path):
    """已开始执行的下载任务仍必须等待 worker 静默后才删除。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    import threading as _threading
    started = _threading.Event()
    release = _threading.Event()

    _mk_task("r1", status="downloading")
    job = downloader._RuntimeJob(("r1", "u", "l", [], 1))
    job.started.set()  # worker 已进入执行
    try:
        with downloader._runtime_jobs_lock:
            downloader._runtime_jobs["r1"] = job

        def _wait_in_other_thread():
            result = downloader.delete_tasks_detailed(
                ["r1"], delete_file=False, timeout=5)
            return result

        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_wait_in_other_thread)
            assert started.wait(2) is False, "未放行前不应有 started 事件可等"
            # 删除应阻塞在 quiesce 等待,而不是立刻返回
            done = _cf.wait([fut], timeout=0.4).done
            assert not done or fut.result()["count"] == 0, \
                "运行中的任务不允许在 worker 静默前删除成功"
            # worker 收尾:落 cancelled 终态并静默
            downloader._finalize_cancelled("r1")
            job.quiesced.set()
            result = fut.result(timeout=5)
        assert result["count"] == 1
    finally:
        release.set()
        job.quiesced.set()
        with downloader._runtime_jobs_lock:
            downloader._runtime_jobs.pop("r1", None)
        downloader._tasks.pop("r1", None)


def test_progress_hook_reset_discards_previous_attempt_state(monkeypatch):
    """重试前必须清空上一次尝试的流记账,否则重试会瞬间跳到 100%。

    回归:_base_opts 在重试循环之外构建,同一个 progress hook 闭包跨 attempt
    复用。上一次尝试已经把「视频流」标成 finished,重试的音轨一下完就满足
    「全部流完成」,进度直接 100% 并置 merging——而音轨其实还在下,用户看到
    的是「重试中」几秒后满格卡住。
    """
    _mk_task("rs1", status="retrying", progress=0)
    try:
        hook = downloader._progress_hook("rs1", 2)
        # 上一次尝试:视频流下完并 finished
        hook({"status": "downloading", "downloaded_bytes": 800,
              "total_bytes": 800, "info_dict": {"format_id": "303"}})
        hook({"status": "finished", "info_dict": {"format_id": "303"}})
        assert downloader._tasks["rs1"]["status"] == "downloading"

        hook.reset()  # _run_download 在每次 attempt 开始前调用
        # 重试的音轨刚下 10%:不得因为上一次的 finished 计数就报 100%/merging
        hook({"status": "downloading", "downloaded_bytes": 20,
              "total_bytes": 200, "info_dict": {"format_id": "302"}})
        task = downloader._tasks["rs1"]
        assert task["progress"] <= 20.0, \
            f"重试沿用了上一次尝试的字节记账: {task['progress']}"
        assert task["status"] == "downloading", "重试首个回调不得误报 merging"
        # 这次两条流都真正完成,才允许 100%
        hook({"status": "finished", "info_dict": {"format_id": "302"}})
        hook({"status": "finished", "info_dict": {"format_id": "303"}})
        assert task["status"] == "merging"
        assert task["progress"] == 100
    finally:
        downloader._tasks.pop("rs1", None)


def test_run_download_resets_progress_hooks_per_attempt():
    """重试循环里必须对 progress hook 调 reset():光有 reset 方法不接线没用。"""
    src = inspect.getsource(downloader._run_download)
    assert 'getattr(hook, "reset", None)' in src, "重试前没有重置进度钩子"
    loop_idx = src.find("for attempt in range(")
    reset_idx = src.find('getattr(hook, "reset", None)')
    assert 0 < loop_idx < reset_idx, "重置必须发生在重试循环内部"
