"""任务状态存储、持久化与下载产物清单。

任务表是全应用共享的可变状态。锁纪律：
- ``_tasks_lock`` 只保护内存任务表；慢 IO 不在锁内执行。
- ``_persist_lock`` 串行化 tasks.json 的快照提交。
- ``_final_paths_lock`` 保护 yt-dlp 后处理器交给文件层的运行时路径。
- 每任务 artifact lock 串行化同一任务的改名、清单刷新与删除。
"""
import copy
import logging
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from glob import escape as glob_escape
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

# 直接导入 taskstore 的脚本/测试也必须先锁住真实任务史；app.py 会更早调用，
# 同一进程重复获取是幂等的。
from instance_lock import acquire_instance_lock

acquire_instance_lock()

import storage
from config import TASKS_FILE
import config
import extras

log = logging.getLogger("bd.taskstore")
bvid_from_url = extras.bvid_from_url

TASK_SCHEMA_VERSION = 2
MANIFEST_VERSION = 1
# 个人本地应用的硬容量边界。达到上限时整批拒绝，不静默裁历史，也不先写盘后
# 让队列 put 失败。终态记录可由用户在下载记录页显式清理。
MAX_PENDING_TASKS = 500
MAX_TOTAL_TASKS = 10_000
MAX_TASKS_FILE_BYTES = 64 * 1024 * 1024
_ARTIFACT_SET_NAMESPACE = uuid.UUID("fd30f3f2-93ee-5e58-a7ad-72c0e0a5b91a")
_ARTIFACT_ID_NAMESPACE = uuid.UUID("a3151d53-7519-553f-973f-9e497b503fb5")
_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
               ".m4a", ".mp3", ".aac", ".opus", ".ogg"}
_SIDECAR_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif",
                 ".srt", ".vtt", ".ass", ".ssa", ".lrc", ".xml"}

# 任务状态表：task_id -> task dict。对象身份是测试和下游模块的兼容契约。
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
_persist_lock = threading.Lock()
# 健康状态代次只存在内存中；tasks.json 中的成功快照永远写成 durable。
_persistence_versions: dict[str, int] = {}
_persisted_versions: dict[str, int] = {}


class TaskStoreWriteError(RuntimeError):
    """任务事实未能可靠写入 tasks.json。"""


class TaskStoreLoadError(RuntimeError):
    """任务历史规模超出安全加载边界；原文件保持不动。"""


class TaskCapacityError(RuntimeError):
    """新任务会突破 pending 或总任务容量。"""

    def __init__(self, capacity: str, limit: int, current: int,
                 requested: int) -> None:
        self.capacity = str(capacity)
        self.limit = int(limit)
        self.current = int(current)
        self.requested = int(requested)
        label = "等待队列" if self.capacity == "pending" else "任务历史"
        super().__init__(
            f"{label}容量不足（当前 {self.current}，新增 {self.requested}，"
            f"上限 {self.limit}）；请等待任务开始或清理历史后重试"
        )


class UnsafeDownloadPathError(ValueError):
    """路径解析后不属于下载根目录，或指向下载根本身。"""


# postprocessor 捕获的最终落盘路径，仅为运行时交接信息。
_final_paths: dict[str, str] = {}
_final_paths_lock = threading.Lock()

_artifact_locks_guard = threading.Lock()
_artifact_locks: dict[str, threading.RLock] = {}
_artifact_lock_refs: dict[str, int] = {}

# 统计页可能被前端频繁轮询；仅缓存昂贵的磁盘 rglob，两秒内任务数仍实时计算。
_DISK_USAGE_CACHE_SECONDS = 2.0
_disk_usage_cache_lock = threading.Lock()
_disk_usage_cache: Optional[tuple[str, float, int]] = None

# 状态集合是全项目唯一来源。
ACTIVE = ("queued", "downloading", "merging", "cancelling", "retrying")
TERMINAL = ("done", "failed", "cancelled", "interrupted")
_ACTIVE = ACTIVE  # 旧名保留，避免下游 import 断裂。


def _download_root() -> Path:
    return Path(config.DOWNLOAD_DIR).expanduser().resolve(strict=False)


def _canonical_root(root) -> Path:
    raw = Path(root).expanduser()
    if not raw.is_absolute():
        raise UnsafeDownloadPathError("下载根必须是绝对路径")
    try:
        resolved = raw.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnsafeDownloadPathError(f"下载根无效: {root}") from exc
    if resolved == Path(resolved.anchor):
        raise UnsafeDownloadPathError("文件系统根目录不能作为下载根")
    return resolved


def _resolve_under_root(path, root: Path, *, must_exist: bool = False) -> Path:
    if path is None or str(path).strip() == "":
        raise UnsafeDownloadPathError("下载产物路径为空")
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnsafeDownloadPathError(f"路径越出下载目录: {path}") from exc
    if resolved == root or not relative.parts:
        raise UnsafeDownloadPathError("下载根目录不能作为文件产物")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def resolve_download_path(path, *, must_exist: bool = False, root=None) -> Path:
    """把绝对路径或下载根相对路径规范为安全绝对路径。

    默认根是 ``config.DOWNLOAD_DIR``。内部产物流程可传入同一次操作捕获的
    规范根，避免配置在两次解析之间变化。``resolve(strict=False)`` 会解析已
    存在的 symlink，因此根内 symlink 指向根外也会被拒绝；根本身也永远非法。
    """
    safe_root = _download_root() if root is None else _canonical_root(root)
    return _resolve_under_root(path, safe_root, must_exist=must_exist)


def download_relative_path(path, *, root=None) -> str:
    """返回规范的下载根相对 POSIX 路径；整次计算只使用一个根快照。"""
    safe_root = _download_root() if root is None else _canonical_root(root)
    return _resolve_under_root(path, safe_root).relative_to(safe_root).as_posix()


def task_download_root(task: dict) -> Path:
    """返回任务固定的产物根，并拒绝把旧清单静默重绑定到新配置根。

    legacy（无 manifest）继续使用当前下载根。有 manifest 的任务必须带创建时
    固定的 ``artifact_root``；若用户之后切换下载目录，文件操作安全失败，切回
    原目录后即可恢复，而不会误认新目录中的同名文件。
    """
    current = _download_root()
    raw = task.get("artifact_root") if isinstance(task, dict) else None
    if not raw:
        if isinstance(task, dict) and "manifest" in task:
            raise UnsafeDownloadPathError("产物清单缺少固定下载根")
        return current
    pinned = _canonical_root(raw)
    if pinned != current:
        raise UnsafeDownloadPathError("任务下载根与当前配置不一致，拒绝重绑定产物")
    return pinned


def artifact_lock(task_id: str):
    """取得同一任务文件生命周期的可重入锁（兼容旧直接调用）。"""
    key = str(task_id)
    with _artifact_locks_guard:
        return _artifact_locks.setdefault(key, threading.RLock())


@contextmanager
def artifact_lifecycle(task_id: str):
    """串行化任务文件操作，并在最后一个使用者离开后回收锁表条目。"""
    key = str(task_id)
    with _artifact_locks_guard:
        lock = _artifact_locks.setdefault(key, threading.RLock())
        _artifact_lock_refs[key] = _artifact_lock_refs.get(key, 0) + 1
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _artifact_locks_guard:
            remaining = _artifact_lock_refs.get(key, 1) - 1
            if remaining <= 0:
                _artifact_lock_refs.pop(key, None)
                if _artifact_locks.get(key) is lock:
                    _artifact_locks.pop(key, None)
            else:
                _artifact_lock_refs[key] = remaining


def _uuid_hex(value) -> Optional[str]:
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return None


def new_artifact_set_id() -> str:
    return uuid.uuid4().hex


def _legacy_artifact_set_id(task_id: str) -> str:
    return uuid.uuid5(_ARTIFACT_SET_NAMESPACE, str(task_id)).hex


def ensure_task_identity(task: dict, *, with_manifest: bool = False) -> dict:
    """补齐 schema/artifact identity；原地修改并返回 task。"""
    task["schema_version"] = TASK_SCHEMA_VERSION
    task["artifact_set_id"] = (
        _uuid_hex(task.get("artifact_set_id"))
        or _legacy_artifact_set_id(str(task.get("task_id") or "missing"))
    )
    if with_manifest:
        task.setdefault("manifest", {"version": MANIFEST_VERSION, "artifacts": []})
        # 此分支只用于新建/首次接管任务；加载旧的非空清单会走显式迁移，
        # 不在这里把历史 relpath 静默绑定到当前配置目录。
        task.setdefault("artifact_root", str(_download_root()))
    task.setdefault("durable", True)
    return task


def artifact_id_for(artifact_set_id: str, role: str, relpath: str) -> str:
    """产物 ID 仅由产物集合、角色和规范相对路径决定，重启后稳定。"""
    key = f"{artifact_set_id}:{role}:{relpath}"
    return uuid.uuid5(_ARTIFACT_ID_NAMESPACE, key).hex


def _normalise_relpath(value) -> Optional[str]:
    """仅接受规范的相对 POSIX 路径；实际访问时再解析 symlink 边界。"""
    text = str(value or "").strip()
    if not text or "\\" in text:
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."}
                                                   for part in path.parts):
        return None
    return path.as_posix()


def _normalise_manifest(task: dict, manifest) -> dict:
    artifact_set_id = ensure_task_identity(task)["artifact_set_id"]
    raw_items = manifest.get("artifacts") if isinstance(manifest, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "")
        if role not in {"media", "sidecar"}:
            continue
        relpath = _normalise_relpath(raw.get("relpath"))
        if relpath is None:
            continue
        key = (role, relpath)
        if key in seen:
            continue
        try:
            size = max(0, int(raw.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        seen.add(key)
        items.append({
            "artifact_id": artifact_id_for(artifact_set_id, role, relpath),
            "role": role,
            "relpath": relpath,
            "size": size,
        })
    items.sort(key=lambda item: (item["role"] != "media", item["relpath"]))
    return {"version": MANIFEST_VERSION, "artifacts": items}


def _snapshot(tasks: Iterable[dict]) -> list[dict]:
    """生成深拷贝提交快照；运行时取消与写盘健康元数据不进入历史。"""
    snapshot = copy.deepcopy(list(tasks))
    for task in snapshot:
        task.pop("cancel", None)
        task.pop("persistence_pending", None)
        task.pop("persistence_error", None)
        # 只有原子写成功后该快照才会出现于磁盘，因此盘上版本必然 durable。
        task["durable"] = True
    return snapshot


def _active_key(t: dict) -> tuple[str, str, int]:
    try:
        page = int(t.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    return str(t.get("url") or ""), str(t.get("label") or ""), page


def _apply_persistence_success(versions: dict[str, int]) -> None:
    with _tasks_lock:
        for task_id, version in versions.items():
            _persisted_versions[task_id] = max(
                version, _persisted_versions.get(task_id, 0))
            task = _tasks.get(task_id)
            if task and _persistence_versions.get(task_id, 0) == version:
                task["durable"] = True
                task.pop("persistence_pending", None)
                task.pop("persistence_error", None)


def _persist() -> bool:
    """串行提交全表，并按快照代次更新所有任务的持久化健康。"""
    with _persist_lock:
        with _tasks_lock:
            snapshot = _snapshot(_tasks.values())
            versions = {
                str(task.get("task_id")): _persistence_versions.get(
                    str(task.get("task_id")), 0)
                for task in snapshot if task.get("task_id")
            }
        result = storage.write_json(TASKS_FILE, snapshot)
        if result is not False:
            _apply_persistence_success(versions)
        return result


def mark_persistence_pending(task_ids: Iterable[str]) -> dict[str, int]:
    """为本次状态提交分配代次；返回供 success/failure CAS 使用的 token。"""
    token: dict[str, int] = {}
    with _tasks_lock:
        for task_id in task_ids:
            key = str(task_id)
            task = _tasks.get(key)
            if not task:
                continue
            version = _persistence_versions.get(key, 0) + 1
            _persistence_versions[key] = version
            token[key] = version
            task["durable"] = False
            task["persistence_pending"] = True
            task.pop("persistence_error", None)
    return token


def mark_persistence_success(token) -> None:
    """完成一次提交；旧 token 不得覆盖同任务更新后的健康状态。"""
    if isinstance(token, dict):
        versions = {str(key): int(value) for key, value in token.items()}
    else:
        with _tasks_lock:
            versions = {str(key): _persistence_versions.get(str(key), 0)
                        for key in token}
    _apply_persistence_success(versions)


def mark_persistence_failure(token, error: str) -> None:
    """仅在该代次尚未被更新的成功全表快照覆盖时暴露失败。"""
    if isinstance(token, dict):
        versions = {str(key): int(value) for key, value in token.items()}
    else:
        with _tasks_lock:
            versions = {str(key): _persistence_versions.get(str(key), 0)
                        for key in token}
    with _tasks_lock:
        for task_id, version in versions.items():
            task = _tasks.get(task_id)
            if (task and _persistence_versions.get(task_id, 0) == version
                    and _persisted_versions.get(task_id, 0) < version):
                task["durable"] = False
                task.pop("persistence_pending", None)
                task["persistence_error"] = str(error)


def request_task_cancellation(task_id: str) -> bool:
    """先持久化取消事实，再向 worker 发布运行时取消标志。

    锁序固定为 ``_persist_lock → _tasks_lock``。写盘期间不持任务锁，因此进度
    与终态仍可推进；若目标在 IO 期间已经终结或被替换，会先用当前全表校正磁盘，
    绝不把旧任务重新拉回 ``cancelling``。任务容器和目标字典均保持原对象身份。
    """
    key = str(task_id)
    with _persist_lock:
        with _tasks_lock:
            target = _tasks.get(key)
            if not target or target.get("status") in TERMINAL:
                return False

            version = _persistence_versions.get(key, 0) + 1
            _persistence_versions[key] = version
            items = list(_tasks.items())
            snapshot = _snapshot(task for _, task in items)
            versions = {
                current_id: _persistence_versions.get(current_id, 0)
                for current_id, _ in items
            }
            for (current_id, _), candidate in zip(items, snapshot):
                if current_id == key:
                    candidate["status"] = "cancelling"
                    break

        result = storage.write_json(TASKS_FILE, snapshot)
        if result is False:
            message = "取消意图未能写入 tasks.json"
            mark_persistence_failure({key: version}, message)
            log.error("%s task_id=%s", message, key)
            raise TaskStoreWriteError(message)

        with _tasks_lock:
            live = _tasks.get(key)
            publish = live is target and live.get("status") in ACTIVE
            if publish:
                # 直到上面的原子写成功，worker 才能观察到这两个字段。
                live["status"] = "cancelling"
                live["cancel"] = True
                correction = None
                correction_versions = None
            else:
                # 候选快照已短暂写入旧 cancelling；在释放持久化锁前以当前
                # 全表覆盖，确保重启后不会复活已终结/已替换的任务。
                correction = _snapshot(_tasks.values())
                correction_versions = {
                    current_id: _persistence_versions.get(current_id, 0)
                    for current_id in _tasks
                }

        if publish:
            _apply_persistence_success(versions)
            return True

        corrected = storage.write_json(TASKS_FILE, correction)
        if corrected is False:
            message = "取消竞态校正未能写入 tasks.json"
            mark_persistence_failure(correction_versions, message)
            log.error("%s task_id=%s", message, key)
            raise TaskStoreWriteError(message)
        _apply_persistence_success(correction_versions)
        return False


def create_tasks_if_absent(candidates: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """原子过滤活跃重复、检查容量并 durable-first 创建任务。

    容量检查与活跃去重共用 ``_persist_lock + _tasks_lock`` 临界区，因此并发批次
    不会同时看见“还有最后几个空位”。任何一项超限都会整批拒绝，磁盘与内存
    均保持原状。
    """
    if not candidates:
        return [], {}
    with _persist_lock:
        with _tasks_lock:
            active = {_active_key(t): tid for tid, t in _tasks.items()
                      if t.get("status") in ACTIVE}
            occupied_ids = set(_tasks)
            accepted: list[dict] = []
            duplicates: dict[str, str] = {}
            for raw in candidates:
                task = copy.deepcopy(raw)
                tid = str(task.get("task_id") or "")
                if not tid:
                    raise ValueError("任务缺少 task_id")
                if tid in occupied_ids:
                    raise ValueError(f"任务 ID 冲突:{tid}")
                key = _active_key(task)
                existing = active.get(key)
                if existing:
                    duplicates[tid] = existing
                    continue
                task.setdefault("cancel", False)
                ensure_task_identity(task, with_manifest=True)
                accepted.append(task)
                occupied_ids.add(tid)
                active[key] = tid
            if not accepted:
                return [], duplicates

            total_current = len(_tasks)
            if total_current + len(accepted) > MAX_TOTAL_TASKS:
                raise TaskCapacityError(
                    "total", MAX_TOTAL_TASKS, total_current, len(accepted))
            pending_current = sum(
                task.get("status") == "queued" for task in _tasks.values())
            pending_requested = sum(
                task.get("status") == "queued" for task in accepted)
            if pending_current + pending_requested > MAX_PENDING_TASKS:
                raise TaskCapacityError(
                    "pending", MAX_PENDING_TASKS, pending_current,
                    pending_requested)

            snapshot = _snapshot([*_tasks.values(), *accepted])
            versions = {task_id: _persistence_versions.get(task_id, 0)
                        for task_id in _tasks}
        if not storage.write_json(TASKS_FILE, snapshot):
            raise TaskStoreWriteError("任务记录写入失败，下载未入队")
        _apply_persistence_success(versions)
        with _tasks_lock:
            for task in accepted:
                task_id = task["task_id"]
                _tasks[task_id] = task
                _persistence_versions.setdefault(task_id, 0)
                _persisted_versions.setdefault(task_id, 0)
        return copy.deepcopy(accepted), duplicates


def _backup_semantically_invalid(path: Path, reason: str) -> None:
    backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    if backup.exists():
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}")
    try:
        path.rename(backup)
        log.error("%s 内容无效(%s)，原文件已备份到 %s", path, reason, backup)
    except OSError:
        log.exception("%s 内容无效(%s)且备份失败", path, reason)


def _infer_artifact_root(task: dict) -> Optional[Path]:
    """从 legacy v2 的 absolute actual + media relpath 严格反推原下载根。"""
    manifest = task.get("manifest")
    raw_items = manifest.get("artifacts") if isinstance(manifest, dict) else []
    media_relpaths = [
        _normalise_relpath(item.get("relpath"))
        for item in raw_items if isinstance(item, dict) and item.get("role") == "media"
    ]
    media_relpaths = [path for path in media_relpaths if path]
    actual_raw = task.get("actual_filename")
    if len(media_relpaths) != 1 or not actual_raw:
        return None
    actual_path = Path(actual_raw).expanduser()
    if not actual_path.is_absolute():
        return None
    try:
        actual = actual_path.resolve(strict=False)
        root = actual
        for _part in PurePosixPath(media_relpaths[0]).parts:
            root = root.parent
        root = _canonical_root(root)
        if _resolve_under_root(media_relpaths[0], root) != actual:
            return None
        return root
    except (OSError, RuntimeError, UnsafeDownloadPathError, ValueError):
        return None


def _normalise_loaded_task(raw: dict) -> dict:
    task = copy.deepcopy(raw)
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("任务缺少 task_id")
    task["task_id"] = task_id
    ensure_task_identity(task)
    task["durable"] = True
    task.pop("persistence_pending", None)
    task.pop("persistence_error", None)
    if task.get("status") in ACTIVE:
        task["status"] = "interrupted"
        task["error"] = "服务重启,任务中断"
    task["cancel"] = False

    pinned_root: Optional[Path] = None
    if "manifest" in task:
        raw_items = ((task.get("manifest") or {}).get("artifacts")
                     if isinstance(task.get("manifest"), dict) else [])
        try:
            if task.get("artifact_root"):
                pinned_root = _canonical_root(task["artifact_root"])
            elif not raw_items:
                pinned_root = _download_root()
            else:
                pinned_root = _infer_artifact_root(task)
        except UnsafeDownloadPathError as exc:
            task["path_error"] = str(exc)
        if pinned_root is not None:
            task["artifact_root"] = str(pinned_root)
            task.pop("manifest_unbound", None)
        else:
            task.pop("artifact_root", None)
            task["manifest_unbound"] = True
            task["path_error"] = "产物清单无法证明原下载根，已禁止文件操作"
        task["manifest"] = _normalise_manifest(task, task.get("manifest"))

    actual = task.get("actual_filename")
    if actual:
        try:
            root = pinned_root if "manifest" in task else _download_root()
            if root is None:
                raise UnsafeDownloadPathError("产物清单缺少固定下载根")
            task["actual_filename"] = str(_resolve_under_root(actual, root))
        except UnsafeDownloadPathError as exc:
            task.pop("actual_filename", None)
            task["path_error"] = str(exc)

    conflict = task.get("rename_conflict")
    if isinstance(conflict, dict):
        normalised = {}
        try:
            root = pinned_root if "manifest" in task else _download_root()
            if root is None:
                raise UnsafeDownloadPathError("产物清单缺少固定下载根")
            for key in ("source", "expected", "existing"):
                if conflict.get(key):
                    normalised[key] = str(_resolve_under_root(conflict[key], root))
            if normalised:
                task["rename_conflict"] = normalised
            else:
                task.pop("rename_conflict", None)
        except UnsafeDownloadPathError as exc:
            task.pop("rename_conflict", None)
            task["path_error"] = str(exc)
    elif conflict is not None:
        task.pop("rename_conflict", None)
    return task


def _load_persisted() -> None:
    """加载、校验并迁移历史；不会恢复下载。

    语法损坏由 ``storage.read_json`` 备份；顶层类型、任务类型、空/重复 ID 等
    语义损坏在本层按同一 ``.corrupt-*`` 契约备份。合法但超出字节/记录容量的
    文件不是“损坏”：启动会明确失败，原文件保持原位，绝不截断或覆盖。
    """
    try:
        data = storage.read_json(
            Path(TASKS_FILE), default=None, max_bytes=MAX_TASKS_FILE_BYTES)
    except storage.JsonReadLimitError as exc:
        message = (
            f"tasks.json 超过 {MAX_TASKS_FILE_BYTES} 字节安全上限；"
            "请先备份并人工清理历史，原文件未被修改"
        )
        log.critical(message)
        raise TaskStoreLoadError(message) from exc
    if data is None:
        return
    if not isinstance(data, list):
        _backup_semantically_invalid(Path(TASKS_FILE), "顶层必须是 list")
        return
    if len(data) > MAX_TOTAL_TASKS:
        message = (
            f"tasks.json 含 {len(data)} 条记录，超过 {MAX_TOTAL_TASKS} 条安全上限；"
            "请先备份并人工清理历史，原文件未被修改"
        )
        log.critical(message)
        raise TaskStoreLoadError(message)

    normalised: list[dict] = []
    seen: set[str] = set()
    try:
        for raw in data:
            if not isinstance(raw, dict):
                raise ValueError("任务项必须是 dict")
            task = _normalise_loaded_task(raw)
            task_id = task["task_id"]
            if task_id in seen:
                raise ValueError(f"重复 task_id:{task_id}")
            seen.add(task_id)
            normalised.append(task)
    except (TypeError, ValueError) as exc:
        _backup_semantically_invalid(Path(TASKS_FILE), str(exc))
        return

    with _persist_lock:
        ok = storage.write_json(TASKS_FILE, _snapshot(normalised))
    if not ok:
        message = "启动迁移未能写回 tasks.json"
        for task in normalised:
            task["durable"] = False
            task["persistence_error"] = message
        log.error(message)
    with _tasks_lock:
        for task in normalised:
            task_id = task["task_id"]
            _tasks[task_id] = task
            _persistence_versions.setdefault(task_id, 0)
            _persisted_versions.setdefault(task_id, 0)


# 模块加载时恢复历史。
_load_persisted()


def get_task(task_id: str) -> Optional[dict]:
    """返回任务深拷贝，嵌套 manifest/conflict 不泄露共享引用。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
        return copy.deepcopy(task) if task else None


def list_tasks() -> list[dict]:
    """返回全部任务的深拷贝快照。"""
    with _tasks_lock:
        return copy.deepcopy(list(_tasks.values()))


def find_active_by_url(url: str, label: str, page: int = 1) -> Optional[str]:
    with _tasks_lock:
        for tid, task in _tasks.items():
            if (task.get("url") == url and task.get("label") == label
                    and task.get("page", 1) == page
                    and task.get("status") in ACTIVE):
                return tid
    return None


def is_downloaded(url: str) -> bool:
    """该 (BV 号, 分 P) 是否已有 done 任务。"""
    bv = bvid_from_url(url)
    if not bv:
        return False
    key = extras.dl_key(bv, extras.page_from_url(url))
    with _tasks_lock:
        for task in _tasks.values():
            if task.get("status") != "done":
                continue
            tbv = bvid_from_url(task.get("url") or "")
            if tbv and extras.dl_key(tbv, int(task.get("page") or 1)) == key:
                return True
    return False


_STEM_SUFFIX_STARTS = (" ", ".", "-", "_", "(", "[")


def stem_belongs(stem: str, expected: str) -> bool:
    if stem == expected:
        return True
    if not stem.startswith(expected):
        return False
    return stem[len(expected):].startswith(_STEM_SUFFIX_STARTS)


_stem_belongs = stem_belongs


def is_owned_sidecar(main: Path, candidate: Path) -> bool:
    """只接管本应用会生成的封面/字幕/弹幕，排除同 stem 的其他媒体。"""
    return (candidate != main
            and candidate.name.startswith(main.stem + ".")
            and candidate.suffix.lower() in _SIDECAR_EXTS)


def _file_family(main: Path, *, root=None) -> list[Path]:
    """返回安全主文件及白名单副件；任何越界或未知成员均不归属该任务。"""
    try:
        safe_main = resolve_download_path(main, root=root)
    except UnsafeDownloadPathError:
        return []
    family: list[Path] = [safe_main] if safe_main.is_file() else []
    try:
        for candidate in safe_main.parent.glob(glob_escape(safe_main.stem) + ".*"):
            try:
                safe = resolve_download_path(candidate, root=root)
            except UnsafeDownloadPathError:
                continue
            if safe.is_file() and is_owned_sidecar(safe_main, safe):
                family.append(safe)
    except OSError:
        pass
    return sorted(set(family))


def build_manifest(task: dict, media_path) -> dict:
    """由已知主媒体和白名单副件重建完整产物清单。"""
    if "manifest" not in task:
        ensure_task_identity(task, with_manifest=True)
    else:
        ensure_task_identity(task)
    try:
        root = task_download_root(task)
        media = resolve_download_path(media_path, must_exist=True, root=root)
    except (UnsafeDownloadPathError, FileNotFoundError):
        return {"version": MANIFEST_VERSION, "artifacts": []}
    artifacts: list[dict] = []
    for path in _file_family(media, root=root):
        role = "media" if path == media else "sidecar"
        relpath = download_relative_path(path, root=root)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        artifacts.append({
            "artifact_id": artifact_id_for(task["artifact_set_id"], role, relpath),
            "role": role,
            "relpath": relpath,
            "size": size,
        })
    artifacts.sort(key=lambda item: (item["role"] != "media", item["relpath"]))
    return {"version": MANIFEST_VERSION, "artifacts": artifacts}


def refresh_task_manifest(task_id: str, media_path=None) -> list[Path]:
    """从安全主媒体刷新 manifest/actual_filename，返回当前整族文件。"""
    with _tasks_lock:
        task = copy.deepcopy(_tasks.get(task_id))
    if not task:
        return []
    if "manifest" not in task:
        ensure_task_identity(task, with_manifest=True)
    try:
        root = task_download_root(task)
    except UnsafeDownloadPathError:
        return []
    if media_path is None:
        media_path = manifest_media_path(task) or task.get("actual_filename")
    if not media_path:
        return []
    try:
        media = resolve_download_path(media_path, must_exist=True, root=root)
    except (UnsafeDownloadPathError, FileNotFoundError):
        return []
    manifest = build_manifest(task, media)
    with _tasks_lock:
        current = _tasks.get(task_id)
        if not current:
            return []
        ensure_task_identity(current, with_manifest=True)
        current["artifact_root"] = task["artifact_root"]
        current["manifest"] = manifest
        current["actual_filename"] = str(media)  # 展示兼容字段，不作为新任务文件真相源。
    return _manifest_files({**task, "manifest": manifest})


def _manifest_files_state(task: dict, *, root=None) -> tuple[bool, list[Path]]:
    """返回 ``(可安全检查, 已存在文件)``，根不一致/清单歧义不是“缺失”。"""
    try:
        safe_root = task_download_root(task) if root is None else _canonical_root(root)
    except UnsafeDownloadPathError:
        return False, []
    manifest = _normalise_manifest(task, task.get("manifest"))
    media_items = [item for item in manifest["artifacts"] if item["role"] == "media"]
    if len(media_items) > 1:
        return False, []
    paths: list[Path] = []
    for item in manifest["artifacts"]:
        try:
            path = resolve_download_path(item["relpath"], root=safe_root)
        except UnsafeDownloadPathError:
            return False, []
        if path.is_file():
            paths.append(path)
    return True, paths


def _manifest_files(task: dict, *, root=None) -> list[Path]:
    return _manifest_files_state(task, root=root)[1]


def manifest_media_path(task: dict) -> Optional[Path]:
    if "manifest" not in task:
        return None
    try:
        root = task_download_root(task)
    except UnsafeDownloadPathError:
        return None
    manifest = _normalise_manifest(task, task.get("manifest"))
    media: list[Path] = []
    for item in manifest["artifacts"]:
        if item["role"] != "media":
            continue
        try:
            path = resolve_download_path(item["relpath"], root=root)
        except UnsafeDownloadPathError:
            continue
        if path.is_file():
            media.append(path)
    return media[0] if len(media) == 1 else None


def _legacy_dirs(*, root=None) -> list[Path]:
    safe_root = _download_root() if root is None else _canonical_root(root)
    dirs = [safe_root]
    try:
        for child in safe_root.iterdir():
            if not child.is_dir():
                continue
            try:
                safe = resolve_download_path(child, root=safe_root)
            except UnsafeDownloadPathError:
                continue
            if safe.is_dir():
                dirs.append(safe)
    except OSError:
        pass
    return dirs


def _legacy_primary_candidates(task: dict, *, root=None) -> list[Path]:
    title = (task.get("title") or "").strip()
    if not title or title == "(解析中)":
        return []
    expected = extras.build_filename(title, int(task.get("page") or 1))
    exact: list[Path] = []
    belongs: list[Path] = []
    safe_root = _download_root() if root is None else _canonical_root(root)
    for directory in _legacy_dirs(root=safe_root):
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                safe = resolve_download_path(child, root=safe_root)
            except UnsafeDownloadPathError:
                continue
            if not safe.is_file() or safe.suffix.lower() not in _MEDIA_EXTS:
                continue
            if safe.stem == expected:
                exact.append(safe)
            elif stem_belongs(safe.stem, expected):
                belongs.append(safe)
    return sorted(set(exact if exact else belongs))


def _task_files_state(task: dict) -> tuple[bool, list[Path]]:
    """区分“已验证不存在”与“当前根不匹配/路径不安全，无法检查”。"""
    if "manifest" in task:
        return _manifest_files_state(task)

    root = _download_root()
    actual = task.get("actual_filename")
    if actual:
        try:
            path = resolve_download_path(actual, root=root)
        except UnsafeDownloadPathError:
            return False, []
        if path.is_file():
            return True, _file_family(path, root=root)
        return True, []

    if not root.is_dir():
        return False, []
    files: list[Path] = []
    for media in _legacy_primary_candidates(task, root=root):
        files.extend(_file_family(media, root=root))
    return True, sorted(set(files))


def _task_files(task: dict) -> list[Path]:
    """定位任务产物；有 manifest 的任务只信 manifest，legacy 才使用旧规则。"""
    return _task_files_state(task)[1]


def find_task_files(task_id: str) -> list[Path]:
    task = get_task(task_id)
    return _task_files(task) if task else []


def _files_for_destructive_delete(task: dict, *, root=None) -> list[Path]:
    """返回允许删除的文件；legacy 扫描只有主候选唯一时才放行。"""
    safe_root = (_canonical_root(root) if root is not None
                 else task_download_root(task))
    if "manifest" in task:
        return _manifest_files(task, root=safe_root)

    actual = task.get("actual_filename")
    if actual:
        try:
            media = resolve_download_path(actual, root=safe_root)
        except UnsafeDownloadPathError:
            return []
        if media.is_file():
            return _file_family(media, root=safe_root)
        return []

    candidates = _legacy_primary_candidates(task, root=safe_root)
    if len(candidates) != 1:
        if len(candidates) > 1:
            log.warning("legacy 任务 %s 文件候选歧义，拒绝删除文件: %s",
                        task.get("task_id"), [str(path) for path in candidates])
        return []
    return _file_family(candidates[0], root=safe_root)


def delete_tasks_detailed(task_ids: list, delete_file: bool) -> dict:
    """保守删除终态记录，并返回记录与文件层面的可观察结果。

    记录仍保持 durable-first：写盘失败时既不移除内存记录也不 unlink 文件。
    ``file_skipped_count`` 包括文件已不存在，以及没有可安全确认归属的产物；
    调用方可据此区分“记录已删”与“本地文件确实已删”。
    """
    wanted = sorted({str(task_id) for task_id in task_ids})
    result = {
        "count": 0,
        "requested_count": len(wanted),
        "retained_count": len(wanted),
        "file_deleted_count": 0,
        "file_failed_count": 0,
        "file_skipped_count": 0,
        "partial": False,
    }
    if not wanted:
        return result
    with ExitStack() as stack:
        for task_id in wanted:
            stack.enter_context(artifact_lifecycle(task_id))

        removed: list[dict] = []
        files_by_task: dict[str, list[Path]] = {}
        roots_by_task: dict[str, Path] = {}
        with _persist_lock:
            with _tasks_lock:
                for task_id in wanted:
                    task = _tasks.get(task_id)
                    if not task or task.get("status") in ACTIVE:
                        continue
                    snapshot_task = copy.deepcopy(task)
                    removed.append(snapshot_task)
                    if delete_file:
                        try:
                            root = task_download_root(snapshot_task)
                            files_by_task[task_id] = _files_for_destructive_delete(
                                snapshot_task, root=root)
                            roots_by_task[task_id] = root
                        except UnsafeDownloadPathError:
                            files_by_task[task_id] = []
                            log.warning("任务 %s 的下载根不安全，跳过文件删除", task_id)
                if not removed:
                    result["partial"] = bool(wanted)
                    return result
                removed_ids = {task["task_id"] for task in removed}
                remaining = [(tid, task) for tid, task in _tasks.items()
                             if tid not in removed_ids]
                snapshot = _snapshot(task for _tid, task in remaining)
                versions = {tid: _persistence_versions.get(tid, 0)
                            for tid, _task in remaining}
            if not storage.write_json(TASKS_FILE, snapshot):
                raise TaskStoreWriteError("任务记录删除未能写入，文件未删除")
            _apply_persistence_success(versions)
            with _tasks_lock:
                for task_id in removed_ids:
                    _tasks.pop(task_id, None)
                    _persistence_versions.pop(task_id, None)
                    _persisted_versions.pop(task_id, None)
            with _final_paths_lock:
                for task_id in removed_ids:
                    _final_paths.pop(task_id, None)

        if delete_file:
            for task in removed:
                task_id = task["task_id"]
                paths = files_by_task.get(task_id, [])
                if not paths:
                    result["file_skipped_count"] += 1
                    continue
                for path in paths:
                    try:
                        safe_path = resolve_download_path(
                            path, must_exist=False, root=roots_by_task[task_id])
                        if not safe_path.exists():
                            result["file_skipped_count"] += 1
                            continue
                        safe_path.unlink()
                        result["file_deleted_count"] += 1
                    except FileNotFoundError:
                        result["file_skipped_count"] += 1
                    except (OSError, UnsafeDownloadPathError):
                        result["file_failed_count"] += 1
                        log.exception("删除任务产物失败 task_id=%s path=%s",
                                      task_id, path)

        result["count"] = len(removed)
        result["retained_count"] = max(0, len(wanted) - len(removed))
        result["partial"] = bool(
            result["retained_count"] or result["file_failed_count"])
        return result


def delete_tasks(task_ids: list, delete_file: bool) -> int:
    """兼容旧调用方：只返回成功移除的任务记录数。"""
    return int(delete_tasks_detailed(task_ids, delete_file)["count"])


def clear_tasks(delete_file: bool) -> int:
    with _tasks_lock:
        ids = [tid for tid, task in _tasks.items()
               if task.get("status") not in _ACTIVE]
    return delete_tasks(ids, delete_file)


def delete_invalid_tasks(task_ids: list[str]) -> dict:
    """在 artifact lock 内最终验证缺失事实，并持锁完成条件删除。"""
    wanted = sorted({str(task_id) for task_id in task_ids})
    with ExitStack() as stack:
        for task_id in wanted:
            stack.enter_context(artifact_lifecycle(task_id))
        with _tasks_lock:
            snapshots = {task_id: copy.deepcopy(_tasks.get(task_id))
                         for task_id in wanted}

        failed_ids: list[str] = []
        invalid_ids: list[str] = []
        for task_id, task in snapshots.items():
            if not task:
                continue
            if task.get("status") == "failed":
                failed_ids.append(task_id)
            elif task.get("status") == "done":
                inspectable, files = _task_files_state(task)
                if inspectable and not files:
                    invalid_ids.append(task_id)

        candidates = failed_ids + invalid_ids
        if candidates:
            delete_tasks(candidates, delete_file=False)
        with _tasks_lock:
            remaining = set(_tasks)
        return {
            "failed": sum(task_id not in remaining for task_id in failed_ids),
            "invalid": sum(task_id not in remaining for task_id in invalid_ids),
            "removed": sum(task_id not in remaining for task_id in candidates),
        }


def clean_invalid_records() -> dict:
    with _tasks_lock:
        candidates = [tid for tid, task in _tasks.items()
                      if task.get("status") in {"failed", "done"}]
    return delete_invalid_tasks(candidates)


def _disk_usage_bytes() -> int:
    """短时缓存下载根磁盘用量，避免统计轮询反复全树扫描。"""
    global _disk_usage_cache
    try:
        root = _download_root()
    except Exception:
        return 0
    key = str(root)
    now = time.monotonic()
    with _disk_usage_cache_lock:
        cached = _disk_usage_cache
        if (cached is not None and cached[0] == key
                and now - cached[1] < _DISK_USAGE_CACHE_SECONDS):
            return cached[2]
        total = 0
        try:
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            total = 0
        _disk_usage_cache = (key, now, total)
        return total


def compute_stats() -> dict:
    import datetime
    today = datetime.date.today()
    month_start = int(time.mktime(datetime.date(today.year, today.month, 1).timetuple()))
    day_start = int(time.mktime(today.timetuple()))

    total_count = 0
    total_bytes = 0
    month_count = 0
    day_count = 0
    for task in list_tasks():
        if task.get("status") != "done":
            continue
        total_count += 1
        total_bytes += task.get("downloaded_bytes") or 0
        finished = task.get("finished_at")
        if finished:
            if finished >= day_start:
                day_count += 1
            if finished >= month_start:
                month_count += 1

    disk_bytes = _disk_usage_bytes()

    return {
        "total_count": total_count,
        "total_bytes": total_bytes if total_bytes else disk_bytes,
        "month_count": month_count,
        "day_count": day_count,
        "disk_bytes": disk_bytes,
    }
