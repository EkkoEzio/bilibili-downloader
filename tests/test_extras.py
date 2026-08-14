"""extras.py:弹幕 protobuf 解析 / XML 落盘 / bvid 提取。"""
import pytest

import extras


# ---------- 测试用 protobuf 构造器 ----------

def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _ld(num: int, payload: bytes) -> bytes:
    return bytes([(num << 3) | 2]) + _varint(len(payload)) + payload


def _vi(num: int, val: int) -> bytes:
    return bytes([(num << 3) | 0]) + _varint(val)


def _seg(*elems) -> bytes:
    """DmSegMobileReply{ repeated DanmukuElem elems=1 }"""
    return b"".join(_ld(1, e) for e in elems)


def _elem(text="弹幕", progress=1000, mode=1, size=25, color=0xFFFFFF,
          mid_hash="hash", ctime=1700000000, dmid=1):
    return (_vi(1, dmid) + _vi(2, progress) + _vi(3, mode) + _vi(4, size)
            + _vi(5, color) + _ld(6, mid_hash.encode()) + _ld(7, text.encode())
            + _vi(8, ctime))


# ---------- bvid 提取 ----------

def test_bvid_from_url():
    assert extras.bvid_from_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2") == "BV1xx411c7mD"
    assert extras.bvid_from_url("https://www.bilibili.com/video/BV1GJ411x7h7/") == "BV1GJ411x7h7"
    assert extras.bvid_from_url("https://b23.tv/abc123") is None
    assert extras.bvid_from_url("https://example.com/nothing") is None


# ---------- protobuf 解析 ----------

def test_parse_proto_extracts_fields():
    data = _seg(_elem(text="前方高能", progress=69960, color=0xFF0000),
                _elem(text="23333", progress=95430))
    out = extras.parse_danmaku_proto(data)
    assert len(out) == 2
    assert out[0]["text"] == "前方高能"
    assert out[0]["progress_ms"] == 69960
    assert out[0]["color"] == 0xFF0000
    assert out[0]["mode"] == 1 and out[0]["fontsize"] == 25
    assert out[1]["text"] == "23333"


def test_parse_proto_empty():
    assert extras.parse_danmaku_proto(b"") == []


def test_parse_proto_skips_special_danmaku_without_text():
    """高级弹幕/代码弹幕没有 content 字段(7),跳过但不炸。"""
    data = _seg(_vi(1, 9) + _vi(2, 500) + _ld(10, b'{"fill_color":"x"}'))
    out = extras.parse_danmaku_proto(data)
    assert out == [] or all("text" in e for e in out)


# ---------- XML 输出 ----------

def test_to_xml_escapes_and_counts():
    xml = extras.to_xml([
        {"progress_ms": 1000, "mode": 1, "fontsize": 25, "color": 16777215,
         "ctime": 1700000000, "mid_hash": "h", "text": 'a<b>&"c', "id": 1, "weight": 0},
        {"progress_ms": 2000, "mode": 4, "fontsize": 18, "color": 0,
         "ctime": 1700000001, "mid_hash": "h", "text": "普通弹幕", "id": 2, "weight": 0},
    ])
    assert xml.count("<d ") == 2
    assert "&lt;b&gt;&amp;" in xml          # 转义生效
    assert xml.startswith("<?xml") and xml.rstrip().endswith("</i>")
    assert "普通弹幕" in xml


# ---------- 保存流程(网络层打桩) ----------

def test_save_danmaku_for_writes_xml(tmp_path, monkeypatch):
    media = tmp_path / "测试视频.mp4"
    media.write_bytes(b"x")
    monkeypatch.setattr(extras, "_cid_for", lambda bvid, page: 12345)
    monkeypatch.setattr(extras, "_fetch_segment", lambda cid, seg, bvid: _seg(_elem(text="哈哈")) if seg == 1 else b"")
    n = extras.save_danmaku_for("https://www.bilibili.com/video/BV1xx411c7mD", media)
    assert n == 1
    out = tmp_path / "测试视频.xml"
    assert out.exists()
    assert "哈哈" in out.read_text(encoding="utf-8")


def test_save_danmaku_for_no_bvid_returns_none(tmp_path):
    assert extras.save_danmaku_for("https://b23.tv/xx", tmp_path / "a.mp4") is None
    assert not (tmp_path / "a.xml").exists()


def test_save_danmaku_for_network_failure_returns_none(tmp_path, monkeypatch):
    media = tmp_path / "v.mp4"
    media.write_bytes(b"x")

    def boom(cid, seg, bvid):
        raise RuntimeError("network down")

    monkeypatch.setattr(extras, "_cid_for", lambda bvid, page: 1)
    monkeypatch.setattr(extras, "_fetch_segment", boom)
    assert extras.save_danmaku_for("https://www.bilibili.com/video/BV1xx411c7mD", media) is None
