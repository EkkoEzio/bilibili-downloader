"""A3: 自定义 UP 清单 + 增量记录 + 本地历史。

数据格式 data/up_list.json:
{
  "ups": [{"mid", "uname", "face", "added_at", "auto"}],  # 用户管理的 UP 列表
  "last_seen": {"<mid>": {"bvid":..., "ts":..., "title":..., "fetched_at":...}},
      # 上次「刷新看到」的最新视频;fetched_at 是上次拉取时刻(自适应页大小用)
  "history": [{"bvid", "title", ..., "found_at"}]  # 历次刷新发现的新增(新在前,上限100)
}

读/写都用文件锁保证并发安全。
"""
import json
import threading
import time
from typing import Optional

from config import UP_LIST_FILE

_lock = threading.Lock()

HISTORY_CAP = 100  # 本地历史上限(用户 2026-08-16 暂定)


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
        # 已存在:不重复加,但补齐缺失的名称/头像(存量记录回填)
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                u.setdefault("auto", False)
                if uname and not u.get("uname"):
                    u["uname"] = uname
                    if face:
                        u["face"] = face
                    _write(data)
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
    """刷新时记录「最新已见」,下次对比用。fetched_at 记录本次拉取时刻
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


def add_history(items: list) -> None:
    """把本轮发现的新增视频存入本地历史:按 bvid 去重、按发布时间倒序、上限 HISTORY_CAP。

    历史「看之前拉到过什么」用,离线可读,不再为看历史打 B 站接口。
    超过上限裁掉最旧的。
    """
    if not items:
        return
    with _lock:
        data = _read()
        hist = data.get("history", [])
        have = {h.get("bvid") for h in hist}
        now = int(time.time())
        for v in items:
            bv = v.get("bvid")
            if not bv or bv in have:
                continue
            have.add(bv)
            entry = dict(v)
            entry["found_at"] = now
            hist.append(entry)
        hist.sort(key=lambda h: (h.get("view_at", 0), h.get("found_at", 0)), reverse=True)
        data["history"] = hist[:HISTORY_CAP]
        _write(data)


def list_history() -> list:
    with _lock:
        return _read().get("history", [])


def reset_seen(mid: int) -> None:
    """重置某个 UP 的已见标记(下次刷新会把他的所有视频都算新增)。"""
    with _lock:
        data = _read()
        data["last_seen"].pop(str(mid), None)
        _write(data)


PENDING_CAP = 200  # 待确认列表上限,超出裁最旧


def add_pending(items: list) -> int:
    """追更发现的新视频写入「待确认」列表(等用户点下载,不自动下)。

    按 bvid 去重;是否已下载由调用方过滤。返回本轮净新增条数。
    """
    if not items:
        return 0
    with _lock:
        data = _read()
        pend = data.get("pending", [])
        have = {p.get("bvid") for p in pend}
        now = int(time.time())
        added = 0
        for v in items:
            bv = v.get("bvid")
            if not bv or bv in have:
                continue
            have.add(bv)
            entry = dict(v)
            entry["found_at"] = now
            pend.append(entry)
            added += 1
        if added:
            data["pending"] = pend[-PENDING_CAP:]
            _write(data)
        return added


def list_pending() -> list:
    with _lock:
        return _read().get("pending", [])


def clear_pending(bvids=None) -> None:
    """清空待确认:bvids 为 None 全清;否则只移除这些(下载入队后调用)。"""
    with _lock:
        data = _read()
        pend = data.get("pending", [])
        if bvids is None:
            data["pending"] = []
        else:
            drop = set(bvids)
            data["pending"] = [p for p in pend if p.get("bvid") not in drop]
        _write(data)