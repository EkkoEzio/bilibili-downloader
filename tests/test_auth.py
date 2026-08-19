import json
import pytest
import yt_dlp
import bilibili_auth
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
    import bilibili_auth
    monkeypatch.setattr(bilibili_auth, "SESSION_COOKIE_FILE", tmp_path / "sess.json")
    save_session("SDATA", "123", "jct")
    loaded = load_session()
    assert loaded["SESSDATA"] == "SDATA"
    assert loaded["DedeUserID"] == "123"
    clear_session()
    assert load_session() is None


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

    monkeypatch.setattr(bilibili_auth, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_auth.requests, "get", fake_get)
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
    items = bilibili_auth.fetch_fav_content(123)
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

    monkeypatch.setattr(bilibili_auth, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_auth.requests, "get", fake_get)
    # fetch_fav_content 内部 import time as _t 打页间隔,测试里免等
    monkeypatch.setattr("time.sleep", lambda s: None)
    items = bilibili_auth.fetch_fav_content(123)
    assert [i["bvid"] for i in items] == ["BVp1", "BVp2"]


def test_fetch_fav_content_api_error_raises(monkeypatch):
    """接口非 0 code(风控/无权限)应报错给路由层,而不是静默空列表。"""
    _patch_fav_env(monkeypatch, [], code=-403)
    with pytest.raises(RuntimeError):
        bilibili_auth.fetch_fav_content(123)


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

    monkeypatch.setattr(bilibili_auth, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_auth.requests, "get", fake_get)
    monkeypatch.setattr(bilibili_auth, "_my_mid", lambda cj: 2974547)
    seasons = bilibili_auth.fetch_collected_seasons()
    assert calls[0]["platform"] == "web", "不带 platform=web 会永远拿到空列表"
    assert seasons == [{"id": 1761108, "title": "数理小品", "media_count": 30,
                        "cover": "https://i0.hdslb.com/bfs/s.jpg"}]


def test_fetch_collected_seasons_error_raises(monkeypatch):
    class _Resp:
        def json(self):
            return {"code": -403, "message": "访问权限不足"}

    monkeypatch.setattr(bilibili_auth, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_auth, "_my_mid", lambda cj: 1)
    monkeypatch.setattr(bilibili_auth.requests, "get",
                        lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError):
        bilibili_auth.fetch_collected_seasons()


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

    monkeypatch.setattr(bilibili_auth, "get_cookiejar", lambda: object())
    monkeypatch.setattr(bilibili_auth.requests, "get", fake_get)
    monkeypatch.setattr("time.sleep", lambda s: None)
    items = bilibili_auth.fetch_season_content(1761108)
    assert items[0]["bvid"] == "BV1"
    assert items[0]["uploader_mid"] == 46817116
    assert items[0]["thumbnail"].startswith("https://")
    assert items[1]["title"] == "已失效视频", "失效视频要有标注"


# ---------- 用户卡片(2026-08-17:mid → UP 名称/头像) ----------

def test_fetch_up_card_parses_name_and_face(monkeypatch):
    class _Resp:
        def json(self):
            return {"code": 0, "data": {"card": {"name": "远古野", "face": "https://x/face.jpg"}}}
    monkeypatch.setattr(bilibili_auth.requests, "get", lambda *a, **k: _Resp())
    card = bilibili_auth.fetch_up_card(46817116)
    assert card == {"uname": "远古野", "face": "https://x/face.jpg"}


def test_fetch_up_card_failure_returns_none(monkeypatch):
    class _Resp:
        def json(self):
            return {"code": -404}
    monkeypatch.setattr(bilibili_auth.requests, "get", lambda *a, **k: _Resp())
    assert bilibili_auth.fetch_up_card(1) is None

    def boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(bilibili_auth.requests, "get", boom)
    assert bilibili_auth.fetch_up_card(1) is None
