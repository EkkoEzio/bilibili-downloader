"""自定义 UP 清单(加入清单 = 自动下载该 UP 的新投稿)。

数据格式 data/up_list.json:
{
  "ups": [{"mid", "uname", "face", "added_at", "quality"}],  # 用户管理的 UP 列表
      # quality: 该 UP 新视频自动下载用的默认清晰度("" = 最高画质自动)
  "last_seen": {
    "<mid>": {
      "bvid": ..., "ts": ..., "title": ..., "fetched_at": ...,
      "same_ts_bvids": [...],
      "scan": {
        "ps": ..., "next_pn": ..., "deep_next_pn": ...,
        "head_next_pn": ..., "head_repair_until_pn": ...,
        "terminal_pn": ... | null, "head_bvid": ..., "head_ts": ...,
        "items": [...]
      }
    }
  },
      # (ts,bvid集合) 是已完整覆盖的复合游标；scan 是达到单轮页预算后的续扫点。
      # fetched_at 是上次完成拉取时刻(自适应页大小用)。
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

# 个人本地清单的硬边界。达到上限时明确拒绝，绝不静默截断，否则用户会误以为
# 被裁掉的 UP/续扫投稿仍在受管。
MAX_UPS = 100
MAX_SCAN_ITEMS = 500


class UpListCapacityError(RuntimeError):
    """UP 清单或单个续扫状态超过安全容量。"""

    def __init__(self, capacity: str, limit: int, current: int,
                 requested: int = 0) -> None:
        self.capacity = str(capacity)
        self.limit = int(limit)
        self.current = int(current)
        self.requested = int(requested)
        if self.capacity == "ups":
            detail = f"UP 清单容量不足（当前 {self.current}，上限 {self.limit}）"
        elif self.capacity == "collection_items":
            detail = (f"该合集/收藏夹含 {self.current} 条，超过去重上限 "
                      f"{self.limit}，无法建立基线")
        elif self.capacity == "collections":
            detail = (f"合集/收藏夹订阅数已达上限（当前 {self.current}，"
                      f"上限 {self.limit}）")
        else:
            detail = f"续扫状态含 {self.current} 项，超过安全上限 {self.limit}"
        super().__init__(f"{detail}；请先清理后重试")


class UpListWriteError(RuntimeError):
    """UP 清单或游标未能可靠写入磁盘。"""


# 每个 UP 可选的默认清晰度(""=最高画质自动);与前端下拉、quality.py 标签一致
# 与 static/js/quality.js 的 QUALITY_OPTIONS 保持一致(测试会比对两侧)。
# 低于 720P 无实际需求已移除;B 站无 1440P 档(上一版误加),60 帧档为 1080P60/720P60。
# 存量配置里的旧值由 set_quality 归一为 ""
QUALITY_CHOICES = ("", "2160P", "1080P60", "1080P", "720P60", "720P", "音频")


def _read() -> dict:
    data = storage.read_json(UP_LIST_FILE, default={"ups": [], "last_seen": {}})
    # default 只在文件缺失/损坏时生效;手改或极旧遗留的合法 JSON 可能缺键,
    # set_quality/remove_up 等写函数直接 data["ups"] 会 KeyError 变 500。
    if not isinstance(data, dict):
        return {"ups": [], "last_seen": {}}
    data.setdefault("ups", [])
    data.setdefault("last_seen", {})
    return data


def _write(data: dict) -> bool:
    return storage.write_json(UP_LIST_FILE, data, indent=1)


def _commit(data: dict) -> None:
    if not _write(data):
        raise UpListWriteError("UP 清单保存失败")


def list_ups() -> list[dict]:
    with _lock:
        ups = _read().get("ups", [])
        if len(ups) > MAX_UPS:
            raise UpListCapacityError("ups", MAX_UPS, len(ups))
    # 老数据带 auto 字段(开关已废除),读时丢弃;quality 统一补默认 ""
    for u in ups:
        u.pop("auto", None)
        # 已下线的档位(480P/360P/1440P)留在存量记录里会变成「界面显示最高画质,
        # 实际按 480P 下载」:前端下拉里没有这个值,浏览器回落显示首项,而
        # best_video_audio_choice 仍按 480P 挑流。读时归一成空串,与
        # list_collections 的 quality 归一保持一致。
        if u.get("quality") not in QUALITY_CHOICES:
            u["quality"] = ""
    return ups


def add_up(mid: int, uname: str = "", face: str = "",
           initial_seen: Optional[dict] = None) -> dict:
    """新增 UP；可把初始游标与列表项作为一次 JSON 事务提交。"""
    with _lock:
        data = _read()
        data.setdefault("ups", [])
        data.setdefault("last_seen", {})
        # 已存在:不重复加,但补齐缺失的名称/头像(存量记录回填)
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                changed = False
                u.pop("auto", None)
                u.setdefault("quality", "")
                if uname and not u.get("uname"):
                    u["uname"] = uname
                    if face:
                        u["face"] = face
                    changed = True
                if initial_seen is not None and str(mid) not in data["last_seen"]:
                    data["last_seen"][str(mid)] = dict(initial_seen)
                    changed = True
                if changed:
                    _commit(data)
                return u
        if len(data["ups"]) >= MAX_UPS:
            raise UpListCapacityError("ups", MAX_UPS, len(data["ups"]), 1)
        u = {"mid": int(mid), "uname": uname, "face": face,
             "added_at": int(time.time()), "quality": ""}
        data["ups"].append(u)
        if initial_seen is not None:
            data["last_seen"][str(mid)] = dict(initial_seen)
        _commit(data)
        return u


def set_quality(mid: int, quality: str) -> bool:
    """设置该 UP 新视频自动下载的默认清晰度("" = 最高画质自动)。"""
    quality = quality if quality in QUALITY_CHOICES else ""
    with _lock:
        data = _read()
        for u in data["ups"]:
            if int(u["mid"]) == int(mid):
                u["quality"] = quality
                _commit(data)
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
                _commit(data)
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
            _commit(data)
        return removed


def get_last_seen(mid: int) -> Optional[dict]:
    with _lock:
        return _read().get("last_seen", {}).get(str(mid))


def get_last_seen_all() -> dict:
    """一次性读回全部游标,键为字符串 mid。

    ``get_last_seen`` 每次调用都要整读一遍 ``up_list.json``。合集订阅加入后
    这份文件会带上每个订阅几百条的已见集合,批量读取方(/api/up-list、
    /api/followings、追更循环)逐 UP 调它就是把文件大小 × UP 数的 JSON
    解析重复 N 次。返回外层浅拷贝,调用方只读不改。
    """
    with _lock:
        return dict(_read().get("last_seen", {}))


def mark_seen(mid: int, bvid: str, ts: int, title: str = "",
              fetched_at: Optional[int] = None) -> None:
    """提交一个已完整覆盖的 ``(ts, bvid)`` 游标成员。

    B 站可能在同一秒发布多条投稿，单个 bvid 不能代表整秒边界；因此相同 ts
    会合并进 ``same_ts_bvids``，更晚 ts 才重置集合。较旧结果不得让游标回退。
    完整扫描提交游标时一并清掉 continuation，避免旧续点污染下一轮。
    """
    try:
        cursor_ts = max(0, int(ts))
    except (TypeError, ValueError):
        cursor_ts = 0
    cursor_bvid = str(bvid or "")
    with _lock:
        data = _read()
        data.setdefault("last_seen", {})
        entry = dict(data["last_seen"].get(str(mid), {}) or {})
        try:
            old_ts = max(0, int(entry.get("ts") or 0))
        except (TypeError, ValueError):
            old_ts = 0

        old_anchor = str(entry.get("bvid") or "")
        same = {str(v) for v in (entry.get("same_ts_bvids") or []) if v}
        if old_anchor:
            same.add(old_anchor)

        if cursor_ts > old_ts:
            entry.update({"bvid": cursor_bvid, "ts": cursor_ts, "title": title})
            same = {cursor_bvid} if cursor_bvid else set()
        elif cursor_ts == old_ts:
            if cursor_bvid:
                same.add(cursor_bvid)
            if not old_anchor and cursor_bvid:
                entry.update({"bvid": cursor_bvid, "title": title})
            entry["ts"] = cursor_ts
        # cursor_ts < old_ts 时只允许刷新 fetched_at，绝不回退高水位。

        entry["same_ts_bvids"] = sorted(same)
        entry.pop("scan", None)
        if fetched_at is not None:
            entry["fetched_at"] = int(fetched_at)
        data["last_seen"][str(mid)] = entry
        _commit(data)


def save_scan_state(mid: int, state: dict) -> None:
    """持久化未完成分页扫描的续点，但不推进已提交游标。"""
    scan = dict(state)
    items = scan.get("items", [])
    if not isinstance(items, list):
        raise ValueError("续扫状态 items 必须是 list")
    if len(items) > MAX_SCAN_ITEMS:
        raise UpListCapacityError(
            "scan_items", MAX_SCAN_ITEMS, len(items))
    with _lock:
        data = _read()
        data.setdefault("last_seen", {})
        entry = dict(data["last_seen"].get(str(mid), {}) or {})
        entry["scan"] = scan
        data["last_seen"][str(mid)] = entry
        _commit(data)


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
        _commit(data)


# ---------- 合集 / 收藏夹订阅(自动下载新入库视频) ----------
#
# 与 UP 订阅并存于同一份 up_list.json,但用独立键,避免改动 ups/last_seen 的
# 既有语义(那份数据结构有续扫游标等复杂状态,不应被这里污染)。
#
# collections: [{kind, id, title, cover, quality, added_at, seen_bvids}]
#   kind = "season"(追更合集) | "fav"(收藏夹)
#   seen_bvids = 已处理过的 bvid 集合(基线 + 后续新增),上限 COLLECTION_SEEN_MAX
#   首次添加时以当前全部条目为基线,之后只处理新出现的 bvid。

COLLECTION_KINDS = ("season", "fav")
MAX_COLLECTIONS = 100
COLLECTION_SEEN_MAX = 300


def _collection_key(kind: str, cid: int) -> str:
    return f"{kind}:{int(cid)}"


def list_collections() -> list[dict]:
    """返回合集/收藏夹订阅列表(quality 统一补默认空串)。"""
    with _lock:
        items = _read().get("collections", [])
        if len(items) > MAX_COLLECTIONS:
            raise UpListCapacityError("collections", MAX_COLLECTIONS, len(items))
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in COLLECTION_KINDS:
            continue
        try:
            cid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        entry = dict(item)
        entry["kind"] = kind
        entry["id"] = cid
        entry.setdefault("quality", "")
        if entry["quality"] not in QUALITY_CHOICES:
            entry["quality"] = ""
        entry.setdefault("seen_bvids", [])
        out.append(entry)
    return out


def add_collection(kind: str, cid: int, title: str = "", cover: str = "",
                   seen_bvids: Optional[list[str]] = None,
                   quality: str = "") -> dict:
    """新增合集/收藏夹订阅;已存在时只回填名称/封面,不重置已见集合。"""
    kind = str(kind or "")
    if kind not in COLLECTION_KINDS:
        raise ValueError(f"非法订阅类型:{kind}")
    cid = int(cid)
    quality = quality if quality in QUALITY_CHOICES else ""
    raw_baseline = [str(v) for v in (seen_bvids or []) if v]
    if len(raw_baseline) > COLLECTION_SEEN_MAX:
        # 与轮询侧保持一致:超限明确失败,不静默截断(截断会让被裁掉的旧条目
        # 在后续检查中被当成新增而重复下载)
        raise UpListCapacityError("collection_items", COLLECTION_SEEN_MAX,
                                  len(raw_baseline), 0)
    baseline = raw_baseline
    with _lock:
        data = _read()
        data.setdefault("collections", [])
        for item in data["collections"]:
            if (str(item.get("kind")) == kind
                    and int(item.get("id") or 0) == cid):
                changed = False
                if title and not item.get("title"):
                    item["title"] = title
                    changed = True
                if cover and not item.get("cover"):
                    item["cover"] = cover
                    changed = True
                item.setdefault("quality", "")
                if changed:
                    _commit(data)
                return dict(item)
        if len(data["collections"]) >= MAX_COLLECTIONS:
            raise UpListCapacityError("collections", MAX_COLLECTIONS,
                                      len(data["collections"]), 1)
        entry = {"kind": kind, "id": cid, "title": title, "cover": cover,
                 "quality": quality, "added_at": int(time.time()),
                 "seen_bvids": baseline}
        data["collections"].append(entry)
        _commit(data)
        return dict(entry)


def remove_collection(kind: str, cid: int) -> bool:
    kind = str(kind or "")
    cid = int(cid)
    with _lock:
        data = _read()
        before = len(data.get("collections", []))
        data["collections"] = [
            item for item in data.get("collections", [])
            if not (str(item.get("kind")) == kind
                    and int(item.get("id") or 0) == cid)
        ]
        removed = len(data["collections"]) < before
        if removed:
            _commit(data)
        return removed


def set_collection_quality(kind: str, cid: int, quality: str) -> bool:
    quality = quality if quality in QUALITY_CHOICES else ""
    kind = str(kind or "")
    cid = int(cid)
    with _lock:
        data = _read()
        for item in data.get("collections", []):
            if (str(item.get("kind")) == kind
                    and int(item.get("id") or 0) == cid):
                item["quality"] = quality
                _commit(data)
                return True
        return False


def reset_collection_seen(kind: str, cid: int) -> bool:
    """清空某合集的已见集合(下次检查会重新以当前内容为基线)。

    用于条目数超过去重上限、或用户想重新对齐基线时。
    """
    kind = str(kind or "")
    cid = int(cid)
    with _lock:
        data = _read()
        for item in data.get("collections", []):
            if (str(item.get("kind")) == kind
                    and int(item.get("id") or 0) == cid):
                item["seen_bvids"] = []
                item["checked_at"] = int(time.time())
                _commit(data)
                return True
        return False


def commit_collection_seen(kind: str, cid: int, bvids: list[str]) -> None:
    """把本轮已处理的 bvid 并入已见集合(去重 + 上限截断,保留最新)。

    传空列表表示「本轮没有新增,但仍检查过」:只更新 checked_at,不动已见集合。
    """
    kind = str(kind or "")
    cid = int(cid)
    fresh = [str(v) for v in bvids if v]
    with _lock:
        data = _read()
        for item in data.get("collections", []):
            if (str(item.get("kind")) == kind
                    and int(item.get("id") or 0) == cid):
                if fresh:
                    seen = [str(v) for v in (item.get("seen_bvids") or []) if v]
                    merged = list(dict.fromkeys([*fresh, *seen]))
                    item["seen_bvids"] = merged[:COLLECTION_SEEN_MAX]
                item["checked_at"] = int(time.time())
                _commit(data)
                return
