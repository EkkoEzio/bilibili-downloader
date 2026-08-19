"""页面数据缓存:打开页面立即展示上次数据,后台再静默拉新。

慢数据源(关注动态串行拉 30 个 UP 约 45 秒、关注列表翻页等)每次打开页面
都干等体验很差。统一模式:各数据端点成功返回前把结果写入 data/cache/,
前端先读 /api/cache/<key> 秒显,再后台请求真接口,有变化才提示「有新内容」。

缓存只是展示加速,不是数据源:删了也能正常工作(回到首次打开的加载态)。
"""
import json
import threading
import time
from pathlib import Path

from config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# 允许读写的缓存键(与路由一一对应,防任意文件读写)
KEYS = ("follow_updates", "followings", "fav_folders", "fav_seasons", "watchlater")

_lock = threading.Lock()


def save(key: str, payload: dict) -> None:
    """把某端点的完整响应体写入缓存(带保存时间)。"""
    if key not in KEYS:
        return
    entry = {"saved_at": int(time.time()), "data": payload}
    with _lock:
        try:
            (CACHE_DIR / f"{key}.json").write_text(
                json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # 缓存写失败不影响正常返回


def load(key: str):
    """读缓存。返回 {"saved_at", "data"} 或 None。"""
    if key not in KEYS:
        return None
    with _lock:
        try:
            return json.loads((CACHE_DIR / f"{key}.json").read_text(encoding="utf-8"))
        except Exception:
            return None
