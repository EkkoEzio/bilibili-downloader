"""A3: 自定义 UP 清单 + 增量记录。

数据格式 data/up_list.json:
{
  "ups": [{"mid", "uname", "face", "added_at"}],  # 用户管理的 UP 列表
  "last_seen": {"<mid>": {"bvid":..., "ts":..., "title":...}}  # 上次「刷新看到」的 UP 最新视频
}

读/写都用文件锁保证并发安全。
"""
import json
import threading
import time
from typing import Optional

from config import UP_LIST_FILE

_lock = threading.Lock()


def _read() -> dict:
    if not UP_LIST_FILE.exists():
        return {"ups": [], "last_seen": {}}
    try:
        return json.loads(UP_LIST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"ups": [], "last_seen": {}}


def _write(data: dict) -> None:
    UP_LIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def list_ups() -> list[dict]:
    with _lock:
        ups = _read().get("ups", [])
    # 老数据没有 auto 字段,统一补默认 False
    for u in ups:
        u.setdefault("auto", False)
    return ups


def add_up(mid: int, uname: str = "", face: str = "") -> dict:
    with _lock:
        data = _read()
        # 已存在就不重复加
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                u.setdefault("auto", False)
                return u
        u = {"mid": int(mid), "uname": uname, "face": face,
             "added_at": int(time.time()), "auto": False}
        data["ups"].append(u)
        _write(data)
        return u


def set_auto(mid: int, auto: bool) -> bool:
    """开/关某 UP 的订阅追更(自动下载新增投稿)。"""
    with _lock:
        data = _read()
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                u["auto"] = bool(auto)
                _write(data)
                return True
        return False


def remove_up(mid: int) -> bool:
    with _lock:
        data = _read()
        before = len(data["ups"])
        data["ups"] = [u for u in data["ups"] if int(u["mid"]) != int(mid)]
        data["last_seen"].pop(str(mid), None)
        removed = len(data["ups"]) < before
        if removed:
            _write(data)
        return removed


def get_last_seen(mid: int) -> Optional[dict]:
    with _lock:
        return _read().get("last_seen", {}).get(str(mid))


def mark_seen(mid: int, bvid: str, ts: int, title: str = "") -> None:
    """刷新时记录「最新已见」,下次对比用。"""
    with _lock:
        data = _read()
        data["last_seen"][str(mid)] = {"bvid": bvid, "ts": ts, "title": title}
        _write(data)


def reset_seen(mid: int) -> None:
    """重置某个 UP 的已见标记(下次刷新会把他的所有视频都算新增)。"""
    with _lock:
        data = _read()
        data["last_seen"].pop(str(mid), None)
        _write(data)