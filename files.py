"""下载落盘文件管理：安全定位、文件名对齐、manifest 与冲突处理。"""
import copy
import logging
import subprocess
import sys
from glob import escape as glob_escape
from pathlib import Path

import config
import taskstore
from extras import sanitize_filename, build_filename
from taskstore import (_tasks, _tasks_lock, _persist, _final_paths,
                       _final_paths_lock, stem_belongs,
                       resolve_download_path, UnsafeDownloadPathError,
                       task_download_root, is_owned_sidecar,
                       artifact_lifecycle, refresh_task_manifest,
                       manifest_media_path, mark_persistence_pending,
                       mark_persistence_success, mark_persistence_failure,
                       MEDIA_EXTS, legacy_dirs)

log = logging.getLogger("bd.files")


def tmpl_literal(s: str) -> str:
    """把 Python 侧字面段安全嵌入 yt-dlp outtmpl。"""
    return s.replace("%", "%%")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_ino == b.stat().st_ino
    except OSError:
        return False


def _safe_media(path, *, root=None) -> Path | None:
    try:
        candidate = resolve_download_path(path, root=root)
    except UnsafeDownloadPathError:
        return None
    if candidate.is_file() and candidate.suffix.lower() in MEDIA_EXTS:
        return candidate
    return None


def _find_actual_file(task_id: str, expected_stem: str):
    """找 yt-dlp 实际落盘主媒体；新任务始终使用创建时固定的根。"""
    with _tasks_lock:
        task = copy.deepcopy(_tasks.get(task_id))
    try:
        root = task_download_root(task or {})
    except UnsafeDownloadPathError:
        return None

    with _final_paths_lock:
        captured = _final_paths.get(task_id)
    if captured:
        media = _safe_media(captured, root=root)
        if media:
            return media
        with _final_paths_lock:
            if _final_paths.get(task_id) == captured:
                _final_paths.pop(task_id, None)

    # manifest 有确认过的主媒体才直接信任。新任务从创建起就带空 manifest:
    # 若见 manifest 就返回,单流下载(仅音频/单体格式,无 PostProcessor,
    # postprocessor_hooks 永不触发)落盘成功也会被判「无法确认安全落盘媒体」
    # 而误标 failed(2026-11-23 审查)。空 manifest 继续走精确匹配/扫盘兜底。
    if task and "manifest" in task:
        confirmed = manifest_media_path(task)
        if confirmed is not None:
            return confirmed

    exact: list[Path] = []
    belongs: list[Path] = []
    for directory in legacy_dirs(root=root):
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            media = _safe_media(child, root=root)
            if not media:
                continue
            if media.stem == expected_stem:
                exact.append(media)
            elif stem_belongs(media.stem, expected_stem):
                belongs.append(media)
    candidates = exact if exact else belongs
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda path: path.stat().st_size)
    except OSError:
        return None


class SidecarTargetConflict(Exception):
    """目标 stem 下已存在外来同名副文件,整族改名中止。

    比主媒体冲突更隐蔽:主媒体改名成功、副文件被跳过后,manifest 按新 stem
    扫盘会把**外来**副文件认领进本任务(删除时误删别人的文件),自己的旧
    副文件反而成孤儿(2026-11-23 审查)。"""

    def __init__(self, existing: Path) -> None:
        self.existing = existing
        super().__init__(
            f"目标名族中已存在同名副文件:{existing.name}")


def _rename_with_sidecars(source: Path, new_stem: str, *, root=None) -> Path:
    """安全、尽量事务化地改名主媒体及白名单封面/字幕/弹幕副件。

    目标 stem 下若已存在任何外来副文件(同标题旧封面/字幕等),整族改名
    中止并抛 SidecarTargetConflict:调用方按既有冲突流程处理,媒体保留
    原名、manifest 继续跟踪源文件族。此处绝不跳过冲突副件继续改名——
    那会让外来文件被 manifest 认领(误删)、自己的副件成孤儿(2026-11-23)。"""
    source = resolve_download_path(source, must_exist=True, root=root)
    if not source.is_file() or source.suffix.lower() not in MEDIA_EXTS:
        raise FileNotFoundError(source)
    target = resolve_download_path(source.with_name(new_stem + source.suffix), root=root)

    plan: list[tuple[Path, Path]] = []
    try:
        sidecars = sorted(source.parent.glob(glob_escape(source.stem) + ".*"))
    except OSError:
        sidecars = []
    for side in sidecars:
        try:
            safe_side = resolve_download_path(side, must_exist=True, root=root)
        except (UnsafeDownloadPathError, FileNotFoundError):
            continue
        if (not safe_side.is_file()
                or not is_owned_sidecar(source, safe_side)):
            continue
        suffix_chain = safe_side.name[len(source.stem):]
        side_target = resolve_download_path(
            safe_side.with_name(new_stem + suffix_chain), root=root)
        plan.append((safe_side, side_target))
    plan.append((source, target))  # 主文件最后改,减少失败时的可见窗口。

    # 目标 stem 的副文件族预检:任何已存在的 `new_stem.<白名单副件后缀>`
    # 都会在改名后被 manifest 按新 stem 扫盘认领——不管我们自己有没有对应
    # 副件,都必须先中止(主媒体冲突由上层单独处理,走到这里 target 不存在)。
    try:
        target_family = sorted(
            source.parent.glob(glob_escape(new_stem) + ".*"))
    except OSError:
        target_family = []
    for candidate in target_family:
        try:
            safe = resolve_download_path(candidate, root=root)
        except (UnsafeDownloadPathError, FileNotFoundError):
            continue
        if safe.is_file() and is_owned_sidecar(target, safe):
            raise SidecarTargetConflict(safe)

    # 主文件的目标冲突由上层单独处理(记 rename_conflict 并保留原名),
    # 走到这里还冲突说明状态变了,明确失败而不是默默覆盖。
    if target.exists() and not _same_file(source, target):
        raise FileExistsError(target)

    moved: list[tuple[Path, Path]] = []
    try:
        for old, new in plan:
            if old == new or _same_file(old, new):
                continue
            old.rename(new)
            moved.append((old, new))
    except OSError:
        for old, new in reversed(moved):
            try:
                if new.exists() and not old.exists():
                    new.rename(old)
            except OSError:
                log.exception("回滚改名失败 %s -> %s", new, old)
        raise
    return target


def _persist_file_state(task_id: str, context: str) -> bool:
    token = mark_persistence_pending([task_id])
    result = _persist()
    # 测试桩沿用旧约定常返回 None，仅显式 False 表示真实写盘失败。
    if result is False:
        message = f"{context}未能写入 tasks.json"
        mark_persistence_failure(token, message)
        log.error("%s task_id=%s", message, task_id)
        return False
    mark_persistence_success(token)
    return True


def _record_rename_error(task_id: str, message: str) -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task:
            task["rename_error"] = str(message)
    _persist_file_state(task_id, "改名状态")


def align_task_file(task_id: str) -> Path | None:
    """下载成功后对齐文件名并刷新 manifest；失败只写展示状态。"""
    with artifact_lifecycle(task_id):
        try:
            with _tasks_lock:
                task = copy.deepcopy(_tasks.get(task_id))
                title = (task.get("title") or "").strip() if task else ""
                page = int(task.get("page") or 1) if task else 1
            root = task_download_root(task or {})
            if not title or title == "(解析中)":
                return None

            expected_stem = build_filename(title, page)
            actual = _find_actual_file(task_id, expected_stem)
            if actual is None:
                _record_rename_error(task_id, "下载完成但没找到落盘文件")
                return None

            if actual.stem == expected_stem:
                refresh_task_manifest(task_id, actual)
                with _tasks_lock:
                    current = _tasks.get(task_id)
                    if current:
                        current.pop("rename_error", None)
                        current.pop("rename_conflict", None)
                        current.pop("rename_skipped", None)
                _persist_file_state(task_id, "文件清单")
                return actual

            target = resolve_download_path(
                actual.with_name(expected_stem + actual.suffix), root=root)
            if target.exists() and not _same_file(actual, target):
                same_size = actual.stat().st_size == target.stat().st_size
                refresh_task_manifest(task_id, actual)
                with _tasks_lock:
                    current = _tasks.get(task_id)
                    if current:
                        current.pop("rename_error", None)
                        if same_size:
                            current["rename_skipped"] = "duplicate"
                            current.pop("rename_conflict", None)
                        else:
                            current.pop("rename_skipped", None)
                            current["rename_conflict"] = {
                                "source": str(actual),
                                "expected": str(target),
                                "existing": str(target),
                            }
                _persist_file_state(task_id, "文件名冲突状态")
                return actual

            new_path = _rename_with_sidecars(actual, expected_stem, root=root)
            refresh_task_manifest(task_id, new_path)
            with _tasks_lock:
                current = _tasks.get(task_id)
                if current:
                    current.pop("rename_error", None)
                    current.pop("rename_conflict", None)
                    current.pop("rename_skipped", None)
            _persist_file_state(task_id, "文件改名与清单")
            return new_path
        except SidecarTargetConflict as exc:
            # 目标 stem 下有外来副文件:整族保留原名并标记冲突,交用户处理。
            # manifest 仍跟踪源文件族——外来文件不被认领、自己的副件不孤儿。
            refresh_task_manifest(task_id, actual)
            with _tasks_lock:
                current = _tasks.get(task_id)
                if current:
                    current.pop("rename_error", None)
                    current.pop("rename_skipped", None)
                    current["rename_conflict"] = {
                        "source": str(actual),
                        "expected": str(target),
                        "existing": str(exc.existing),
                    }
            _persist_file_state(task_id, "副件名冲突状态")
            return actual
        except (OSError, UnsafeDownloadPathError) as exc:
            _record_rename_error(task_id, str(exc))
            return None
        finally:
            with _final_paths_lock:
                _final_paths.pop(task_id, None)


def resolve_rename_conflict(task_id: str, action: str) -> dict:
    """处理改名冲突；所有冲突路径在 reveal/rename 前重新做边界校验。"""
    with artifact_lifecycle(task_id):
        with _tasks_lock:
            task = copy.deepcopy(_tasks.get(task_id))
        conflict = dict(task.get("rename_conflict") or {}) if task else {}
        if not conflict:
            return {"ok": False, "error": "该任务没有待处理的名称冲突"}

        if action == "cancel":
            with _tasks_lock:
                current = _tasks.get(task_id)
                if current:
                    current.pop("rename_conflict", None)
            _persist_file_state(task_id, "冲突处理状态")
            return {"ok": True}

        if action == "open_existing":
            # 保留 legacy API：这里返回记录中的现有文件路径；真正的系统 reveal
            # 在 reveal_path 中识别任务来源并强制执行下载根边界。这样旧调用方仍
            # 能取得 path，而越界/外链路径不会触发任何系统文件管理器操作。
            existing_raw = conflict.get("existing")
            try:
                existing = Path(existing_raw).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return {"ok": False, "error": "现有文件路径无效"}
            if not existing.is_file():
                return {"ok": False, "error": "现有文件已不存在"}
            return {"ok": True, "path": str(existing)}

        if action == "rename_with_index":
            try:
                root = task_download_root(task or {})
                conflict_source = resolve_download_path(
                    conflict.get("source"), must_exist=True, root=root)
            except (UnsafeDownloadPathError, FileNotFoundError):
                return {"ok": False, "error": "下载的文件已不在安全下载目录内"}

            if task and "manifest" in task:
                source = manifest_media_path(task)
                if source is None or source != conflict_source:
                    return {"ok": False, "error": "产物清单与冲突源不一致，拒绝改名"}
            else:
                source = conflict_source
            if not source.is_file():
                return {"ok": False, "error": "下载的文件已不在磁盘上,无法改名"}

            title = (task.get("title") or "").strip()
            base = build_filename(title, int(task.get("page") or 1))
            for index in range(1, 10):
                try:
                    candidate = resolve_download_path(
                        source.with_name(f"{base} ({index}){source.suffix}"),
                        root=root)
                except UnsafeDownloadPathError:
                    return {"ok": False, "error": "改名目标越出下载目录"}
                if candidate.exists():
                    continue
                try:
                    new_path = _rename_with_sidecars(
                        source, f"{base} ({index})", root=root)
                except SidecarTargetConflict:
                    continue  # (N) 号族也有外来副文件:试下一个序号
                except (OSError, UnsafeDownloadPathError) as exc:
                    return {"ok": False, "error": f"改名失败:{exc}"}
                refresh_task_manifest(task_id, new_path)
                with _tasks_lock:
                    current = _tasks.get(task_id)
                    if current:
                        current.pop("rename_conflict", None)
                        current.pop("rename_error", None)
                        current["actual_filename"] = str(new_path)
                _persist_file_state(task_id, "冲突改名与清单")
                return {"ok": True, "actual_filename": str(new_path)}
            return {"ok": False, "error": "后缀已试到 (9) 仍冲突,请手动改名"}

        return {"ok": False, "error": f"未知操作:{action}"}


# ---------- 在系统文件管理器中打开/定位 ----------

def _run_quiet(argv: list[str]) -> bool:
    try:
        subprocess.run(argv, check=False)
        return True
    except OSError as exc:
        log.warning("唤起文件管理器失败(%s):%s", " ".join(argv[:1]), exc)
        return False


def _managed_task_for_path(path: Path) -> dict | None:
    """返回声明该路径的任务；匹配用固定根，实际 reveal 仍要求当前根一致。"""
    with _tasks_lock:
        tasks = copy.deepcopy(list(_tasks.values()))
    for task in tasks:
        try:
            root = (Path(task["artifact_root"]).expanduser().resolve(strict=False)
                    if task.get("artifact_root")
                    else Path(config.DOWNLOAD_DIR).expanduser().resolve(strict=False))
            wanted = path.expanduser()
            wanted = (wanted if wanted.is_absolute() else root / wanted).resolve(
                strict=False)
        except (OSError, RuntimeError, ValueError):
            return task  # 无法解析的任务输入按不安全处理。

        raw_paths = []
        if task.get("actual_filename"):
            raw_paths.append(task["actual_filename"])
        conflict = task.get("rename_conflict")
        if isinstance(conflict, dict):
            raw_paths.extend(conflict.get(key) for key in ("source", "expected", "existing"))
        manifest = task.get("manifest")
        if isinstance(manifest, dict):
            raw_paths.extend(item.get("relpath") for item in manifest.get("artifacts", [])
                             if isinstance(item, dict))
        for raw in raw_paths:
            if not raw:
                continue
            try:
                candidate = Path(raw).expanduser()
                candidate = candidate if candidate.is_absolute() else root / candidate
                if candidate.resolve(strict=False) == wanted:
                    return task
            except (OSError, RuntimeError, ValueError):
                continue
    return None


def reveal_path(path) -> bool:
    """在文件管理器定位路径；任务来源路径必须通过其固定根和当前根双重校验。"""
    candidate = Path(path)
    task = _managed_task_for_path(candidate)
    if task is not None:
        try:
            root = task_download_root(task)
            candidate = resolve_download_path(candidate, root=root)
        except UnsafeDownloadPathError:
            log.error("拒绝在文件管理器中显示越界或已重绑定的任务路径: %s", path)
            return False
    if sys.platform == "darwin":
        return _run_quiet(["open", "-R", str(candidate)])
    if sys.platform == "win32":
        return _run_quiet(["explorer", f"/select,{candidate}"])
    return open_dir(candidate.parent if candidate.is_file() else candidate)


def open_dir(path) -> bool:
    candidate = str(path)
    if sys.platform == "darwin":
        return _run_quiet(["open", candidate])
    if sys.platform == "win32":
        return _run_quiet(["explorer", candidate])
    return _run_quiet(["xdg-open", candidate])
