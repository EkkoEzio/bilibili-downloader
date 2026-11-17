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
    """新分辨率档原样落盘;旧精确标签写入时自动归一(2026-11-17 档位改版)。"""
    store.add_up(123, "A", "")
    assert store.set_quality(123, "720") is True
    assert store.list_ups()[0]["quality"] == "720"
    # 旧精确标签(720P/1080P60 等)归一到分辨率档
    assert store.set_quality(123, "1080P60") is True
    assert store.list_ups()[0]["quality"] == "1080"
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


def test_old_removed_quality_label_normalised_on_read(store, tmp_path):
    """存量旧档位读时归一到当前档位体系(2026-11-17 纯分辨率改版)。

    已下线档位(480P/360P)没有对应分辨率档 → 归一成空串(最高画质自动);
    旧精确标签(1080P60)映射到对应分辨率档(1080)——否则界面显示一个档位、
    实际按另一个挑流。与前端 subscriptions.js 的 _normQuality 同一套映射;
    合集/收藏订阅的旧精确标签原样保留(见 COLLECTION_QUALITY_CHOICES)。
    """
    tmp_path.joinpath("up_list.json").write_text(
        json.dumps({"ups": [{"mid": 8, "uname": "旧档位", "face": "",
                             "added_at": 1, "quality": "480P"},
                            {"mid": 9, "uname": "更旧档位", "face": "",
                             "added_at": 2, "quality": "360P"},
                            {"mid": 10, "uname": "合法档位", "face": "",
                             "added_at": 3, "quality": "1080P60"}],
                    "last_seen": {}}), encoding="utf-8")
    by_mid = {int(u["mid"]): u["quality"] for u in store.list_ups()}
    assert by_mid[8] == "", "已下线的 480P 必须归一成最高画质自动"
    assert by_mid[9] == "", "已下线的 360P 必须归一成最高画质自动"
    assert by_mid[10] == "1080", "旧精确标签 1080P60 归一到 1080 分辨率档"


# ---------- 拉取时间(自适应页大小依据) ----------

def test_mark_seen_stores_fetched_at(store):
    store.mark_seen(1, "BVnew", 200, "标题", fetched_at=1234)
    assert store.get_fetched_at(1) == 1234
    # 不带 fetched_at 调用:保留原值(不清空)
    store.mark_seen(1, "BVnew2", 300, "标题2")
    assert store.get_fetched_at(1) == 1234
    # 从未拉取过 → None
    assert store.get_fetched_at(999) is None


def test_get_last_seen_all_returns_full_cursor_map(store):
    """批量读取方一次拿全量游标,键为字符串 mid,与 get_last_seen 逐条一致。"""
    store.mark_seen(1, "BVa", 100, "甲", fetched_at=11)
    store.mark_seen(2, "BVb", 200, "乙", fetched_at=22)
    got = store.get_last_seen_all()
    assert set(got) == {"1", "2"}
    assert got["1"] == store.get_last_seen(1)
    assert got["2"] == store.get_last_seen(2)
    # 空清单返回空 dict 而不是 None,调用方不用再判空
    assert isinstance(got, dict)


# ---------- 名称回填(2026-08-17:mid 记不住,要显示 UP 名) ----------

def test_set_uname_fills_and_overwrites(store):
    """set_uname 写入 UP 名称(调用方负责只在空时补;显式调用允许改名)。"""
    store.add_up(1, "", "")
    assert store.set_uname(1, "远古野") is True
    assert store.list_ups()[0]["uname"] == "远古野"
    assert store.set_uname(404, "x") is False, "不存在的 UP 返回 False"


def test_quality_choices_single_source():
    """精确标签下拉的单一来源(2026-11-17 档位改版后两套档位并存):

    UP 订阅的「纯分辨率档」绑定(up_list.QUALITY_CHOICES ↔ subscriptions.js
    UP_QUALITY_SEGMENTS)在 test_app.py 的分段契约测试里,这里不重复。
    本测试绑「精确标签」一侧——手动下载/合集/收藏共用:
    quality.js QUALITY_OPTIONS ↔ index.html 操作条静态 <option> ↔
    后端 up_list.COLLECTION_QUALITY_CHOICES(合集下拉的值全部来自
    QUALITY_OPTIONS,后端只接受同一集合)。改任何一侧漏改在这里炸。"""
    import re
    from pathlib import Path
    import up_list
    base = Path(__file__).resolve().parent.parent

    js = (base / "static" / "js" / "quality.js").read_text(encoding="utf-8")
    pairs = re.findall(r"\['([^']*)',\s*'([^']*)'\]", js)
    assert pairs, "quality.js 的 QUALITY_OPTIONS 解析不到(改格式要同步改本测试)"
    js_values = {v for v, _ in pairs}

    html = (base / "templates" / "index.html").read_text(encoding="utf-8")
    i = html.find('id="ab-quality"')
    assert i > 0, "index.html 操作条缺少清晰度下拉"
    bar = html[i:html.find("</select>", i)]
    html_values = set(re.findall(r'<option value="([^"]*)"', bar))

    assert js_values == html_values, f"quality.js 与操作条静态 HTML 不一致: {js_values ^ html_values}"
    assert js_values == set(up_list.COLLECTION_QUALITY_CHOICES), \
        f"quality.js 与合集/收藏后端档位表不一致: {js_values ^ set(up_list.COLLECTION_QUALITY_CHOICES)}"


# ---------- 合集/收藏夹订阅 ----------

def test_collection_add_list_remove_roundtrip(store):
    """合集订阅增删查;已存在时不重置已见集合(避免重复下载历史内容)。"""
    import up_list
    entry = up_list.add_collection("season", 123, title="某合集",
                                   seen_bvids=["BV1", "BV2"], quality="1080P")
    assert entry["kind"] == "season" and entry["id"] == 123
    assert entry["seen_bvids"] == ["BV1", "BV2"]
    assert entry["quality"] == "1080P"

    # 重复添加:只回填缺失字段,不动 seen
    again = up_list.add_collection("season", 123, title="新名字",
                                   seen_bvids=["BV9"])
    assert again["seen_bvids"] == ["BV1", "BV2"], "重复添加不得重置已见集合"

    listed = up_list.list_collections()
    assert len(listed) == 1 and listed[0]["id"] == 123

    assert up_list.remove_collection("season", 123) is True
    assert up_list.list_collections() == []


def test_collection_quality_validation(store):
    """清晰度非法值归一为空串;与 UP 订阅共用同一档位表。"""
    import up_list
    up_list.add_collection("fav", 456, title="收藏夹")
    assert up_list.set_collection_quality("fav", 456, "1080P60") is True
    assert up_list.list_collections()[0]["quality"] == "1080P60"
    up_list.set_collection_quality("fav", 456, "1440P")  # 不存在的档位
    assert up_list.list_collections()[0]["quality"] == ""


def test_collection_seen_dedup_and_cap(store):
    """已处理 bvid 去重、保留最新,且不超过上限。"""
    import up_list
    up_list.add_collection("season", 789, seen_bvids=["BVold"])
    up_list.commit_collection_seen("season", 789, ["BVnew", "BVold"])
    seen = up_list.list_collections()[0]["seen_bvids"]
    assert seen[0] == "BVnew" and "BVold" in seen, "新条目在前且去重"
    assert len(seen) == 2


def test_collection_commit_empty_updates_checked_at_only(store):
    """无新增的检查轮:只更新 checked_at,不动已见集合。

    回归:旧实现传空列表直接 return,`checked_at` 永远停在第一次建立基线的时刻,
    「本轮检查过」这件事没有任何落盘证据。轮询侧靠它区分「没查」与「查了没新增」。
    """
    import up_list
    up_list.add_collection("season", 999, seen_bvids=["BV1"])
    before = up_list.list_collections()[0].get("checked_at") or 0

    up_list.commit_collection_seen("season", 999, [])

    entry = up_list.list_collections()[0]
    assert entry["seen_bvids"] == ["BV1"], "空提交不得改动已见集合"
    assert (entry.get("checked_at") or 0) > before, "空提交必须更新 checked_at"


def test_read_tolerates_hand_edited_file_missing_keys(store):
    """手改/极旧遗留的合法 JSON 缺键:写函数不得 KeyError 变 500。

    回归:_read 的 default 只在文件缺失/损坏时生效,缺 ups/last_seen 键时
    set_quality/remove_up 直接 data["ups"] 会 KeyError。
    """
    store.UP_LIST_FILE.write_text(
        json.dumps({"ups": [{"mid": 5, "uname": "甲", "face": "", "added_at": 1}]}),
        encoding="utf-8")
    assert store.set_quality(5, "1080P") is True

    store.UP_LIST_FILE.write_text(json.dumps({"last_seen": {}}), encoding="utf-8")
    assert store.remove_up(5) is False

    # 连对象都不是的合法 JSON:回落到空清单,不抛
    store.UP_LIST_FILE.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert store.list_ups() == []
