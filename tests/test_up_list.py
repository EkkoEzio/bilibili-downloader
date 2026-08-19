"""up_list.py:清单条目 + per-UP 默认清晰度 + last_seen 基线。"""
import json

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import up_list
    monkeypatch.setattr(up_list, "UP_LIST_FILE", tmp_path / "up_list.json")
    return up_list


def test_add_up_defaults_quality_auto(store):
    """新加的 UP 默认清晰度为空串(最高画质自动)。"""
    u = store.add_up(123, "A", "")
    assert u["quality"] == ""


def test_set_quality_persists(store):
    store.add_up(123, "A", "")
    assert store.set_quality(123, "720P") is True
    assert store.list_ups()[0]["quality"] == "720P"
    # 改回自动
    assert store.set_quality(123, "") is True
    assert store.list_ups()[0]["quality"] == ""


def test_set_quality_rejects_unknown_label(store):
    """非法清晰度标签按空串(自动)处理,不写脏数据。"""
    store.add_up(123, "A", "")
    store.set_quality(123, "8K 超清")
    assert store.list_ups()[0]["quality"] == ""


def test_set_quality_unknown_mid_returns_false(store):
    assert store.set_quality(404, "720P") is False


def test_old_records_default_quality_and_drop_auto(store, tmp_path):
    """历史数据没有 quality:读取时补默认 "";废除的 auto 字段读时丢弃。"""
    tmp_path.joinpath("up_list.json").write_text(
        json.dumps({"ups": [{"mid": 7, "uname": "老数据", "face": "", "added_at": 1,
                             "auto": True}],
                    "last_seen": {}}), encoding="utf-8")
    ups = store.list_ups()
    assert ups[0].get("quality") == ""
    assert "auto" not in ups[0], "auto 开关已废除,不再外露"


# ---------- 拉取时间(自适应页大小依据) ----------

def test_mark_seen_stores_fetched_at(store):
    store.mark_seen(1, "BVnew", 200, "标题", fetched_at=1234)
    assert store.get_fetched_at(1) == 1234
    # 不带 fetched_at 调用:保留原值(不清空)
    store.mark_seen(1, "BVnew2", 300, "标题2")
    assert store.get_fetched_at(1) == 1234
    # 从未拉取过 → None
    assert store.get_fetched_at(999) is None


# ---------- 名称回填(2026-08-17:mid 记不住,要显示 UP 名) ----------

def test_set_uname_fills_and_overwrites(store):
    """set_uname 写入 UP 名称(调用方负责只在空时补;显式调用允许改名)。"""
    store.add_up(1, "", "")
    assert store.set_uname(1, "远古野") is True
    assert store.list_ups()[0]["uname"] == "远古野"
    assert store.set_uname(404, "x") is False, "不存在的 UP 返回 False"
