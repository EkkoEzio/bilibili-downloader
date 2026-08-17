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


# ---------- 收藏夹内容(A4 回归) ----------

class _StubYDL:
    """替身 YoutubeDL:按需返回预设 info 或抛预设异常。"""
    info = {"entries": [{"id": "BV1xx", "title": "视频A",
                         "url": "https://www.bilibili.com/video/BV1xx"}]}
    exc = None

    def __init__(self, opts):
        pass

    def extract_info(self, url, download):
        if _StubYDL.exc:
            raise _StubYDL.exc
        return _StubYDL.info


def _patch_ydl(monkeypatch, info=None, exc=None):
    _StubYDL.info = info if info is not None else _StubYDL.info
    _StubYDL.exc = exc
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _StubYDL)
    monkeypatch.setattr(bilibili_auth, "get_ytdlp_cookie_arg", lambda: {})


def test_fetch_fav_content_parses_entries(monkeypatch):
    """正常解析 yt-dlp 返回的 entries。

    回归:模块内未 import yt_dlp 时抛 NameError 被裸 except 吞掉,
    接口永远返回空列表(2026-08-14 修复)。
    """
    _patch_ydl(monkeypatch, info={"entries": [
        {"id": "BV1xx", "title": "视频A", "url": "https://www.bilibili.com/video/BV1xx"},
        {"id": "BV2xx", "title": "视频B", "url": ""},
    ]})
    items = bilibili_auth.fetch_fav_content(123)
    assert len(items) == 2
    assert items[0]["bvid"] == "BV1xx"
    assert items[0]["title"] == "视频A"
    # 裸 BV 兜底补全成可下载链接
    assert items[1]["url"] == "https://www.bilibili.com/video/BV2xx"


def test_fetch_fav_content_download_error_returns_empty(monkeypatch):
    """下载失败(如私密收藏夹 403)仍返回空列表,这是预期行为。"""
    _patch_ydl(monkeypatch, exc=yt_dlp.utils.DownloadError("403"))
    assert bilibili_auth.fetch_fav_content(123) == []


def test_fetch_fav_content_does_not_swallow_programming_errors(monkeypatch):
    """意外异常(非 DownloadError)应向上抛给路由层报 500,而不是静默返回空。"""
    _patch_ydl(monkeypatch, exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        bilibili_auth.fetch_fav_content(123)


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
