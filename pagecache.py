"""页面数据缓存：生产数据按 fingerprint + incarnation + generation 隔离。

``context=None`` 仅保留给旧调用和测试的顶层兼容命名空间；生产路由必须显式
传入已认证 context。无法证明归属的历史顶层缓存绝不会被认领给任何账号或生命期。
"""
import os
import threading
import time
import uuid
from pathlib import Path

import storage
from bilibili_auth import (AuthContextChangedError, commit_if_current,
                           is_auth_context_current)
from config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

KEYS = ("follow_updates", "followings", "fav_folders", "fav_seasons", "watchlater",
        "home_feed")
DISCARD_AFTER = 30 * 86400
_lock = threading.Lock()


def _context_meta(context):
    if not isinstance(context, dict) or context.get("authenticated") is not True:
        return None
    fingerprint = str(context.get("fingerprint") or "").lower()
    incarnation = str(context.get("incarnation") or "").lower()
    try:
        generation = int(context.get("generation"))
    except (TypeError, ValueError):
        return None
    hex_chars = set("0123456789abcdef")
    if (len(fingerprint) != 64 or any(ch not in hex_chars for ch in fingerprint)
            or len(incarnation) != 32
            or any(ch not in hex_chars for ch in incarnation)
            or generation < 0):
        return None
    return {
        "source": str(context.get("source") or "none"),
        "fingerprint": fingerprint,
        "incarnation": incarnation,
        "generation": generation,
    }


def _cache_path(key: str, context=None):
    if context is None:
        return CACHE_DIR / f"{key}.json"
    meta = _context_meta(context)
    if meta is None:
        return None
    return (CACHE_DIR / "accounts" / meta["fingerprint"]
            / f"i-{meta['incarnation']}" / f"g-{meta['generation']}"
            / f"{key}.json")


def purge_stale() -> int:
    """递归删除超过 30 天的账号缓存并清理空目录。"""
    now = time.time()
    removed = 0
    root = CACHE_DIR
    with _lock:
        if not root.exists():
            return 0
        for cache_file in root.rglob("*.json"):
            try:
                if now - cache_file.stat().st_mtime > DISCARD_AFTER:
                    cache_file.unlink()
                    removed += 1
            except OSError:
                pass
        directories = sorted((path for path in root.rglob("*") if path.is_dir()),
                             key=lambda path: len(path.parts), reverse=True)
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
    return removed


def save(key: str, payload: dict, context=None) -> bool:
    """保存缓存；显式 context 使用候选文件 + 认证 CAS 原子发布。"""
    if key not in KEYS or not isinstance(payload, dict):
        return False
    path = _cache_path(key, context)
    if path is None:
        return False
    meta = _context_meta(context) if context is not None else None
    entry = {"saved_at": int(time.time()), "data": payload}
    if meta is not None:
        entry["auth_context"] = meta

    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if context is None:
            return storage.write_json(path, entry)

        if not is_auth_context_current(context):
            raise AuthContextChangedError("登录状态已变化，请重试")
        candidate = path.with_name(
            f".{path.name}.candidate-{uuid.uuid4().hex}.json")
        if not storage.write_json(candidate, entry):
            return False
        try:
            def publish():
                os.replace(candidate, path)
                return True

            return bool(commit_if_current(context, publish))
        finally:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def load(key: str, context=None):
    """读取 exact context 的缓存；显式 context 永不回退到旧顶层文件。"""
    if key not in KEYS:
        return None
    path = _cache_path(key, context)
    if path is None:
        return None
    if context is not None and not is_auth_context_current(context):
        raise AuthContextChangedError("登录状态已变化，请重试")
    with _lock:
        entry = storage.read_json(path, default=None)
    if not isinstance(entry, dict):
        return None
    if context is not None:
        meta = _context_meta(context)
        if entry.get("auth_context") != meta:
            return None
        if not is_auth_context_current(context):
            raise AuthContextChangedError("登录状态已变化，请重试")
    return entry
