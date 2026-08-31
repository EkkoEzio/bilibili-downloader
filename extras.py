"""附件处理:bvid 提取、弹幕抓取(seg.so protobuf)与 XML 落盘。

弹幕来源是 B 站分段的 protobuf 接口(经典 list.so 已 400),这里做最小
wire-format 解析,转成老式 B 站 XML(`<d p="...">text</d>`),PotPlayer/
IINA 弹幕插件可直接加载。全部为尽力而为:失败记日志、不影响下载本身。
"""
import logging
import re
from typing import Optional
from xml.sax.saxutils import escape

import requests

import bilibili_auth
from bilibili_auth import BROWSER_HEADERS
from request_governor import governed_get, governor

log = logging.getLogger("bd.extras")

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
# pagelist 同一响应已携带分 P 时长；按 6 分钟/段精确控制弹幕请求数量，
# 避免把“本段无普通弹幕”误判为视频结束，也避免固定扫满 36 段。
_CID_SEGMENT_COUNTS: dict[int, int] = {}

# Windows 保留设备名(不带扩展名时整名命中,带扩展名时首个点前段命中)
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str) -> str:
    """清洗成跨平台安全文件名:非法字符→_、空白合并、去首尾空白与点。

    下载文件名的唯一权威清洗入口(downloader 的 outtmpl 与改名对齐共用,
    保证"任务 title → 期望文件名"两端走同一实现)。空名兜底 video。
    """
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "video"
    if name.upper() in _WIN_RESERVED or name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name


def build_filename(title: str, page: int = 1) -> str:
    """任务权威 title → 期望的落盘文件名 stem(不含目录/扩展名)。

    outtmpl 组装(A.2)、下载后改名对齐(A.3)与 taskstore 的文件定位共用本函数,
    保证三端算出的期望名恒一致。多 P 加 ` - P{n}` 后缀。

    放在这里(而不是 files.py)是分层需要:taskstore 定位文件也要用它,
    而 files.py 依赖 taskstore——纯命名逻辑必须待在依赖链底部。
    """
    stem = sanitize_filename(title)
    return f"{stem} - P{int(page)}" if page and int(page) > 1 else stem


def bvid_from_url(url: str) -> Optional[str]:
    """从视频 URL 提取 BV 号;提取不到(短链/av号)返回 None。"""
    m = _BV_RE.search(url or "")
    return m.group(1) if m else None


# ---------- 已下载判定的键规则((bvid, 分P) 粒度) ----------
#
# 放在这里(而不是 taskstore.py)与 bvid_from_url / build_filename 同层:
# taskstore 已经 import extras,auto_refresh 经 downloader 间接复用同一份,
# 前端语义也以此为基准。纯字符串逻辑必须待在依赖链底部。
#
# 两侧取 page 的方式是刻意不对称的,改动前务必读懂:
#   - 查询侧(is_downloaded / 前端 isDownloaded)入参只有 URL,page 只能从
#     ?p= 解析 → 用 page_from_url;
#   - 任务侧读 t["page"],不能解析 t["url"] —— start_download(url, label, page=3)
#     存进任务表的 url 是用户粘贴的原始链接(通常不带 ?p=),page 才是权威值。
# 写反任一侧都会让判定悄悄退回「只看 BV」的缺陷形态。

# 已下载判定的键分隔符。前端 static/js/util.js 的 DL_KEY_SEP 必须与此一致,
# tests 有绑定断言 —— 两侧键规则漂移会让角标与批量跳过各说各话,且不会报错。
DL_KEY_SEP = "#p"

_PAGE_RE = re.compile(r"[?&]p=(\d+)")


def page_from_url(url: str) -> int:
    """从视频 URL 取分 P 序号;无 ?p= / 非法值视为 1。

    URL 形态:https://www.bilibili.com/video/{BV}?p={N};带其它查询参数时
    分 P 以 `&p=` 出现(如 ?spm_id_from=333.999&p=2),所以 `[?&]` 两种都认。
    归一到 1 是 3.12 的前提:单 P 视频 URL 不带 ?p=,其任务 page 是 1,
    两边都落到 1,「不带 ?p= 的单 P URL 仍判已下载」才成立。
    """
    m = _PAGE_RE.search(url or "")
    return int(m.group(1)) if m else 1


def dl_key(bvid: str, page: int) -> str:
    """「已下载」判定的唯一键:(bvid, 分P) → "BVxxxxxxxxxx#p1"。

    判定粒度必须是 (bvid, page):只看 bvid 会让合集下完 P1 后其余分 P
    全被当已下载而跳过,自动下载也漏下新分 P。

    page 缺失 / 0 / None 一律归一为 1,写法必须是 `int(page or 1)`:
    `int(page) or 1` 会先对 None 调 int() 直接抛 TypeError,or 来不及兜底。
    """
    return f"{bvid}{DL_KEY_SEP}{int(page or 1)}"


# ---------- 最小 protobuf wire-format 解析 ----------

def _read_varint(buf: bytes, i: int):
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _fields(buf: bytes):
    """产出 (field_number, value);value 为 int(varint)或 bytes(长度分隔)。"""
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        num, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
            yield num, v
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            yield num, buf[i:i + ln]
            i += ln
        elif wt == 5:
            yield num, buf[i:i + 4]
            i += 4
        elif wt == 1:
            yield num, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"unsupported wire type {wt}")


def parse_danmaku_proto(data: bytes) -> list[dict]:
    """解析 DmSegMobileReply{repeated DanmakuElem elems=1}。

    DanmakuElem 关键字段:id=1, progress=2(毫秒), mode=3, fontsize=4,
    color=5, midHash=6, content=7, ctime=8, weight=9。没有 content 的
    (高级/代码弹幕)跳过。
    """
    out = []
    try:
        for num, val in _fields(data):
            if num != 1 or not isinstance(val, bytes):
                continue
            d = {}
            for n2, v2 in _fields(val):
                if isinstance(v2, bytes):
                    if n2 in (6, 7):
                        d[n2] = v2.decode("utf-8", "replace")
                elif n2 in (1, 2, 3, 4, 5, 8, 9):
                    d[n2] = v2
            if 7 in d:
                out.append({
                    "id": d.get(1, 0),
                    "progress_ms": d.get(2, 0),
                    "mode": d.get(3, 1),
                    "fontsize": d.get(4, 25),
                    "color": d.get(5, 0xFFFFFF),
                    "mid_hash": d.get(6, ""),
                    "text": d[7],
                    "ctime": d.get(8, 0),
                    "weight": d.get(9, 0),
                })
    except (ValueError, IndexError):
        pass
    return out


def to_xml(elems: list[dict]) -> str:
    """转成经典 B 站弹幕 XML 格式。"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             "<i>", "<chatserver>chat.bilibili.com</chatserver>",
             "<mission>0</mission>", "<source>k-v</source>"]
    for e in elems:
        p = (f"{e['progress_ms']},{e['mode']},{e['fontsize']},{e['color']},"
             f"{e['ctime']},0,{e['mid_hash']},{e['id']}")
        parts.append(f'<d p="{p}">{escape(e["text"])}</d>')
    parts.append("</i>")
    return "".join(parts)


# ---------- 网络层 ----------

def _cid_for(bvid: str, page: int) -> Optional[int]:
    """取视频(指定分P)的 cid，并缓存同响应中的弹幕分段数量。"""
    endpoint = "https://api.bilibili.com/x/player/pagelist"
    r = governed_get(
        requests.get, endpoint, kind="default", params={"bvid": bvid},
        headers=BROWSER_HEADERS, cookies=bilibili_auth.get_cookiejar(), timeout=10)
    data = r.json()
    governor.note_api_code(data.get("code"), endpoint=endpoint)
    if data.get("code") != 0:
        return None
    pages = data.get("data") or []
    if not pages:
        return None
    idx = min(max(page - 1, 0), len(pages) - 1)
    selected = pages[idx]
    cid = selected.get("cid")
    duration = int(selected.get("duration") or 0)
    if cid and duration > 0:
        # 每段覆盖 360 秒；36 段是项目原有的 3.6 小时保护上限。
        _CID_SEGMENT_COUNTS[int(cid)] = min(36, max(1, (duration + 359) // 360))
        if len(_CID_SEGMENT_COUNTS) > 256:
            _CID_SEGMENT_COUNTS.pop(next(iter(_CID_SEGMENT_COUNTS)))
    return cid


def _fetch_segment(cid: int, segment_index: int, bvid: str) -> bytes:
    endpoint = "https://api.bilibili.com/x/v2/dm/web/seg.so"
    r = governed_get(
        requests.get, endpoint, kind="bulk",
        params={"type": 1, "oid": cid, "segment_index": segment_index},
        headers={**BROWSER_HEADERS,
                 "Referer": f"https://www.bilibili.com/video/{bvid}"},
        cookies=bilibili_auth.get_cookiejar(), timeout=10)
    return r.content if r.status_code == 200 else b""


def fetch_danmaku(bvid: str, page: int = 1) -> list[dict]:
    """按视频时长抓取弹幕分片；未知时长才用首个空响应作为保守终点。"""
    cid = _cid_for(bvid, page)
    if not cid:
        return []
    segment_count = _CID_SEGMENT_COUNTS.get(int(cid))
    out = []
    for seg in range(1, (segment_count or 36) + 1):
        payload = _fetch_segment(cid, seg, bvid)
        elems = parse_danmaku_proto(payload)
        if elems:
            out.extend(elems)
        # 正常路径已从 pagelist 得到时长：即使中间分片无弹幕也继续，防止漏掉
        # 后续时间段；测试桩/旧响应无时长时保留空响应终止，避免扫满 36 次。
        elif segment_count is None and not payload:
            break
    return out


def save_danmaku_for(url: str, media_path, page: int = 1) -> Optional[int]:
    """把弹幕存成与视频同名的 .xml。返回条数;无 BV 号/失败返回 None,空弹幕返回 0。"""
    bvid = bvid_from_url(url)
    if not bvid:
        return None
    try:
        elems = fetch_danmaku(bvid, page)
        if not elems:
            return 0
        xml_path = media_path.with_suffix(".xml")
        xml_path.write_text(to_xml(elems), encoding="utf-8")
        log.info("弹幕已保存 %s(%d 条)", xml_path.name, len(elems))
        return len(elems)
    except Exception:
        log.exception("弹幕保存失败 %s", url)
        return None
