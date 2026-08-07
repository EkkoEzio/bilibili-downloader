import json
from bilibili_auth import parse_poll_response, POLL_CODES, save_session, load_session, clear_session


def test_poll_codes_constants():
    assert POLL_CODES["86101"] == "waiting"
    assert POLL_CODES["86090"] == "scanned"
    assert POLL_CODES["86038"] == "expired"
    assert POLL_CODES["0"] == "success"


def test_parse_waiting():
    r = parse_poll_response({"data": {"code": 86101}}, "")
    assert r == {"status": "waiting", "sessdata": "", "message": "未扫码"}


def test_parse_scanned():
    r = parse_poll_response({"data": {"code": 86090}}, "")
    assert r["status"] == "scanned"


def test_parse_expired():
    r = parse_poll_response({"data": {"code": 86038}}, "")
    assert r["status"] == "expired"


def test_parse_success_extracts_sessdata():
    body = {"code": 0, "data": {"code": 0, "url": "https://x?SESSDATA=abc"}}
    set_cookie = "SESSDATA=hello123; Path=/; HttpOnly"
    r = parse_poll_response(body, set_cookie)
    assert r["status"] == "success"
    assert r["sessdata"] == "hello123"


def test_save_load_clear_session(tmp_path, monkeypatch):
    import bilibili_auth
    monkeypatch.setattr(bilibili_auth, "SESSION_COOKIE_FILE", tmp_path / "sess.json")
    save_session("SDATA", "123", "jct")
    loaded = load_session()
    assert loaded["SESSDATA"] == "SDATA"
    assert loaded["DedeUserID"] == "123"
    clear_session()
    assert load_session() is None
