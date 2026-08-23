"""页面数据缓存:打开页面立即展示上次数据,后台再静默拉新。

慢数据源(关注动态串行拉 30 个 UP 约 45 秒、关注列表翻页等)每次打开页面
都干等体验很差。统一模式:各数据端点成功返回前把结果写入 data/cache/,
前端先读 /api/cache/<key> 秒显,再后台请求真接口,有变化才提示「有新内容」。

缓存只是展示加速,不是数据源:删了也能正常工作(回到首次打开的加载态)。
"""
import threading
import time
from pathlib import Path

import storage

from config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# 允许读写的缓存键(与路由一一对应,防任意文件读写)
KEYS = ("follow_updates", "followings", "fav_folders", "fav_seasons", "watchlater")

# 磁盘清理:超 30 天没更新的缓存文件,应用启动时删除(防长期积累)。
DISCARD_AFTER = 30 * 86400

_lock = threading.Lock()


def purge_stale() -> int:
    """删除超 30 天没更新的缓存文件(按 mtime),返回删除数。启动时调用。"""
    now = time.time()
    removed = 0
    with _lock:
        for f in CACHE_DIR.glob("*.json"):
            try:
                if now - f.stat().st_mtime > DISCARD_AFTER:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def save(key: str, payload: dict) -> None:
    """把某端点的完整响应体写入缓存(带保存时间)。"""
    if key not in KEYS:
        return
    entry = {"saved_at": int(time.time()), "data": payload}
    with _lock:
        storage.write_json(CACHE_DIR / f"{key}.json", entry)


def load(key: str):
    """读缓存。返回 {"saved_at", "data"} 或 None。"""
    if key not in KEYS:
        return None
    with _lock:
        return storage.read_json(CACHE_DIR / f"{key}.json", default=None)
