"""下载文件名与任务 title 对齐(设计 2026-08-19,A.1~A.5)。

落盘命名权在 Python 侧:outtmpl 用清洗后的字面名,不再让 yt-dlp 按
字节截断 %(title).200B;下载完成后把磁盘文件名强制对齐任务 title;
同名冲突拒绝覆盖,交用户在 UI 上处理。
"""
from pathlib import Path

import pytest
import yt_dlp

import downloader
import taskstore
import extras

_FMTS = [{"format_id": "1", "label": "720P", "kind": "av", "tbr": 1, "height": 720}]


def _mk_task(tid, **kw):
    t = {
        "task_id": tid, "status": "downloading", "progress": 0,
        "speed": 0, "eta": 0, "title": "测试视频", "url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "label": "", "page": 1, "error": "", "cancel": False,
    }
    t.update(kw)
    downloader._tasks[tid] = t
    return t


def _reset_dl_config(monkeypatch):
    monkeypatch.setattr(downloader.config, "SORT_BY_UP", False)
    monkeypatch.setattr(downloader.config, "SAVE_COVER", False)
    monkeypatch.setattr(downloader.config, "SAVE_SUBTITLE", False)
    monkeypatch.setattr(downloader.config, "SAVE_DANMAKU", False)
    monkeypatch.setattr(downloader.config, "NOTIFY", False)


def _mk_ydl(files_writer):
    """mock YoutubeDL:download() 时用 opts 模拟 yt-dlp 落盘。"""
    class _YDL:
        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            files_writer(self._opts)
            return "ok"
    return _YDL


# ---------- A.2 sanitize_filename / build_filename ----------

def test_sanitize_filename_replaces_special_chars():
    assert extras.sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_filename_merges_whitespace_and_strips_dots():
    assert extras.sanitize_filename("  标题\t\r\n 多  空白 ..") == "标题 多 空白"
    assert extras.sanitize_filename("...") == "video"
    assert extras.sanitize_filename("") == "video"
    assert extras.sanitize_filename(None) == "video"


def test_sanitize_filename_windows_reserved():
    assert extras.sanitize_filename("CON") == "_CON"
    assert extras.sanitize_filename("con.mp4") == "_con.mp4"
    assert extras.sanitize_filename("普通的标题.mp4") == "普通的标题.mp4"


def test_build_filename_single_and_multi_part():
    assert downloader.build_filename("漫士沉思录 第 12 集") == "漫士沉思录 第 12 集"
    assert downloader.build_filename("漫士沉思录 第 12 集", 3) == "漫士沉思录 第 12 集 - P3"
    assert downloader.build_filename("坏/名字:x", 1) == "坏_名字_x"


# ---------- A.2 _base_opts:Python 侧组装 outtmpl ----------

def test_base_opts_literal_outtmpl(monkeypatch):
    _reset_dl_config(monkeypatch)
    _mk_task("ot1", title="漫士沉思录 第 12 集", uploader="远古野")
    try:
        o = downloader._base_opts("30016+30280", 1, "ot1")
        tmpl = o["outtmpl"]
        assert "漫士沉思录 第 12 集.%(ext)s" in tmpl
        assert ".200B" not in tmpl and "%(title)" not in tmpl, "不再让 yt-dlp 截断标题"
        assert "postprocessor_hooks" in o, "要捕获最终落盘路径"
    finally:
        downloader._tasks.pop("ot1", None)


def test_base_opts_multi_part_and_up_dir(monkeypatch):
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(downloader.config, "SORT_BY_UP", True)
    _mk_task("ot2", title="合集: 第一季", uploader="远古/野", page=2)
    try:
        o = downloader._base_opts("best", 2, "ot2")
        assert o["outtmpl"].endswith("远古_野/合集_ 第一季 - P2.%(ext)s")
    finally:
        downloader._tasks.pop("ot2", None)


def test_base_opts_escapes_percent_in_title(monkeypatch):
    """标题里的 % 是 outtmpl 模板元字符,必须转义成 %%。"""
    _reset_dl_config(monkeypatch)
    _mk_task("ot3", title="100%还原")
    try:
        o = downloader._base_opts("30280", 1, "ot3")
        assert "100%%还原.%(ext)s" in o["outtmpl"]
        assert "100%还原.%(ext)s" not in o["outtmpl"]
    finally:
        downloader._tasks.pop("ot3", None)


def test_base_opts_placeholder_title_falls_back_to_live_template(monkeypatch):
    """无权威 title(占位/缺任务)时退回 live 模板,不让占位符落盘。"""
    _reset_dl_config(monkeypatch)
    _mk_task("ot4", title="(解析中)")
    try:
        o = downloader._base_opts("30280", 1, "ot4")
        assert "%(title)" in o["outtmpl"]
    finally:
        downloader._tasks.pop("ot4", None)


# ---------- A.1 入队即锚定 title ----------

def test_run_download_keeps_anchored_title(monkeypatch, tmp_path):
    """formats 缺失时的二次解析只补空/占位 title,不覆盖已锚定的权威值;
    落盘与改名都用任务 title,UP 中途改标题不再造成「页面 A、磁盘 B」。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(downloader, "parse_video",
                        lambda url: {"title": "UP 改过的最新标题", "uploader": "u",
                                     "uploader_mid": 1, "thumbnail": "", "duration": 1,
                                     "formats": _FMTS, "best_label": "720P"})

    def writer(opts):
        stem = downloader.build_filename(downloader._tasks["an1"]["title"])
        (tmp_path / f"{stem}.mp4").write_bytes(b"x" * 10)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(writer))
    t = _mk_task("an1", title="入队时的标题")
    try:
        downloader._run_download("an1", t["url"], "", [], 1)  # formats 空 → 二次解析
        assert t["title"] == "入队时的标题"
        assert (tmp_path / "入队时的标题.mp4").is_file()
    finally:
        downloader._tasks.pop("an1", None)


def test_progress_hook_backfills_placeholder_title():
    """权威 title 缺失时,下载 finished 回调从 yt-dlp 最终解析兜底一次。"""
    _mk_task("bf1", title="(解析中)")
    try:
        hook = downloader._progress_hook("bf1")
        hook({"status": "finished", "filename": "x.mp4",
              "info_dict": {"title": "真实标题"}})
        t = downloader._tasks["bf1"]
        assert t["title"] == "真实标题"
        assert t["status"] == "merging"
    finally:
        downloader._tasks.pop("bf1", None)


# ---------- A.3 下载完成后强制对齐 ----------

def test_run_download_renames_file_to_task_title(monkeypatch, tmp_path):
    """yt-dlp 落盘名与 title 不一致时,完成后强制改名对齐,副文件一起改。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)

    mangled = "漫士沉思录第12集"  # 模拟 yt-dlp sanitize 后丢了空格

    def writer(opts):
        # postprocessor 钩子捕获的最终路径(主路径)
        downloader._final_paths["al1"] = str(tmp_path / f"{mangled}.mp4")
        (tmp_path / f"{mangled}.mp4").write_bytes(b"x" * 100)
        (tmp_path / f"{mangled}.jpg").write_bytes(b"x" * 10)  # 封面副文件

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(writer))
    t = _mk_task("al1", title="漫士沉思录 第 12 集")
    try:
        downloader._run_download("al1", t["url"], "", _FMTS, 1)
        assert t["status"] == "done"
        assert (tmp_path / "漫士沉思录 第 12 集.mp4").is_file()
        assert (tmp_path / "漫士沉思录 第 12 集.jpg").is_file()
        assert not (tmp_path / f"{mangled}.mp4").exists(), "旧名文件应已改名,不是复制"
        assert t["actual_filename"].endswith("漫士沉思录 第 12 集.mp4")
        assert "rename_conflict" not in t and not t.get("rename_error")
    finally:
        downloader._tasks.pop("al1", None)
        downloader._final_paths.pop("al1", None)


def test_run_download_align_glob_fallback(monkeypatch, tmp_path):
    """pp 钩子没捕获到路径时(兜底),按 stem 前缀在下载目录里找到再对齐。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)

    def writer(opts):
        (tmp_path / "标题 [下载].mp4").write_bytes(b"x" * 10)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(writer))
    t = _mk_task("al2", title="标题")
    try:
        downloader._run_download("al2", t["url"], "", _FMTS, 1)
        assert t["status"] == "done"
        assert (tmp_path / "标题.mp4").is_file()
        assert t["actual_filename"].endswith("标题.mp4")
    finally:
        downloader._tasks.pop("al2", None)


def test_run_download_already_aligned_noop(monkeypatch, tmp_path):
    """落盘名已与 title 一致:幂等,只记录权威路径,不做任何改名。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)

    def writer(opts):
        (tmp_path / "标题.mp4").write_bytes(b"x" * 10)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(writer))
    t = _mk_task("al3", title="标题")
    try:
        downloader._run_download("al3", t["url"], "", _FMTS, 1)
        assert t["status"] == "done"
        assert t["actual_filename"].endswith("标题.mp4")
        assert "rename_conflict" not in t
        assert not t.get("rename_error") and not t.get("rename_skipped")
    finally:
        downloader._tasks.pop("al3", None)


def test_run_download_no_file_records_error(monkeypatch, tmp_path):
    """下载成功但没找到落盘文件:记 rename_error,不影响完成状态。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(lambda opts: None))
    t = _mk_task("al4", title="标题")
    try:
        downloader._run_download("al4", t["url"], "", _FMTS, 1)
        assert t["status"] == "done"
        assert t["rename_error"]
    finally:
        downloader._tasks.pop("al4", None)


# ---------- A.4 同名冲突:拒绝覆盖 ----------

def _conflict_env(monkeypatch, tmp_path):
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)


def test_run_download_conflict_marks_and_keeps_both(monkeypatch, tmp_path):
    """期望名已被不同大小的文件占用:拒绝覆盖,标记冲突等用户处理。"""
    _conflict_env(monkeypatch, tmp_path)
    (tmp_path / "漫士沉思录 第 12 集.mp4").write_bytes(b"existing-different")  # 17B

    mangled = tmp_path / "漫士沉思录第12集.mp4"

    def writer(opts):
        downloader._final_paths["cf1"] = str(mangled)
        mangled.write_bytes(b"x" * 100)  # 100B,大小不同

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(writer))
    t = _mk_task("cf1", title="漫士沉思录 第 12 集")
    try:
        downloader._run_download("cf1", t["url"], "", _FMTS, 1)
        assert t["status"] == "done"
        c = t["rename_conflict"]
        assert c["source"].endswith("漫士沉思录第12集.mp4")
        assert c["expected"].endswith("漫士沉思录 第 12 集.mp4")
        assert mangled.is_file(), "下载的文件保持原名,不被覆盖也不丢弃"
        assert (tmp_path / "漫士沉思录 第 12 集.mp4").read_bytes() == b"existing-different"
    finally:
        downloader._tasks.pop("cf1", None)
        downloader._final_paths.pop("cf1", None)


def test_run_download_duplicate_same_size_skips(monkeypatch, tmp_path):
    """期望名被同大小文件占用:视为重复,跳过改名并标记。"""
    _conflict_env(monkeypatch, tmp_path)
    (tmp_path / "标题.mp4").write_bytes(b"x" * 50)

    mangled = tmp_path / "标题 [1].mp4"

    def writer(opts):
        downloader._final_paths["cf2"] = str(mangled)
        mangled.write_bytes(b"y" * 50)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(writer))
    t = _mk_task("cf2", title="标题")
    try:
        downloader._run_download("cf2", t["url"], "", _FMTS, 1)
        assert t["rename_skipped"] == "duplicate"
        assert "rename_conflict" not in t
        assert mangled.is_file() and (tmp_path / "标题.mp4").is_file()
    finally:
        downloader._tasks.pop("cf2", None)
        downloader._final_paths.pop("cf2", None)


# ---------- A.4 冲突处理路由 ----------

@pytest.fixture()
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_resolve_conflict_rename_with_index(client, monkeypatch, tmp_path):
    """「加 (1) 改名」:清冲突标记,actual_filename 指向新名。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    src = tmp_path / "yt-dlp落盘名.mp4"
    src.write_bytes(b"x" * 10)
    expected = tmp_path / "期望名.mp4"
    expected.write_bytes(b"y" * 99)
    _mk_task("rc1", status="done", title="期望名", actual_filename=str(src),
             rename_conflict={"source": str(src), "expected": str(expected),
                              "existing": str(expected)})
    try:
        r = client.post("/api/task/rc1/resolve-conflict",
                        json={"action": "rename_with_index"}).get_json()
        assert r["ok"]
        assert (tmp_path / "期望名 (1).mp4").is_file()
        assert not src.exists()
        t = downloader._tasks["rc1"]
        assert "rename_conflict" not in t
        assert t["actual_filename"].endswith("期望名 (1).mp4")
    finally:
        downloader._tasks.pop("rc1", None)


def test_resolve_conflict_cancel(client, monkeypatch, tmp_path):
    """「忽略」:只清标记,文件不动。"""
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    src = tmp_path / "源.mp4"
    src.write_bytes(b"x")
    _mk_task("rc2", status="done", title="t",
             rename_conflict={"source": str(src), "expected": "e", "existing": "e"})
    try:
        r = client.post("/api/task/rc2/resolve-conflict",
                        json={"action": "cancel"}).get_json()
        assert r["ok"]
        assert "rename_conflict" not in downloader._tasks["rc2"]
        assert src.is_file()
    finally:
        downloader._tasks.pop("rc2", None)


def test_resolve_conflict_open_existing(client, monkeypatch, tmp_path):
    """「打开现有文件」:返回路径并由路由层交给 reveal_path 定位。"""
    calls = []
    monkeypatch.setattr("app.downloader.reveal_path",
                        lambda p: calls.append(str(p)) or True)
    existing = tmp_path / "占坑的旧文件.mp4"
    existing.write_bytes(b"x")
    _mk_task("rc3", status="done", title="t",
             rename_conflict={"source": "s", "expected": str(existing),
                              "existing": str(existing)})
    try:
        r = client.post("/api/task/rc3/resolve-conflict",
                        json={"action": "open_existing"}).get_json()
        assert r["ok"] and r["path"] == str(existing)
        assert calls == [str(existing)]
    finally:
        downloader._tasks.pop("rc3", None)


def test_resolve_conflict_unknown_action(client, monkeypatch, tmp_path):
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    _mk_task("rc4", status="done", title="t",
             rename_conflict={"source": "s", "expected": "e", "existing": "e"})
    try:
        r = client.post("/api/task/rc4/resolve-conflict",
                        json={"action": "什么鬼"})
        assert r.status_code == 400
    finally:
        downloader._tasks.pop("rc4", None)


# ---------- actual_filename 优先 + 前端提示 ----------

def test_task_files_prefers_actual_filename(monkeypatch, tmp_path):
    """有 actual_filename 时直接命中,不再前缀扫描(根治 Reveal 找不到文件)。"""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    (tmp_path / "同前缀的另一个视频.mp4").write_bytes(b"x")
    p = tmp_path / "权威路径.mp4"
    p.write_bytes(b"x")
    files = downloader._task_files({"title": "同前缀", "actual_filename": str(p)})
    assert files == [p]


def test_frontend_has_conflict_ui():
    base = Path(__file__).resolve().parent.parent
    html = "\n".join(p.read_text(encoding="utf-8") for p in [
        base / "templates" / "index.html",
        base / "static" / "css" / "main.css",
        *sorted((base / "static" / "js").rglob("*.js"))])
    assert "resolveTaskConflict" in html, "前端缺少冲突处理函数"
    assert "名称冲突" in html, "前端缺少冲突提示"
    assert "自动改名失败" in html, "前端缺少改名失败提示"


# ---------- 文件管理器定位:跨平台 ----------

def test_reveal_dispatches_per_platform(monkeypatch, tmp_path):
    """reveal_path 按平台选命令,不再硬编码 macOS 的 `open -R`。"""
    import files
    f = tmp_path / "视频.mp4"
    f.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(files.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))

    monkeypatch.setattr(files.sys, "platform", "darwin")
    files.reveal_path(f)
    assert calls[-1] == ["open", "-R", str(f)]

    monkeypatch.setattr(files.sys, "platform", "win32")
    files.reveal_path(f)
    assert calls[-1] == ["explorer", f"/select,{f}"]

    # Linux 无通用「选中文件」协议 → 退化为打开所在目录
    monkeypatch.setattr(files.sys, "platform", "linux")
    files.reveal_path(f)
    assert calls[-1] == ["xdg-open", str(tmp_path)]


def test_reveal_survives_missing_command(monkeypatch, tmp_path):
    """命令不存在时返回 False 而不是抛 FileNotFoundError。

    回归:check=False 只忽略返回码,命令缺失照样抛;非 macOS 上硬编码
    `open` 会让 /api/task/<id>/reveal 直接 500。
    """
    import files

    def boom(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(files.subprocess, "run", boom)
    monkeypatch.setattr(files.sys, "platform", "linux")
    assert files.open_dir(tmp_path) is False
    assert files.reveal_path(tmp_path / "不存在.mp4") is False


def test_routes_have_no_hardcoded_platform_command():
    """路由层不再直接 subprocess 唤起系统命令,统一走 files 的跨平台封装。"""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "subprocess" not in src, "app.py 不应再直接调 subprocess"
    assert "reveal_path" in src and "open_dir" in src


# ---------- _task_files 匹配收紧(消除误删邻居文件) ----------

def test_task_files_ignores_longer_neighbour_title(monkeypatch, tmp_path):
    """标题是别人前缀的任务,不得匹配上邻居的文件。

    回归:旧实现按未清洗的 title[:30] 做 name.startswith,任务「第01期」会
    把另一个视频「第01期特别版.mp4」也算成自己的文件——配上
    「删除记录 + 文件」就是删别人的东西。
    """
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    mine = tmp_path / "第01期.mp4"
    mine.write_bytes(b"x")
    neighbour = tmp_path / "第01期特别版.mp4"
    neighbour.write_bytes(b"y")

    files = downloader._task_files({"title": "第01期"})
    assert files == [mine], "只应命中自己的文件"
    assert neighbour.is_file()


def test_delete_file_keeps_neighbour_on_disk(monkeypatch, tmp_path):
    """删除记录 + 文件:只删自己的,邻居文件必须还在。"""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    mine = tmp_path / "教程.mp4"
    mine.write_bytes(b"x")
    neighbour = tmp_path / "教程完整版.mp4"
    neighbour.write_bytes(b"y")
    _mk_task("nb1", status="done", title="教程")
    try:
        taskstore.delete_tasks(["nb1"], delete_file=True)
    finally:
        downloader._tasks.pop("nb1", None)
    assert not mine.exists(), "自己的文件应被删除"
    assert neighbour.is_file(), "邻居文件不得被误删"


def test_task_files_matches_sanitized_title(monkeypatch, tmp_path):
    """标题含非法字符时,磁盘上是清洗后的名字,也要能定位到。

    回归:旧实现拿原始 title 做前缀匹配,`坏/名字:x` 这类标题永远匹配不上
    (磁盘上已是 `坏_名字_x`),Reveal 找不到文件、还会被判成「失效记录」。
    """
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    f = tmp_path / "坏_名字_x.mp4"
    f.write_bytes(b"x")
    assert downloader._task_files({"title": "坏/名字:x"}) == [f]


def test_task_files_matches_multipart_suffix(monkeypatch, tmp_path):
    """多 P 任务的期望名带 ` - P{n}`,应精确命中对应分集而不是别的分集。"""
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    p2 = tmp_path / "合集 - P2.mp4"
    p2.write_bytes(b"x")
    p3 = tmp_path / "合集 - P3.mp4"
    p3.write_bytes(b"y")
    assert downloader._task_files({"title": "合集", "page": 3}) == [p3]


def test_task_files_includes_sidecars(monkeypatch, tmp_path):
    """封面/字幕/弹幕等副文件要跟主文件一起算进任务。

    回归:有 actual_filename 时旧实现只返回主文件,「删除记录 + 文件」会留下
    孤儿 .xml/.srt/.jpg,downloaded_bytes 也少算附件体积。
    """
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    main = tmp_path / "视频.mp4"
    main.write_bytes(b"x" * 10)
    (tmp_path / "视频.xml").write_bytes(b"d")          # 弹幕
    (tmp_path / "视频.jpg").write_bytes(b"c")          # 封面
    (tmp_path / "视频.zh-Hans.srt").write_bytes(b"s")  # 字幕
    (tmp_path / "别的视频.mp4").write_bytes(b"z")

    names = {f.name for f in
             downloader._task_files({"title": "视频", "actual_filename": str(main)})}
    assert names == {"视频.mp4", "视频.xml", "视频.jpg", "视频.zh-Hans.srt"}

    # 没有 actual_filename 的老任务走扫描路径,同样要覆盖整族
    names2 = {f.name for f in downloader._task_files({"title": "视频"})}
    assert names2 == {"视频.mp4", "视频.xml", "视频.jpg", "视频.zh-Hans.srt"}
