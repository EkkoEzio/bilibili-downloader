"""B1 重试逻辑的结构性测试。

不依赖 yt-dlp mock(易死锁),改用源码静态分析 + 简单业务测试。
"""
import inspect
from pathlib import Path

import pytest
import yt_dlp

import downloader


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
    assert "_t.sleep" in src, "缺少退避 sleep"
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
    html = "\n".join((base / p).read_text(encoding="utf-8") for p in (
        "templates/index.html", "static/js/app.js", "static/css/main.css"))
    assert "retrying" in html, "前端缺少 retrying 状态处理"
    # 验证 statusCN 中有中文标签
    assert "重试中" in html


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
    monkeypatch.setattr(downloader, "TASKS_FILE", tmp_path / "tasks.json")
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
            downloader._tasks["mid1"]["cancel"] = True  # 模拟用户此刻点取消
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
    """done 任务的 bvid 命中即视为已下载;进行中/失败的不算。"""
    _mk_task("dd1", status="done", url="https://www.bilibili.com/video/BV1xx411c7mD")
    _mk_task("dd2", status="downloading", url="https://www.bilibili.com/video/BV2xx411c7mD")
    try:
        assert downloader.is_downloaded("https://www.bilibili.com/video/BV1xx411c7mD?p=3") is True
        assert downloader.is_downloaded("https://www.bilibili.com/video/BV2xx411c7mD") is False
        assert downloader.is_downloaded("https://www.bilibili.com/video/BV3xx411c7mD") is False
    finally:
        downloader._tasks.pop("dd1", None)
        downloader._tasks.pop("dd2", None)


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
    monkeypatch.setattr(downloader, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(downloader.config, "SAVE_DANMAKU", True)
    monkeypatch.setattr(downloader.config, "NOTIFY", True)

    notified, saved = [], []
    monkeypatch.setattr(downloader, "_notify_done", lambda title: notified.append(title))
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
            return "ok"

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _OkYDL)
    t = _mk_task("ok1", title="测试视频", url="https://www.bilibili.com/video/BV1xx411c7mD")
    try:
        downloader._run_download(
            "ok1", t["url"], "",
            [{"format_id": "1", "label": "720P", "kind": "av", "tbr": 1, "height": 720}], 1)
        assert t["status"] == "done"
        assert "finished_at" in t
        assert notified == ["测试视频"]
        assert saved and saved[0][0] == t["url"] and saved[0][1].endswith("测试视频.mp4")
    finally:
        downloader._tasks.pop("ok1", None)


def test_run_download_success_notify_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(downloader.config, "NOTIFY", False)

    notified = []
    monkeypatch.setattr(downloader, "_notify_done", lambda title: notified.append(title))

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
    monkeypatch.setattr(downloader, "_persist", lambda: persists.append(1))
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
    monkeypatch.setattr(downloader, "TASKS_FILE", tmp_path / "tasks.json")
    saved = dict(downloader._tasks)
    downloader._tasks.clear()
    try:
        for i in range(5):
            _mk_task(f"c{i}", status="done", title=f"视频{i}", progress=100)
        threads = [_th.Thread(target=downloader._persist) for _ in range(16)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        data = _json.loads(downloader.TASKS_FILE.read_text(encoding="utf-8"))
        assert len(data) == 5, "并发写盘后历史应完整"
        assert not list(tmp_path.glob("*.tmp")), "不应残留临时文件"
    finally:
        downloader._tasks.clear()
        downloader._tasks.update(saved)


def test_persist_write_is_atomic_via_tmp_rename(monkeypatch, tmp_path):
    """写盘走 storage.write_json(原子替换,详见 test_storage)。"""
    import inspect
    src = inspect.getsource(downloader._persist)
    assert "storage.write_json" in src, "必须走统一持久化层(原子写)"
    assert downloader._persist_lock is not downloader._tasks_lock, \
        "写盘锁应独立于任务锁(快照在任务锁内,写盘不阻塞任务更新)"


def test_load_persisted_backs_up_corrupt_not_silent_wipe(monkeypatch, tmp_path):
    """tasks.json 损坏:改名备份 .corrupt-* 并记日志,绝不静默当空历史
    (旧行为:解析失败直接 return,下次写盘把整份下载史覆盖清零)。"""
    f = tmp_path / "tasks.json"
    f.write_text('{"broken": [', encoding="utf-8")
    monkeypatch.setattr(downloader, "TASKS_FILE", f)
    saved = dict(downloader._tasks)
    downloader._tasks.clear()
    try:
        downloader._load_persisted()
        assert not downloader._tasks, "损坏文件不应加载出任务"
        backups = list(tmp_path.glob("tasks.json.corrupt-*"))
        assert backups, "损坏的 tasks.json 应留备份供手工找回"
        assert not f.exists(), "原路径损坏文件应移走(下次写盘全新开始)"
    finally:
        downloader._tasks.clear()
        downloader._tasks.update(saved)
