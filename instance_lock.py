"""进程级单实例锁。

锁必须在导入任何会读取/迁移运行时状态的模块之前取得。锁文件只保存诊断元数据；
真正的互斥由操作系统文件锁提供，进程异常退出时内核也会自动释放。
"""
from __future__ import annotations

import atexit
import json
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO, Optional


class AlreadyRunningError(RuntimeError):
    """同一数据目录已有进程持有实例锁。"""


_guard = threading.Lock()
_handle: Optional[BinaryIO] = None


def _lock_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "app.lock"


def _try_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _owner_description(handle: BinaryIO) -> str:
    try:
        handle.seek(0)
        payload = handle.read().lstrip(b"\0")
        owner = json.loads(payload.decode("utf-8")) if payload else {}
        pid = int(owner.get("pid") or 0)
        if pid > 0:
            return f"（PID {pid}）"
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return ""


def acquire_instance_lock() -> None:
    """非阻塞取得工作区单实例锁；同一进程重复调用是幂等的。"""
    global _handle
    with _guard:
        if _handle is not None:
            return

        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            _try_lock(handle)
        except (BlockingIOError, OSError) as exc:
            owner = _owner_description(handle)
            handle.close()
            raise AlreadyRunningError(
                f"已有另一个 bilibili download 实例正在运行{owner}；"
                "请先在原窗口停止它，再重新启动。"
            ) from exc

        metadata = json.dumps({
            "pid": os.getpid(),
            "started_at": int(time.time()),
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handle.seek(0)
        handle.truncate()
        handle.write(metadata)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            # 锁本身已生效；诊断元数据无法 fsync 不应让实例失去保护。
            pass
        _handle = handle


def release_instance_lock() -> None:
    """显式释放实例锁；通常由 atexit 调用。"""
    global _handle
    # 解锁和清空句柄必须处于同一 guard 临界区；否则另一线程可能在旧句柄
    # 尚未 unlock 时看见 None，并把同进程的短暂竞争误报成第二实例。
    with _guard:
        handle = _handle
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:
            pass
        finally:
            handle.close()
            _handle = None


atexit.register(release_instance_lock)
