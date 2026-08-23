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

log = logging.getLogger("bd.extras")

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

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


def bvid_from_url(url: str) -> Optional[str]:
    """从视频 URL 提取 BV 号;提取不到(短链/av号)返回 None。"""
    m = _BV_RE.search(url or "")
    return m.group(1) if m else None


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
    """取视频(指定分P)的 cid。公开接口,无需登录。"""
    r = requests.get("https://api.bilibili.com/x/player/pagelist",
                     params={"bvid": bvid}, headers=BROWSER_HEADERS,
                     cookies=bilibili_auth.get_cookiejar(), timeout=10)
    data = r.json()
    if data.get("code") != 0:
        return None
    pages = data.get("data") or []
    if not pages:
        return None
    idx = min(max(page - 1, 0), len(pages) - 1)
    return pages[idx].get("cid")


def _fetch_segment(cid: int, segment_index: int, bvid: str) -> bytes:
    r = requests.get("https://api.bilibili.com/x/v2/dm/web/seg.so",
                     params={"type": 1, "oid": cid, "segment_index": segment_index},
                     headers={**BROWSER_HEADERS,
                              "Referer": f"https://www.bilibili.com/video/{bvid}"},
                     cookies=bilibili_auth.get_cookiejar(), timeout=10)
    return r.content if r.status_code == 200 else b""


def fetch_danmaku(bvid: str, page: int = 1) -> list[dict]:
    """抓取全部弹幕(每段 6 分钟,逐段拉到空为止,36 段封顶)。"""
    cid = _cid_for(bvid, page)
    if not cid:
        return []
    out = []
    for seg in range(1, 37):
        elems = parse_danmaku_proto(_fetch_segment(cid, seg, bvid))
        if not elems:
            break
        out.extend(elems)
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
