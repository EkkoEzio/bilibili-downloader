"""config.py 新增可配项(2026-08-15 批次):保存开关、归档、通知、追更间隔。

注意:不能用 importlib.reload 隔离配置文件路径——reload 会重新执行模块
顶层代码,把补丁过的 CONFIG_FILE 又指回真实路径,导致测试写坏真实配置
(踩过一次)。这里改为:导入后直接替换 CONFIG_FILE 属性(不发 reload),
默认值通过常量断言,持久化通过读取写出的文件断言。
"""
import json

import pytest


@pytest.fixture()
def fresh_config(tmp_path, monkeypatch):
    import config as config_module
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    yield config_module


def test_defaults_match_documented_values(fresh_config):
    c = fresh_config
    assert c._DEFAULT_SAVE_DANMAKU is False
    assert c._DEFAULT_SAVE_SUBTITLE is False
    assert c._DEFAULT_SAVE_COVER is False
    assert c._DEFAULT_SORT_BY_UP is False
    assert c._DEFAULT_NOTIFY is True
    assert c._DEFAULT_AUTO_REFRESH_MINUTES == 30
    assert c.AUTO_REFRESH_MIN >= 10, "追更间隔下限不得低于 10 分钟(防风控)"


def test_set_and_persist(fresh_config):
    r = fresh_config.set_config({
        "save_danmaku": True, "save_subtitle": True, "save_cover": True,
        "sort_by_up": True, "notify": False, "auto_refresh_minutes": 60,
    })
    assert r["ok"]
    c = fresh_config.get_config()
    assert c["save_danmaku"] is True and c["save_subtitle"] is True
    assert c["save_cover"] is True and c["sort_by_up"] is True
    assert c["notify"] is False and c["auto_refresh_minutes"] == 60
    # 持久化:直接看写出的文件
    on_disk = json.loads(fresh_config.CONFIG_FILE.read_text(encoding="utf-8"))
    assert on_disk["auto_refresh_minutes"] == 60
    assert on_disk["save_danmaku"] is True


def test_auto_refresh_minutes_clamped(fresh_config):
    """追更间隔有下限(防风控),过小/过大都会被截到边界。"""
    fresh_config.set_config({"auto_refresh_minutes": 1})
    assert fresh_config.get_config()["auto_refresh_minutes"] == fresh_config.AUTO_REFRESH_MIN
    fresh_config.set_config({"auto_refresh_minutes": 99999})
    assert fresh_config.get_config()["auto_refresh_minutes"] == fresh_config.AUTO_REFRESH_MAX


def test_invalid_values_rejected_without_side_effect(fresh_config):
    before = fresh_config.get_config()
    r = fresh_config.set_config({"auto_refresh_minutes": "abc"})
    assert not r["ok"]
    assert fresh_config.get_config() == before
    assert not fresh_config.CONFIG_FILE.exists(), "失败不得写盘"


def test_bool_coercion(fresh_config):
    """前端可能送 true/false 之外的值(勾选框),一律按真假规整。"""
    r = fresh_config.set_config({"save_danmaku": "yes"})
    assert r["ok"]
    assert fresh_config.get_config()["save_danmaku"] is True
    r = fresh_config.set_config({"save_danmaku": 0})
    assert r["ok"]
    assert fresh_config.get_config()["save_danmaku"] is False


def test_empty_dir_rejected(fresh_config):
    """空串目录会静默解析成当前目录(Path("")),下载会全部落进应用源码树。"""
    r = fresh_config.set_config({"download_dir": "   "})
    assert not r["ok"]
