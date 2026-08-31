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


# ---------- 已下载判定的键规则(缺陷 B3:判定粒度必须是 (bvid, page)) ----------

def test_page_from_url():
    """从视频 URL 取分 P 序号;无 `?p=` / 非法值一律视为 1。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)。

    查询侧的 page 只能从 URL 解析(is_downloaded / isDownloaded 的入参只有 URL),
    这个函数就是那一侧的唯一来源。归一到 1 很关键:单 P 视频 URL 不带 `?p=`,
    而它对应的任务 page 是 1 —— 两边都落到 1,3.12「不带 ?p= 的单 P URL 仍判
    已下载」才成立。

    本函数由任务 9.1 引入,在它落地前本用例的守卫断言先挂(守卫在测试体内,
    不会污染其它用例的收集)。

    Validates: Requirements 2.6, 2.7, 3.12
    """
    assert hasattr(extras, "page_from_url"), (
        "extras.page_from_url 尚未引入(任务 9.1):查询侧分 P 解析的唯一来源缺失")

    base = "https://www.bilibili.com/video/BV1xx411c7mD"
    assert extras.page_from_url(f"{base}?p=3") == 3
    assert extras.page_from_url(base) == 1, "无 ?p= 的单 P URL 视为 P1"
    assert extras.page_from_url(f"{base}/") == 1
    assert extras.page_from_url(f"{base}?spm_id_from=333.999&p=2") == 2, "`&p=` 形态也要认"
    assert extras.page_from_url(f"{base}?p=abc") == 1, "非法值兜底 1,不得抛异常"
    assert extras.page_from_url(f"{base}?p=") == 1
    assert extras.page_from_url("") == 1
    assert extras.page_from_url(None) == 1, "None 兜底 1(调用方可能传 t.get('url'))"
    assert extras.page_from_url(f"{base}?p=12") == 12, "多位数分 P 不能被截成 1"


def test_dl_key_format():
    """「已下载」判定的唯一键:(bvid, 分P) → `BVxxxxxxxxxx#p{N}`。

    Property 3: Bug Condition —— 已下载判定的粒度是 (bvid, page)。

    键必须把 page 编进去(只按 bvid 建键就是缺陷 B3),且分隔符走常量
    `DL_KEY_SEP` —— 前端 static/js/util.js 有同名常量,两侧靠
    tests/test_app.py::test_frontend_download_key_shared_rule 绑定比较。
    键规则漂移(例如后端 `f"{bv}:{page}"`、前端 `bv + '#p' + page`)会让角标与
    批量跳过各说各话,且不会报任何错。

    page 缺失 / 0 / None 一律归一为 1:任务表里的 page 字段可能缺失,查询侧
    page_from_url 对无 `?p=` 也返回 1,两侧必须落到同一个键。

    Validates: Requirements 2.6, 2.7, 3.12
    """
    for name in ("DL_KEY_SEP", "dl_key"):
        assert hasattr(extras, name), (
            f"extras.{name} 尚未引入(任务 9.1):已下载判定的键规则唯一来源缺失")

    assert extras.DL_KEY_SEP == "#p", "分隔符是前后端绑定断言的锚点,取值固定为 '#p'"
    assert extras.dl_key("BV1xx411c7mD", 3) == "BV1xx411c7mD#p3"
    assert extras.dl_key("BV1xx411c7mD", 1) == "BV1xx411c7mD#p1"
    assert extras.dl_key("BV1xx411c7mD", 12) == "BV1xx411c7mD#p12"
    assert extras.dl_key("BV1xx411c7mD", 0) == "BV1xx411c7mD#p1", "0 归一为 P1"
    assert extras.dl_key("BV1xx411c7mD", None) == "BV1xx411c7mD#p1", "None 归一为 P1"
    # 键必须由 DL_KEY_SEP 拼出:改常量键跟着改,前后端绑定断言才有意义
    assert extras.dl_key("BV1xx411c7mD", 2) == "BV1xx411c7mD" + extras.DL_KEY_SEP + "2"
    # 不同分 P 必须是不同的键(B3 的核心:同 BV 不同 P 不得互相顶替)
    assert extras.dl_key("BV1xx411c7mD", 1) != extras.dl_key("BV1xx411c7mD", 2)


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
