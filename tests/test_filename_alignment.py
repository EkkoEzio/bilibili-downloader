"""下载文件名与任务 title 对齐(设计 2026-08-19,A.1~A.5)。

落盘命名权在 Python 侧:outtmpl 用清洗后的字面名,不再让 yt-dlp 按
字节截断 %(title).200B;下载完成后把磁盘文件名强制对齐任务 title;
同名冲突拒绝覆盖,交用户在 UI 上处理。
"""
from pathlib import Path

import pytest
import yt_dlp

import downloader
import files
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
    """pp 钩子没捕获到路径时(兜底),按 stem 归属规则(精确 / 分隔符后缀)在下载目录里找到再对齐。"""
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


# ---------- B2 扫盘兜底:共享前缀不等于归属 ----------
# Property 2(Bug Condition):兜底匹配与 stem 归属规则等价。
#   isBugCondition_B2(期望名, 磁盘 stem) = pp 钩子未捕获权威路径
#     且 磁盘 stem ≠ 期望名 且 磁盘 stem.startswith(期望名[:10])
#
# 缺陷现场:files._find_actual_file 的兜底分支写成
#   prefix = [f for f in media if f.stem.startswith(expected_stem[:10])]
# 「共享前 10 个字符」被当成了归属判据;期望名短于 10 字时切片等于整名,退化为
# 无边界的整名前缀匹配(「测试」命中「测试2」)。
#
# 真实后果不是"找错了没关系":命中后 align_task_file 会把那个**另一个视频**的
# 文件连同它同 stem 的封面 / 字幕 / 弹幕副文件一起改名成本任务的期望名。邻居视频
# 的原文件名永久丢失(改名不是复制,磁盘上再也找不回来),两条下载记录同时指向
# 同一个文件。这是不可恢复的数据损坏。
#
# 归属规则的唯一来源是 taskstore 里的 stem_belongs(精确相等,或期望名后紧跟
# _STEM_SUFFIX_STARTS 里的分隔符)。下面引用的是它当前的名字 _stem_belongs:
# 提升为公开名时会保留 `_stem_belongs = stem_belongs` 别名,两个名字指向同一个
# 函数对象,所以这处引用在修复前后都成立、且始终是同一份规则。公开名本身的存在性
# 由改名任务自己的 test_stem_belongs_public_alias 锁,不在本批测试的职责范围。

def _clear_captured(task_id: str):
    """清掉 pp 钩子捕获的权威路径。

    isBugCondition_B2 的前提之一就是"没捕获到权威路径",必须让
    _find_actual_file 真的走到扫盘兜底那一级;_final_paths 是从 taskstore
    import 过来的同一个 dict,跨用例会留残留。
    """
    files._final_paths.pop(task_id, None)


def test_find_actual_file_rejects_prefix_neighbour(monkeypatch, tmp_path):
    """期望名是邻居视频文件名的前缀时,不得把邻居的文件认成自己的。(需求 2.4)

    isBugCondition_B2 成立:未捕获权威路径、磁盘 stem「第01期特别版」≠ 期望名
    「第01期」、二者共享前 10 个字符(期望名不足 10 字,切片即整名)。

    旧兜底会返回邻居文件的 Path,align_task_file 随即把它改名成「第01期.mp4」,
    邻居视频的原文件名永久丢失。期望行为:不认、不改名,返回 None。
    """
    monkeypatch.setattr(files.config, "DOWNLOAD_DIR", tmp_path)
    neighbour = tmp_path / "第01期特别版.mp4"
    neighbour.write_bytes(b"y" * 20)
    _clear_captured("b2p")
    try:
        got = files._find_actual_file("b2p", "第01期")
    finally:
        _clear_captured("b2p")
    assert got is None, f"不属于本任务的邻居文件被认作落盘文件:{got}"
    assert neighbour.is_file(), "邻居视频的文件必须原样留在磁盘上"
    assert [p.name for p in tmp_path.iterdir()] == ["第01期特别版.mp4"], \
        "邻居文件名必须逐字未变"


def test_find_actual_file_rejects_short_name_prefix(monkeypatch, tmp_path):
    """期望名短于 10 字时,旧兜底退化为整名无边界前缀匹配。(需求 2.5)

    expected_stem[:10] 对「测试」等于整名,「测试2」startswith 成立 → 另一个
    视频的文件被认作本任务的落盘文件并被改名。期望行为:判定为不属于本任务。
    """
    monkeypatch.setattr(files.config, "DOWNLOAD_DIR", tmp_path)
    other = tmp_path / "测试2.mp4"
    other.write_bytes(b"z" * 20)
    _clear_captured("b2s")
    try:
        got = files._find_actual_file("b2s", "测试")
    finally:
        _clear_captured("b2s")
    assert got is None, f"「测试2」不属于期望名「测试」,却被认作落盘文件:{got}"
    assert other.is_file() and other.name == "测试2.mp4", "邻居文件名必须逐字未变"


# Property 2 的等价类遍历(替代 PBT:输入域被这四类穷尽,项目测试栈无 PBT 依赖)。
# ② 逐个遍历 _STEM_SUFFIX_STARTS 本身,将来往唯一来源里加分隔符会自动纳入覆盖。
_BELONG_CASES = (
    # ① 精确相等 —— 走的是 _find_actual_file 的第一级精确匹配,未修复代码本就正确
    [("第01期", "第01期"),
     ("第01期开学第一课啦", "第01期开学第一课啦")]
    # ② 期望名 + 合法分隔符后缀 —— 分 P 的 ` - P3`、字幕的 `.zh-Hans`、
    #    改名冲突的 ` (1)`、yt-dlp 追加的 ` [下载]` 都是这一类(3.9 必须保持命中)
    + [("标题", f"标题{sep}后缀") for sep in taskstore._STEM_SUFFIX_STARTS]
    + [("标题", "标题 - P3"), ("标题", "标题.zh-Hans"), ("标题", "标题 (1)")]
    # ③ 缺陷现场:共享前缀但不归属
    #    前两条是"期望名后紧跟非分隔符字符";第三条是 [:10] 截断特有的形态 ——
    #    期望名 11 字,磁盘 stem 只共享前 10 字、第 11 字起就是另一个视频的名字
    + [("第01期", "第01期特别版"),
       ("测试", "测试2"),
       ("第01期开学第一课啦啦", "第01期开学第一课啦下集特别版")]
    # ④ 完全无关 —— 两侧都不命中,修复前后一致
    + [("第01期", "另一个视频"),
       ("标题", "毫不相干的名字")]
)


@pytest.mark.parametrize("expected, disk_stem", _BELONG_CASES)
def test_find_actual_file_matches_stem_belongs_rule(monkeypatch, tmp_path,
                                                   expected, disk_stem):
    """Property 2:扫盘兜底的命中结果与 stem 归属规则逐一等价。

    (matched = diskStem) ⟺ stem_belongs(diskStem, expectedStem)

    归属规则只有一份(taskstore),files 侧不得再写第二份判据 —— 旧的
    expected_stem[:10] 前缀匹配就是那第二份,它比唯一来源宽松得多,把邻居
    视频的文件连同副文件一起改名成本任务的期望名(原名永久丢失)。
    """
    monkeypatch.setattr(files.config, "DOWNLOAD_DIR", tmp_path)
    f = tmp_path / f"{disk_stem}.mp4"
    f.write_bytes(b"x" * 10)
    _clear_captured("b2eq")
    try:
        got = files._find_actual_file("b2eq", expected)
    finally:
        _clear_captured("b2eq")
    # 无命中时 _find_actual_file 返回 None、stem_belongs 返回 False → 做真值等价
    want = taskstore._stem_belongs(disk_stem, expected)
    assert (got is not None) is bool(want), (
        f"期望名 {expected!r} 对磁盘 stem {disk_stem!r}:兜底定位"
        f"{'命中 ' + str(got) if got is not None else '未命中'},"
        f"而归属规则 stem_belongs = {want}"
    )
    if want:
        assert got == f
    assert f.is_file(), "定位本身不得改动磁盘上的文件"


def test_stem_belongs_public_alias():
    """归属规则提升为公开名后,旧名仍指向同一个函数对象。(需求 2.4、2.5)

    `_stem_belongs = stem_belongs` 这行别名是向后兼容契约(与同文件的
    `_ACTIVE = ACTIVE` 同一处理方式):历史注释、CHANGELOG 与既有测试里对旧名的
    引用不会断裂。断言 `is` 而不是行为相同 —— 必须是同一份规则,不许出现第二份
    实现各自演进(B2 的根因正是"同一条规则的第二份实现漏改")。
    """
    assert taskstore.stem_belongs is taskstore._stem_belongs, \
        "旧名 _stem_belongs 必须是公开名 stem_belongs 的别名,而非另一份实现"


def test_find_actual_file_shares_stem_rule():
    """files 侧不许再有第二份归属判据:规则从 taskstore import,不在本地重写。

    单一来源纪律的静态守卫。B2 的根因不是判据写错了,而是**同一条规则存在第二份
    实现**:v2.1 已在 taskstore 侧收紧为「期望名 + 分隔符边界」,files 侧那份却
    留着「期望名前 10 个字符」的无边界前缀匹配,而且更宽松。行为断言只能覆盖被
    想到的输入;这条断言覆盖的是「有没有第二份规则」本身 —— 谁把切片前缀兜底加
    回来(哪怕只是"临时放宽一下"),这里立刻红。

    因此 files.py 源码里连出现过那个切片写法的注释都不留:守卫做成朴素的子串
    断言,不去区分代码与注释,才不会被自己的措辞绕过。旧写法在注释里用中文散文
    描述(「拿期望名的前 10 个字符做无边界前缀匹配」),失败模式记录不丢。
    """
    src = (Path(__file__).resolve().parent.parent / "files.py").read_text(encoding="utf-8")
    assert "[:10]" not in src, \
        "files.py 不得再出现切片前缀兜底(哪怕在注释里),归属判定只有 stem_belongs 一份"
    import_lines = [ln for ln in src.splitlines()
                    if "taskstore" in ln or "stem_belongs" in ln]
    assert any("from taskstore import" in ln for ln in import_lines), \
        "files.py 应从 taskstore import 归属规则,而不是自己实现"
    assert any("stem_belongs" in ln for ln in import_lines), \
        "files.py 的 taskstore import 里必须包含 stem_belongs"
    assert "def stem_belongs" not in src and "def _stem_belongs" not in src, \
        "归属规则不得在 files.py 里重新定义 —— 唯一来源在 taskstore"


def test_align_records_rename_error_when_no_belonging_file(monkeypatch, tmp_path):
    """盘上只有邻居视频的整族文件时,整条下载链路退化为可见的 rename_error。

    走完整 _run_download → align_task_file,锁两件事:

    1. **邻居视频的文件一个字都不许改**,主文件与它的 `.jpg` / `.srt` / `.xml`
       副文件都是。旧兜底命中邻居主文件后,_rename_with_sidecars 会把整族一起
       改名成本任务的期望名,原名永久丢失、两条记录指向同一个文件。
    2. **退化为 rename_error 不影响完成状态**(这是设计取舍,必须锁住):找不到
       属于本任务的落盘文件时只写展示字段,任务照样 done。用"少数异常落盘形态
       变成可见、可恢复的 rename_error"换掉"静默误改邻居文件的不可逆损坏"。
    """
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(downloader.config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(downloader, "_ydl_opts", lambda extra=None: dict(extra or {}))
    _reset_dl_config(monkeypatch)

    # 另一个视频(另一条记录)的整族文件,本任务开始下载前就在磁盘上
    neighbour = {ext: tmp_path / f"第01期特别版{ext}"
                 for ext in (".mp4", ".jpg", ".srt", ".xml")}
    for p in neighbour.values():
        p.write_bytes(b"n" * 30)

    # 落盘路径没被 pp 钩子捕获(→ 走扫盘兜底),且盘上没有属于本任务的文件
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(lambda opts: None))
    t = _mk_task("b2al", title="第01期")
    try:
        downloader._run_download("b2al", t["url"], "", _FMTS, 1)
        assert t["status"] == "done", "改名对齐是尽力而为,失败不得影响完成状态"
        assert t.get("rename_error"), \
            "没有属于本任务的落盘文件时应写可见的 rename_error"
        for ext, p in neighbour.items():
            assert p.is_file(), f"邻居视频的 {ext} 文件必须原样留在磁盘上"
        on_disk = sorted(p.name for p in tmp_path.iterdir() if p.suffix != ".json")
        assert on_disk == sorted(p.name for p in neighbour.values()), \
            "邻居整族文件名必须逐字未变,且不得凭空出现期望名的文件"
    finally:
        downloader._tasks.pop("b2al", None)
        downloader._final_paths.pop("b2al", None)


# ---------- B2 保持(Property 8):权威路径优先,不扫盘 ----------
# 需求 3.7 的既有覆盖只到「捕获的路径被用上了」这一层:
# test_run_download_renames_file_to_task_title 的落盘名(`漫士沉思录第12集`)在新旧
# 两套规则下都扫不到,所以「先扫盘、扫不到再用捕获路径」这种把优先级写反的实现,
# 在那条用例里照样能过 —— 它区分不出「优先」与「兜底」。
#
# 下面这条用一个**会被扫盘命中、而且体积更大**的邻居文件把两者拉开:
#   - 期望名 `合集 第一季`;
#   - pp 钩子捕获的是 `合集第一季.mp4`(yt-dlp 把空格吞了),100B,
#     两套规则都扫不到它(缺空格 → 既非精确相等,也不是「期望名 + 分隔符后缀」);
#   - 盘上另有 `合集 第一季 - P3.mp4`,5000B —— 那是**同一合集另一个分 P 的任务**
#     的文件,余部 ` - P3` 以空格开头,stem_belongs 命中,旧的 [:10] 前缀也命中,
#     且体积更大 → 任何扫盘分支的 max(size) 都会挑中它。
#
# 于是「先扫盘」的实现必然把 P3 的文件改名成 `合集 第一季.mp4`(P3 的原名永久丢失,
# 两条记录指向同一个文件),而「捕获优先」的实现只动捕获到的那一个。断言落在
# **最终谁背着期望名、内容是谁的字节**上,与实现细节无关。

def test_run_download_prefers_captured_path_over_bigger_disk_candidate(
        monkeypatch, tmp_path):
    """pp 钩子已捕获权威路径且文件存在 → 用它,不扫盘。(需求 3.7)

    isBugCondition_B2 不成立(权威路径已捕获),故本条锁的是既有正确行为:
    捕获路径的优先级高于扫盘,哪怕盘上有个体积更大、且按归属规则命中的候选。
    """
    _conflict_env(monkeypatch, tmp_path)

    # 同一合集另一个分 P 的落盘文件:按 stem 归属规则命中期望名,且体积更大
    sibling = tmp_path / "合集 第一季 - P3.mp4"
    sibling.write_bytes(b"p" * 5000)
    captured = tmp_path / "合集第一季.mp4"  # yt-dlp 吞掉了空格

    def writer(opts):
        downloader._final_paths["cap1"] = str(captured)
        captured.write_bytes(b"c" * 100)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mk_ydl(writer))
    t = _mk_task("cap1", title="合集 第一季")
    try:
        downloader._run_download("cap1", t["url"], "", _FMTS, 1)
        aligned = tmp_path / "合集 第一季.mp4"
        assert t["status"] == "done"
        assert aligned.is_file(), "捕获到的文件应被改名对齐到期望名"
        assert aligned.read_bytes() == b"c" * 100, \
            "背着期望名的必须是 pp 钩子捕获的那个文件,而不是扫盘挑出的体积更大者"
        assert not captured.exists(), "是改名而不是复制"
        assert t["actual_filename"] == str(aligned)
        assert "rename_conflict" not in t and not t.get("rename_error")
        # 别的分 P 的文件一个字节、一个字都不许动
        assert sibling.is_file() and sibling.read_bytes() == b"p" * 5000, \
            "扫盘一旦抢在捕获路径之前,这个更大的候选就会被改名成本任务的期望名"
    finally:
        downloader._tasks.pop("cap1", None)
        downloader._final_paths.pop("cap1", None)

# ---------- 副文件改名:多段扩展名(字幕语言标签)----------
# 缺陷现场:files._rename_with_sidecars 给副文件算新名时用的是 `side.suffix`,
# 而 pathlib 的 .suffix 只取**最后一段**扩展名。字幕由 yt-dlp 按
# subtitleslangs=["zh-Hans", "zh-CN"] 落盘,名字形如 `<stem>.zh-Hans.srt`
# ——两段扩展名。对 Path("视频.zh-Hans.srt"),.suffix 是 ".srt",`.zh-Hans`
# 被算进了 stem。于是有两个后果:
#
# (a) 语言标签丢失:`旧名.zh-Hans.srt` → `新名.srt`。
#
# (b) 第二种语言变孤儿(更严重,不可自愈):两种语言都存在时,两个 side 算出
#     **同一个** side_target = `新名.srt`。第一个改名成功,第二个撞上
#     `if not side_target.exists()` 被跳过,永久留在旧 stem 下。而
#     taskstore._file_family 是按**新** stem glob(`新名.*`)找整族的,再也
#     匹配不到那个孤儿。后果链:「删除记录 + 文件」清不掉它(磁盘永久残留,
#     v2.1 专门修过一次「删除后残留孤儿 .xml/.srt/.jpg」,同类问题从改名路径
#     重新长出来),downloaded_bytes / 体积统计也少算这个文件。
#
# 修法:副文件取「从 source.stem 之后的整条扩展名链」而不是最后一段:
#     side.name[len(source.stem):]
# 切片的正确性依赖 glob 模式 —— 模式是 glob_escape(source.stem) + ".*",
# 所以每个 side.name 必然以 source.stem 开头且紧跟一个 ".",切片结果就是完整
# 的 `.xxx[.yyy]` 链。改这行时必须连 glob 模式一起看。
#
# 主文件那行 `target = source.with_name(new_stem + source.suffix)` **刻意**仍用
# .suffix:落盘名若是 `视频.f137.mp4` 这类 yt-dlp 中间产物形态,我们希望改名后
# 得到干净的 `新名.mp4` 而不是 `新名.f137.mp4`。主/副两处取法不同是设计,不是
# 疏漏,不要「统一」掉。

def test_rename_sidecars_keeps_subtitle_language_tag(monkeypatch, tmp_path):
    """字幕副文件的语言标签必须随改名保留。

    `旧名.zh-Hans.srt` 应改成 `新名.zh-Hans.srt`。未修复时 .suffix 只取
    `.srt`,`.zh-Hans` 被当成 stem 的一部分丢掉 → 落成 `新名.srt`,播放器
    再也认不出这是哪种语言的字幕轨。
    """
    monkeypatch.setattr(files.config, "DOWNLOAD_DIR", tmp_path)
    main = tmp_path / "旧名.mp4"
    main.write_bytes(b"m" * 10)
    (tmp_path / "旧名.zh-Hans.srt").write_bytes(b"s")

    new_main = files._rename_with_sidecars(main, "新名")

    assert new_main == tmp_path / "新名.mp4"
    on_disk = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert on_disk == ["新名.mp4", "新名.zh-Hans.srt"], \
        f"字幕语言标签应随改名保留,实际落盘:{on_disk}"


def test_rename_sidecars_no_orphan_with_two_subtitle_langs(monkeypatch, tmp_path):
    """两种语言的字幕都在时,不得有任何一个被留在旧 stem 下变成孤儿。

    未修复时两个 side 算出同一个 side_target(`新名.srt`):先被 glob 到的那个
    改名成功,另一个撞上 `if not side_target.exists()` 被静默跳过,永久留在
    旧 stem 下(`旧名.zh-CN.srt` 或 `旧名.zh-Hans.srt`,取决于 glob 顺序)。

    额外断言 taskstore._file_family(新主文件) 能找齐全部 3 个文件 —— 直接锁住
    真实后果:_file_family 按**新** stem glob,匹配不到孤儿,「删除记录 + 文件」
    清不掉它(磁盘永久残留),体积统计也少算它。
    """
    monkeypatch.setattr(files.config, "DOWNLOAD_DIR", tmp_path)
    main = tmp_path / "旧名.mp4"
    main.write_bytes(b"m" * 10)
    (tmp_path / "旧名.zh-Hans.srt").write_bytes(b"h")
    (tmp_path / "旧名.zh-CN.srt").write_bytes(b"c")

    new_main = files._rename_with_sidecars(main, "新名")

    assert new_main == tmp_path / "新名.mp4"
    on_disk = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert on_disk == ["新名.mp4", "新名.zh-CN.srt", "新名.zh-Hans.srt"], \
        f"两种语言的字幕都应落到新 stem 下,实际落盘:{on_disk}"
    # 旧 stem 下不许留任何东西 —— 孤儿一旦产生就再也追不回来
    assert not list(tmp_path.glob("旧名.*")), \
        f"旧 stem 下残留孤儿文件:{[p.name for p in tmp_path.glob('旧名.*')]}"
    # 真实后果:整族定位(删除 / 体积统计的唯一来源)必须覆盖全部 3 个文件
    fam = sorted(p.name for p in taskstore._file_family(new_main))
    assert fam == ["新名.mp4", "新名.zh-CN.srt", "新名.zh-Hans.srt"], \
        f"_file_family 漏掉了文件(删除清不掉 / 体积少算):{fam}"


def test_rename_sidecars_single_suffix_unchanged(monkeypatch, tmp_path):
    """保持:单段扩展名的副文件行为与修复前逐位相同。

    封面 `.jpg`、弹幕 `.xml`、无语言标签的字幕 `.srt` 都只有一段扩展名,
    `side.name[len(source.stem):]` 与 `side.suffix` 在这类文件上结果完全一致
    (glob 模式保证 side.name 以 source.stem + "." 开头)。这条是基线:未修复
    代码上它本来就绿,修复后必须仍绿 —— 不许为了修多段扩展名把单段搞坏。
    """
    monkeypatch.setattr(files.config, "DOWNLOAD_DIR", tmp_path)
    main = tmp_path / "旧名.mp4"
    main.write_bytes(b"m" * 10)
    (tmp_path / "旧名.jpg").write_bytes(b"c")   # 封面
    (tmp_path / "旧名.xml").write_bytes(b"d")   # 弹幕
    (tmp_path / "旧名.srt").write_bytes(b"s")   # 字幕(无语言标签)

    new_main = files._rename_with_sidecars(main, "新名")

    assert new_main == tmp_path / "新名.mp4"
    on_disk = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert on_disk == ["新名.jpg", "新名.mp4", "新名.srt", "新名.xml"], \
        f"单段扩展名副文件的改名行为不应改变,实际落盘:{on_disk}"
    assert not list(tmp_path.glob("旧名.*")), "旧 stem 下不得残留"
