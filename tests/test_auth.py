import json
import pytest
import yt_dlp
import bilibili_auth
import bilibili_api
from bilibili_auth import parse_poll_response, POLL_CODES, save_session, load_session, clear_session


def test_poll_codes_constants():
    assert POLL_CODES["86101"] == "waiting"
    assert POLL_CODES["86090"] == "scanned"
    assert POLL_CODES["86038"] == "expired"
    assert POLL_CODES["0"] == "success"


def test_parse_waiting():
    r = parse_poll_response({"data": {"code": 86101}}, "")
    assert r["status"] == "waiting"
    assert r["sessdata"] == ""
    assert r["message"] == "未扫码"


def test_parse_scanned():
    r = parse_poll_response({"data": {"code": 86090}}, "")
    assert r["status"] == "scanned"


def test_parse_expired():
    r = parse_poll_response({"data": {"code": 86038}}, "")
    assert r["status"] == "expired"


def test_parse_success_extracts_sessdata_from_url():
    # 真实 B 站响应:SESSDATA 在 data.url 的 query 参数里(主来源,最可靠)
    body = {"code": 0, "data": {
        "code": 0,
        "url": "https://passport.biligame.com/x/passport-login/web/crossDomain?DedeUserID=123&DedeUserID__ckMd5=abc&Expires=999&SESSDATA=url_sess&gourl=/"}}
    r = parse_poll_response(body, "")
    assert r["status"] == "success"
    assert r["sessdata"] == "url_sess"
    assert r["dede_userid"] == "123"


def test_parse_success_extracts_sessdata_from_setcookie_fallback():
    # 兜底:url 里没有时,从 Set-Cookie 头提取
    body = {"code": 0, "data": {"code": 0, "url": "https://x?gourl=/"}}
    set_cookie = "SESSDATA=cookie_sess; Path=/; HttpOnly"
    r = parse_poll_response(body, set_cookie)
    assert r["status"] == "success"
    assert r["sessdata"] == "cookie_sess"


def test_save_load_clear_session(tmp_path, monkeypatch):
    """会话保存、v1→v2 迁移及状态丢失/损坏时的 incarnation 旋转。"""
    import bilibili_auth
    import bilibili_api
    import pagecache

    monkeypatch.setattr(bilibili_auth, "SESSION_COOKIE_FILE", tmp_path / "sess.json")
    monkeypatch.setattr(bilibili_auth, "COOKIE_FILE", tmp_path / "cookies.txt")

    def reset_context_cache():
        with bilibili_auth._session_lock:
            bilibili_auth._auth_state_cache.update(path=None, context=None)
            bilibili_auth._cj_cache = {"cj": None, "ts": 0.0, "context": None}

    reset_context_cache()
    assert bilibili_auth.save_session("SDATA", "123", "jct") is True
    loaded = bilibili_auth.load_session()
    assert loaded["SESSDATA"] == "SDATA"
    assert loaded["DedeUserID"] == "123"

    context = bilibili_auth.get_auth_context()
    assert context["version"] == 2
    assert len(context["incarnation"]) == 32
    public = bilibili_auth._public_context_fields(context)
    assert public["auth_incarnation"] == context["incarnation"]
    cache_path = pagecache._cache_path("fav_folders", context)
    assert f"i-{context['incarnation']}" in str(cache_path)
    assert pagecache.save("fav_folders", {"ok": True}, context=context)
    assert pagecache.load("fav_folders", context=context)["data"] == {"ok": True}

    # 合法 v1 保留身份与 generation，但获得新 incarnation 并原子升级到 v2；
    # 因此旧的 fingerprint/generation 缓存目录不会被重新认领。
    state_file = bilibili_auth._auth_state_file()
    legacy = {
        "version": 1,
        "source": context["source"],
        "fingerprint": context["fingerprint"],
        "generation": 7,
        "authenticated": True,
    }
    state_file.write_text(json.dumps(legacy), encoding="utf-8")
    reset_context_cache()
    migrated = bilibili_auth.get_auth_context()
    assert migrated["source"] == legacy["source"]
    assert migrated["fingerprint"] == legacy["fingerprint"]
    assert migrated["generation"] == legacy["generation"]
    assert migrated["authenticated"] is True
    assert migrated["incarnation"] != context["incarnation"]
    assert json.loads(state_file.read_text(encoding="utf-8"))["version"] == 2
    assert pagecache.load("fav_folders", context=migrated) is None

    # generation 只在 incarnation 内有意义；状态缺失或不可恢复时从新生命期开始。
    state_file.unlink()
    reset_context_cache()
    after_missing = bilibili_auth.get_auth_context()
    assert after_missing["incarnation"] != migrated["incarnation"]
    assert after_missing["generation"] == 1

    state_file.write_text("{broken", encoding="utf-8")
    reset_context_cache()
    after_corrupt = bilibili_auth.get_auth_context()
    assert after_corrupt["incarnation"] != after_missing["incarnation"]
    assert after_corrupt["generation"] == 1

    other_incarnation = ("0" * 32 if after_corrupt["incarnation"] != "0" * 32
                         else "1" * 32)
    same_generation_other_life = dict(
        after_corrupt, incarnation=other_incarnation)
    assert not bilibili_auth._same_context(
        after_corrupt, same_generation_other_life)
    assert (bilibili_api._context_key(after_corrupt)
            != bilibili_api._context_key(same_generation_other_life))

    bilibili_auth.clear_session()
    assert bilibili_auth.load_session() is None


# ---------- 收藏夹内容(原生 /x/v3/fav/resource/list 接口) ----------

class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _patch_fav_env(monkeypatch, medias, has_more=False, code=0):
    """打桩 cookiejar + 收藏夹接口响应;返回记录到的请求参数。"""
    calls = []

    def fake_get(url, cookies=None, headers=None, params=None, timeout=None):
        calls.append(params)
        return _Resp({"code": code, "data": {"medias": medias, "has_more": has_more}})

    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_api.requests, "get", fake_get)
    return calls


def test_fetch_fav_content_parses_entries(monkeypatch):
    """归一化原生接口返回:标题/UP/封面/链接;失效视频标注。"""
    _patch_fav_env(monkeypatch, [
        {"bvid": "BV1xx", "title": "视频A", "attr": 0, "duration": 300,
         "cover": "http://i0.hdslb.com/bfs/a.jpg",
         "upper": {"name": "UP1", "mid": 111}},
        {"bvid": "BV2xx", "title": "", "attr": 2, "duration": 0,
         "cover": "", "upper": {"name": "UP2", "mid": 222}},
    ])
    items = bilibili_api.fetch_fav_content(123)
    assert len(items) == 2
    assert items[0]["bvid"] == "BV1xx"
    assert items[0]["uploader_mid"] == 111
    assert items[0]["thumbnail"].startswith("https://")
    assert items[0]["url"] == "https://www.bilibili.com/video/BV1xx"
    assert items[1]["title"] == "已失效视频", "attr!=0 的失效视频要有标注"


def test_fetch_fav_content_paginates(monkeypatch):
    """has_more 翻页直到取完,页间有防限流间隔。"""
    pages = [{"page": 1, "has_more": True}, {"page": 2, "has_more": False}]

    def fake_get(url, cookies=None, headers=None, params=None, timeout=None):
        p = pages.pop(0)
        return _Resp({"code": 0, "data": {
            "medias": [{"bvid": f"BVp{p['page']}", "title": "t", "attr": 0,
                        "upper": {"name": "U", "mid": 1}}],
            "has_more": p["has_more"]}})

    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_api.requests, "get", fake_get)
    # fetch_fav_content 翻页间隔走 time.sleep,测试里免等
    monkeypatch.setattr("time.sleep", lambda s: None)
    items = bilibili_api.fetch_fav_content(123)
    assert [i["bvid"] for i in items] == ["BVp1", "BVp2"]


def test_fetch_fav_content_api_error_raises(monkeypatch):
    """接口非 0 code(风控/无权限)应报错给路由层,而不是静默空列表。"""
    _patch_fav_env(monkeypatch, [], code=-403)
    with pytest.raises(RuntimeError):
        bilibili_api.fetch_fav_content(123)


# ---------- 我追的合集(collected/list + season/list,2026-08-18) ----------

def test_fetch_collected_seasons_requires_platform_web(monkeypatch):
    """collected/list 必须带 platform=web,否则 B 站返回空列表(实测踩坑)。"""
    calls = []

    class _Resp:
        def json(self):
            return {"code": 0, "data": {
                "count": 1, "has_more": False,
                "list": [{"id": 1761108, "title": "数理小品", "media_count": 30,
                          "cover": "http://i0.hdslb.com/bfs/s.jpg", "attr": 0}]}}

    def fake_get(url, cookies=None, headers=None, params=None, timeout=None):
        calls.append(params)
        return _Resp()

    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_api.requests, "get", fake_get)
    monkeypatch.setattr(bilibili_api, "_my_mid", lambda cj: 2974547)
    seasons = bilibili_api.fetch_collected_seasons()
    assert calls[0]["platform"] == "web", "不带 platform=web 会永远拿到空列表"
    assert seasons == [{"id": 1761108, "title": "数理小品", "media_count": 30,
                        "cover": "https://i0.hdslb.com/bfs/s.jpg"}]


def test_fetch_collected_seasons_error_raises(monkeypatch):
    class _Resp:
        def json(self):
            return {"code": -403, "message": "访问权限不足"}

    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_api, "_my_mid", lambda cj: 1)
    monkeypatch.setattr(bilibili_api.requests, "get",
                        lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError):
        bilibili_api.fetch_collected_seasons()


def test_fetch_season_content_parses_and_paginates(monkeypatch):
    pages = [{"has_more": True, "medias": [
        {"bvid": "BV1", "title": "第一集", "attr": None,
         "cover": "http://i0.hdslb.com/bfs/1.jpg", "duration": 302,
         "upper": {"name": "漫士沉思录", "mid": 46817116}}]},
        {"has_more": False, "medias": [
            {"bvid": "", "title": "", "attr": 2,
             "cover": "", "duration": 0, "upper": {"name": "", "mid": 0}}]}]

    def fake_get(url, cookies=None, headers=None, params=None, timeout=None):
        return _Resp({"code": 0, "data": pages.pop(0)})

    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_api.requests, "get", fake_get)
    monkeypatch.setattr("time.sleep", lambda s: None)
    items = bilibili_api.fetch_season_content(1761108)
    assert items[0]["bvid"] == "BV1"
    assert items[0]["uploader_mid"] == 46817116
    assert items[0]["thumbnail"].startswith("https://")
    assert items[1]["title"] == "已失效视频", "失效视频要有标注"


# ---------- 用户卡片(2026-08-17:mid → UP 名称/头像) ----------

def test_fetch_up_card_parses_name_and_face(monkeypatch):
    class _Resp:
        def json(self):
            return {"code": 0, "data": {"card": {"name": "远古野", "face": "https://x/face.jpg"}}}
    monkeypatch.setattr(bilibili_api.requests, "get", lambda *a, **k: _Resp())
    card = bilibili_api.fetch_up_card(46817116)
    assert card == {"uname": "远古野", "face": "https://x/face.jpg"}


def test_fetch_up_card_failure_returns_none(monkeypatch):
    class _Resp:
        def json(self):
            return {"code": -404}
    monkeypatch.setattr(bilibili_api.requests, "get", lambda *a, **k: _Resp())
    assert bilibili_api.fetch_up_card(1) is None

    def boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(bilibili_api.requests, "get", boom)
    assert bilibili_api.fetch_up_card(1) is None


# ---------- 模块级缓存补锁(2026-08-19 评审 P0:登出竞态残留旧身份) ----------

def test_logout_during_cookie_extract_no_stale_writeback(monkeypatch, tmp_path):
    """登出发生在 Edge cookie 提取期间:提取结果不得写回缓存。
    旧行为:clear_session 无锁先清,T1 提取完把旧 cookiejar 写回缓存,
    登出后最长 5 分钟仍以旧身份访问 B 站接口。"""
    import threading
    import time
    import yt_dlp as _ydl

    started, release = threading.Event(), threading.Event()

    class _FakeJar:
        pass

    class _FakeYDL:
        cookiejar = _FakeJar()
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            started.set()
            assert release.wait(3)
            return self
        def __exit__(self, *a):
            return False

    # 隔离:无 sess.json / 无 cookies.txt → 走 cookiesfrombrowser(假 YDL)分支
    monkeypatch.setattr(bilibili_auth, "SESSION_COOKIE_FILE", tmp_path / "sess.json")
    monkeypatch.setattr(bilibili_auth, "COOKIE_FILE", tmp_path / "cookies.txt")
    monkeypatch.setattr(_ydl, "YoutubeDL", _FakeYDL)

    result = {}

    def t_extract():
        result["cj"] = bilibili_auth.get_cookiejar()

    t1 = threading.Thread(target=t_extract)
    t1.start()
    assert started.wait(2), "T1 应进入慢提取"
    t2 = threading.Thread(target=clear_session)  # 登出发生在提取期间
    t2.start()
    time.sleep(0.2)  # 让 clear 走到(有锁时)等锁处
    release.set()
    t1.join(3)
    t2.join(3)
    assert result["cj"] is not None, "T1 返回旧 jar 本身无妨"
    assert bilibili_auth._cj_cache["cj"] is None, \
        "登出后缓存不得残留旧 cookiejar(5 分钟内所有请求都带旧身份)"


def test_session_and_fetch_caches_are_lock_protected():
    """会话域缓存(_cj_cache/_wbi_ie)持 _session_lock;数据域缓存
    (_followings_cache/_follow_cache)持 _fetch_lock;clear_session 覆盖两域。"""
    import inspect
    for fn, lock in (
        (bilibili_auth.get_cookiejar, "_session_lock"),
        (bilibili_api._get_wbi_ie, "_session_lock"),
        (bilibili_auth.clear_session, "_session_lock"),
        (bilibili_api.fetch_followings_all, "_fetch_lock"),
        (bilibili_api.fetch_follow_updates, "_fetch_lock"),
        (bilibili_api.reset_data_caches, "_fetch_lock"),
    ):
        assert lock in inspect.getsource(fn), f"{fn.__name__} 应持 {lock}"
    assert "reset_data_caches" in inspect.getsource(bilibili_auth.clear_session), \
        "clear_session 也要清数据域缓存(经 bilibili_api.reset_data_caches)"


# ---------- fetch_* 公共骨架 + 归一化收敛(2026-08-19 评审 P2) ----------

class _PagedResp:
    def __init__(self, data):
        self._d = data
    def json(self):
        return self._d


def test_paged_get_pages_and_sleeps(monkeypatch):
    """_paged_get:翻页收集、页间限速、pn 递增、终止即停。"""
    pages = [{"code": 0, "data": {"list": [1], "has_more": True}},
             {"code": 0, "data": {"list": [2], "has_more": False}}]
    pns, sleeps = [], []
    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())

    def fake_get(url, cookies=None, headers=None, params=None, timeout=None):
        pns.append(params["pn"])
        return _PagedResp(pages.pop(0))

    monkeypatch.setattr(bilibili_api.requests, "get", fake_get)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def parse(d, fetched):
        return d["list"], bool(d.get("has_more"))

    out = bilibili_api._paged_get("https://x/api", {}, what="测试", max_pages=5,
                                   ps=20, gap=0.3, parse=parse)
    assert out == [1, 2]
    assert pns == [1, 2]
    assert sleeps == [0.3], "页间限速只在还有下一页时执行"


def test_paged_get_mid_fetched_once(monkeypatch):
    """骨架取 mid 只打一次 nav 接口;翻页不再重复(旧行为每页一次)。"""
    pages = [{"code": 0, "data": {"list": [1], "has_more": True}},
             {"code": 0, "data": {"list": [2], "has_more": False}}]
    mid_calls = []

    def fake_mid(cj):
        mid_calls.append(1)
        return 2974547

    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_api, "_my_mid", fake_mid)
    monkeypatch.setattr(bilibili_api.requests, "get",
                        lambda *a, **k: _PagedResp(pages.pop(0)))
    monkeypatch.setattr("time.sleep", lambda s: None)

    def parse(d, fetched):
        return d["list"], bool(d.get("has_more"))

    out = bilibili_api._paged_get("https://x/api", {}, what="测试", max_pages=5,
                                   ps=20, mid_param="vmid", parse=parse)
    assert out == [1, 2]
    assert len(mid_calls) == 1, "mid 应只取一次,不是每页一次"


def test_video_item_normalizes_variants():
    """归一化器对齐各接口字段差异:owner/upper/顶层作者、pic/cover、
    时间戳键、失效标记、extra 覆盖。"""
    vi = bilibili_api._video_item
    a = vi({"bvid": "BV1", "title": "t", "pic": "http://i0/x.jpg",
            "owner": {"name": "U", "mid": 1}, "duration": 10, "view_at": 5})
    assert (a["bvid"], a["uploader"], a["uploader_mid"], a["duration"],
            a["view_at"], a["url"], a["thumbnail"]) == (
        "BV1", "U", 1, 10, 5, "https://www.bilibili.com/video/BV1", "https://i0/x.jpg")
    # upper 容器 + cover 键 + 无时间戳 + 失效标记
    b = vi({"bvid": "", "title": "", "attr": 2, "cover": "",
            "upper": {"name": "U2", "mid": 2}},
           owner_key="upper", pic_key="cover", view_at_key=None, mark_invalid=True)
    assert b["title"] == "已失效视频" and b["url"] == "" and "view_at" not in b
    # 顶层作者(arc/search 的 author/created) + extra 覆盖
    c = vi({"bvid": "BV3", "title": "t3", "author": "顶层UP", "pic": "",
            "created": 99}, owner_key=None, name_key="author",
           view_at_key="created", uploader_mid=42)
    assert c["uploader"] == "顶层UP" and c["view_at"] == 99 and c["uploader_mid"] == 42


def test_fetch_family_uses_shared_skeleton():
    """分页家族统一走 _paged_get;视频归一化统一走 _video_item。"""
    import inspect
    for fn in ("fetch_collected_seasons", "fetch_season_content",
               "fetch_fav_content", "fetch_followings", "fetch_followings_all"):
        assert "_paged_get" in inspect.getsource(getattr(bilibili_api, fn)), \
            f"{fn} 应走公共分页骨架"
    for fn in ("fetch_history", "fetch_watchlater", "fetch_season_content",
               "fetch_fav_content", "fetch_up_videos"):
        assert "_video_item" in inspect.getsource(getattr(bilibili_api, fn)), \
            f"{fn} 的归一化应走 _video_item"


def test_followings_all_mid_once_and_pages(monkeypatch):
    """followings_all 回归:多页拉取 mid 只取一次(旧实现每页重复打 nav)。"""
    pages = [{"code": 0, "data": {"total": 2, "list": [{"mid": 1, "uname": "A", "face": ""}]}},
             {"code": 0, "data": {"total": 2, "list": [{"mid": 2, "uname": "B", "face": ""}]}}]
    mid_calls = []

    def fake_mid(cj):
        mid_calls.append(1)
        return 2974547

    monkeypatch.setattr(bilibili_api, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_api, "_my_mid", fake_mid)
    monkeypatch.setattr(bilibili_api.requests, "get",
                        lambda *a, **k: _PagedResp(pages.pop(0)))
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(bilibili_api, "_followings_cache",
                        {"items": [], "total": 0, "ts": 0})
    r = bilibili_api.fetch_followings_all(force=True, max_pages=3)
    assert [u["mid"] for u in r["items"]] == [1, 2] and r["total"] == 2
    assert len(mid_calls) == 1, "mid 只取一次(旧实现每页调 _my_mid 多打 nav 接口)"
