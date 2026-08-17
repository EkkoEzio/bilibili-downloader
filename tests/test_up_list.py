"""up_list.py:auto(订阅追更)字段与 set_auto。"""
import json

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import up_list
    monkeypatch.setattr(up_list, "UP_LIST_FILE", tmp_path / "up_list.json")
    return up_list


def test_add_up_defaults_auto_off(store):
    u = store.add_up(123, "A", "")
    assert u["auto"] is False


def test_set_auto_toggles_and_persists(store):
    store.add_up(123, "A", "")
    assert store.set_auto(123, True) is True
    ups = store.list_ups()
    assert ups[0]["auto"] is True
    assert store.set_auto(123, False) is True
    assert store.list_ups()[0]["auto"] is False


def test_old_records_without_auto_treated_as_off(store, tmp_path):
    """历史数据没有 auto 字段:读取时补默认 False,不炸。"""
    tmp_path.joinpath("up_list.json").write_text(
        json.dumps({"ups": [{"mid": 7, "uname": "老数据", "face": "", "added_at": 1}],
                    "last_seen": {}}), encoding="utf-8")
    ups = store.list_ups()
    assert ups[0].get("auto") is False
    assert store.set_auto(7, True)  # 老记录也能开
    assert store.list_ups()[0]["auto"] is True


def test_set_auto_unknown_mid_returns_false(store):
    assert store.set_auto(404, True) is False


# ---------- 拉取时间 + 本地历史(2026-08-16 批次) ----------

def test_mark_seen_stores_fetched_at(store):
    store.mark_seen(1, "BVnew", 200, "标题", fetched_at=1234)
    assert store.get_fetched_at(1) == 1234
    # 不带 fetched_at 调用:保留原值(不清空)
    store.mark_seen(1, "BVnew2", 300, "标题2")
    assert store.get_fetched_at(1) == 1234
    # 从未拉取过 → None
    assert store.get_fetched_at(999) is None


def test_history_empty_by_default(store):
    assert store.list_history() == []


def test_add_history_stamps_found_at(store):
    store.add_history([{"bvid": "BV1", "title": "a", "view_at": 100, "url": "u1"}])
    h = store.list_history()
    assert len(h) == 1
    assert h[0]["bvid"] == "BV1"
    assert h[0]["found_at"] > 0, "历史项应记录发现时间"


def test_add_history_dedups_by_bvid(store):
    store.add_history([{"bvid": "BV1", "view_at": 100, "url": "u"},
                       {"bvid": "BV2", "view_at": 200, "url": "u"}])
    store.add_history([{"bvid": "BV1", "view_at": 100, "url": "u"},
                       {"bvid": "BV3", "view_at": 300, "url": "u"}])
    h = store.list_history()
    assert [x["bvid"] for x in h] == ["BV3", "BV2", "BV1"], "按发布时间倒序且去重"


def test_history_caps_at_100(store):
    assert store.HISTORY_CAP == 100
    batch = [{"bvid": f"BV{i:03d}", "view_at": i, "url": "u"} for i in range(105)]
    store.add_history(batch)
    h = store.list_history()
    assert len(h) == 100, "超过上限应裁掉最旧的"
    assert h[0]["bvid"] == "BV104" and h[-1]["bvid"] == "BV005", "保留最新 100 条"


# ---------- 名称回填(2026-08-17:mid 记不住,要显示 UP 名) ----------

def test_set_uname_fills_and_overwrites(store):
    """set_uname 写入 UP 名称(调用方负责只在空时补;显式调用允许改名)。"""
    store.add_up(1, "", "")
    assert store.set_uname(1, "远古野") is True
    assert store.list_ups()[0]["uname"] == "远古野"
    assert store.set_uname(404, "x") is False, "不存在的 UP 返回 False"
