"""下载落盘文件管理:期望文件名组装、下载后文件名对齐、改名冲突处理、文件定位。

命名权在 Python 侧(设计 A.2/A.3):outtmpl 组装与下载后改名共用 build_filename,
保证两端算出的期望名恒一致;落盘后再把磁盘文件强制对齐任务权威 title。
"""
from pathlib import Path
from glob import escape as glob_escape

from extras import sanitize_filename
import config
from taskstore import _tasks, _tasks_lock, _persist, _final_paths


# 主文件扩展名(用于从候选中挑出视频/音频本体,排除封面/字幕等副文件)
_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
               ".m4a", ".mp3", ".aac", ".opus", ".ogg"}


def build_filename(title: str, page: int = 1) -> str:
    """任务权威 title → 期望的落盘文件名 stem(不含目录/扩展名)。

    outtmpl 组装(A.2)与下载后改名对齐(A.3)共用本函数,保证两端算出的
    期望名恒一致。多 P 加 ` - P{n}` 后缀。
    """
    stem = sanitize_filename(title)
    return f"{stem} - P{int(page)}" if page and int(page) > 1 else stem


def tmpl_literal(s: str) -> str:
    """把 Python 侧清洗好的字面段安全嵌入 yt-dlp outtmpl:% 是模板元字符,须转义成 %%。"""
    return s.replace("%", "%%")


def _same_file(a: Path, b: Path) -> bool:
    """两个路径是否同一文件(大小写不敏感盘上仅大小写不同的名字也算)。"""
    try:
        return a.stat().st_ino == b.stat().st_ino
    except OSError:
        return False


def _find_actual_file(task_id: str, expected_stem: str):
    """找 yt-dlp 实际落盘的主文件。

    优先 postprocessor 钩子捕获的最终路径(权威);没捕获到(旧流程/异常)
    再扫下载目录:stem 精确匹配 → 前缀匹配。返回 Path 或 None。
    """
    captured = _final_paths.get(task_id)
    if captured and Path(captured).is_file():
        return Path(captured)
    try:
        dirs = [config.DOWNLOAD_DIR] + [d for d in config.DOWNLOAD_DIR.iterdir() if d.is_dir()]
    except OSError:
        return None
    for d in dirs:
        try:
            files = [f for f in d.iterdir() if f.is_file()]
        except OSError:
            continue
        media = [f for f in files if f.suffix.lower() in _MEDIA_EXTS]
        exact = [f for f in media if f.stem == expected_stem]
        if exact:
            return max(exact, key=lambda f: f.stat().st_size)
        # 兜底:yt-dlp 对字面段做了意外变换时按前缀找(截短些避免长标题误配)
        prefix = [f for f in media if f.stem.startswith(expected_stem[:10])]
        if prefix:
            return max(prefix, key=lambda f: f.stat().st_size)
    return None


def _rename_with_sidecars(source: Path, new_stem: str) -> Path:
    """把 source 改名为 new_stem(保留各自扩展名),同 stem 的封面/字幕等
    副文件一并改名。返回主文件新路径;目标被占等异常向上抛。"""
    target = source.with_name(new_stem + source.suffix)
    for side in source.parent.glob(glob_escape(source.stem) + ".*"):
        if side in (source, target) or not side.is_file():
            continue
        side_target = side.with_name(new_stem + side.suffix)
        if not side_target.exists():
            side.rename(side_target)
    source.rename(target)
    return target


def align_task_file(task_id: str) -> None:
    """下载成功后把磁盘文件名强制对齐任务 title(设计 A.3/A.4)。

    尽力而为:任何失败只写 rename_error/rename_conflict 等展示字段,
    绝不影响任务的「完成」状态。副文件(封面/字幕)随主文件一起改。
    """
    try:
        with _tasks_lock:
            t0 = _tasks.get(task_id)
            title = (t0.get("title") or "").strip() if t0 else ""
            page = int(t0.get("page") or 1) if t0 else 1
        if not title or title == "(解析中)":
            return
        expected_stem = build_filename(title, page)
        actual = _find_actual_file(task_id, expected_stem)
        if actual is None:
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["rename_error"] = "下载完成但没找到落盘文件"
            _persist()
            return
        if actual.stem == expected_stem:
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["actual_filename"] = str(actual)
            _persist()
            return
        target = actual.with_name(expected_stem + actual.suffix)
        if target.exists() and not _same_file(actual, target):
            # A.4:拒绝覆盖,由用户在 UI 上选(加后缀/打开现有/忽略)
            same_size = actual.stat().st_size == target.stat().st_size
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["actual_filename"] = str(actual)
                    if same_size:
                        t["rename_skipped"] = "duplicate"
                    else:
                        t["rename_conflict"] = {"source": str(actual),
                                                "expected": str(target),
                                                "existing": str(target)}
            _persist()
            return
        new_path = _rename_with_sidecars(actual, expected_stem)
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t["actual_filename"] = str(new_path)
        _persist()
    except OSError as e:
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t["rename_error"] = str(e)
        _persist()
    finally:
        _final_paths.pop(task_id, None)


def resolve_rename_conflict(task_id: str, action: str) -> dict:
    """用户处理改名冲突(设计 A.4)。action:
    - rename_with_index: 期望名 stem 后加 ` (1)`(递增到 (9) 封顶)改名
    - cancel: 放弃处理,清掉冲突标记
    - open_existing: 返回现有文件路径,由路由层在 Finder 定位
    """
    with _tasks_lock:
        t = _tasks.get(task_id)
        conflict = dict(t.get("rename_conflict") or {}) if t else {}
    if not conflict:
        return {"ok": False, "error": "该任务没有待处理的名称冲突"}

    if action == "cancel":
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t.pop("rename_conflict", None)
        _persist()
        return {"ok": True}

    if action == "open_existing":
        return {"ok": True, "path": conflict.get("existing", "")}

    if action == "rename_with_index":
        source = Path(conflict.get("source") or "")
        if not source.is_file():
            return {"ok": False, "error": "下载的文件已不在磁盘上,无法改名"}
        title = (t.get("title") or "").strip()
        base = build_filename(title, int(t.get("page") or 1))
        for i in range(1, 10):
            cand = source.with_name(f"{base} ({i}){source.suffix}")
            if cand.exists():
                continue
            try:
                new_path = _rename_with_sidecars(source, f"{base} ({i})")
            except OSError as e:
                return {"ok": False, "error": f"改名失败:{e}"}
            with _tasks_lock:
                t2 = _tasks.get(task_id)
                if t2:
                    t2.pop("rename_conflict", None)
                    t2["actual_filename"] = str(new_path)
            _persist()
            return {"ok": True, "actual_filename": str(new_path)}
        return {"ok": False, "error": "后缀已试到 (9) 仍冲突,请手动改名"}

    return {"ok": False, "error": f"未知操作:{action}"}
