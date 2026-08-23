"""自定义 UP 清单(加入清单 = 自动下载该 UP 的新投稿)。

数据格式 data/up_list.json:
{
  "ups": [{"mid", "uname", "face", "added_at", "quality"}],  # 用户管理的 UP 列表
      # quality: 该 UP 新视频自动下载用的默认清晰度("" = 最高画质自动)
  "last_seen": {"<mid>": {"bvid":..., "ts":..., "title":..., "fetched_at":...}},
      # 上次检查的最新视频;fetched_at 是上次拉取时刻(自适应页大小用)
}

2026-08-18 改版:去掉「auto 开关 + 待确认 + 本地历史」。清单里的 UP 一律
自动下载新投稿(任务直接进下载队列),清晰度按每个 UP 的 quality 配置。

读/写都用文件锁保证并发安全。
"""
import threading
import time
from typing import Optional

import storage
from config import UP_LIST_FILE

_lock = threading.Lock()

# 每个 UP 可选的默认清晰度(""=最高画质自动);与前端下拉、quality.py 标签一致
QUALITY_CHOICES = ("", "1080P", "720P", "480P", "360P", "音频")


def _read() -> dict:
    return storage.read_json(UP_LIST_FILE, default={"ups": [], "last_seen": {}})


def _write(data: dict) -> None:
    storage.write_json(UP_LIST_FILE, data, indent=1)


def list_ups() -> list[dict]:
    with _lock:
        ups = _read().get("ups", [])
    # 老数据带 auto 字段(开关已废除),读时丢弃;quality 统一补默认 ""
    for u in ups:
        u.pop("auto", None)
        u.setdefault("quality", "")
    return ups


def add_up(mid: int, uname: str = "", face: str = "") -> dict:
    with _lock:
        data = _read()
        # 已存在:不重复加,但补齐缺失的名称/头像(存量记录回填)
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                u.pop("auto", None)
                u.setdefault("quality", "")
                if uname and not u.get("uname"):
                    u["uname"] = uname
                    if face:
                        u["face"] = face
                    _write(data)
                return u
        u = {"mid": int(mid), "uname": uname, "face": face,
             "added_at": int(time.time()), "quality": ""}
        data["ups"].append(u)
        _write(data)
        return u


def set_quality(mid: int, quality: str) -> bool:
    """设置该 UP 新视频自动下载的默认清晰度("" = 最高画质自动)。"""
    quality = quality if quality in QUALITY_CHOICES else ""
    with _lock:
        data = _read()
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                u["quality"] = quality
                _write(data)
                return True
        return False


def set_uname(mid: int, uname: str) -> bool:
    """写入 UP 名称。给「添加时没拿到名称、后续拉取到数据」的存量记录回填用
    (mid 人记不住,界面必须能显示名称)。"""
    if not uname:
        return False
    with _lock:
        data = _read()
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                u["uname"] = uname
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


def mark_seen(mid: int, bvid: str, ts: int, title: str = "",
              fetched_at: Optional[int] = None) -> None:
    """检查时记录「最新已见」,下次对比用。fetched_at 记录本次拉取时刻
    (自适应页大小的依据);不传则保留原值。"""
    with _lock:
        data = _read()
        entry = data["last_seen"].get(str(mid), {})
        entry.update({"bvid": bvid, "ts": ts, "title": title})
        if fetched_at is not None:
            entry["fetched_at"] = int(fetched_at)
        data["last_seen"][str(mid)] = entry
        _write(data)


def get_fetched_at(mid: int) -> Optional[int]:
    """某 UP 上次拉取的时刻;从未拉过返回 None。"""
    with _lock:
        last = _read().get("last_seen", {}).get(str(mid))
    return (last or {}).get("fetched_at")


def reset_seen(mid: int) -> None:
    """重置某个 UP 的已见标记(下次检查会把他的所有视频都算新增)。"""
    with _lock:
        data = _read()
        data["last_seen"].pop(str(mid), None)
        _write(data)
