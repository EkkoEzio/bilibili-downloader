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
