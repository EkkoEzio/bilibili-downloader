"""浏览器登录态的多 profile 选择。

背景(2026-11-22 用户报障:浏览器里明明是登录的,应用却显示未登录):
yt-dlp 的 cookiesfrombrowser 在不指定 profile 时,只读「Cookies 库 mtime
最新」的那一个 profile。用户的 Edge 有多个 profile(Default「娱乐」/
Profile 2「工作」),B 站的 SESSDATA 只落在其中一个——读错 profile 时接口
照样 200,但没有登录态。

这里锁住的行为:遍历所有 profile,取真的带 B 站 SESSDATA 的那个。
"""
import json
from collections import namedtuple

import browser_cookies

Cookie = namedtuple("Cookie", ["name", "domain"])


def _jar(*pairs):
    return [Cookie(name, domain) for name, domain in pairs]


def _make_edge(root, profiles):
    """造一个 Edge 用户目录:profiles = {profile 名: 是否有 Cookies 库}。"""
    root.mkdir(parents=True, exist_ok=True)
    info_cache = {name: {"name": name} for name in profiles}
    (root / "Local State").write_text(
        json.dumps({"profile": {"info_cache": info_cache}}), encoding="utf-8")
    for name in profiles:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def test_has_bilibili_session_only_accepts_bilibili_sessdata():
    assert browser_cookies.has_bilibili_session(_jar(("SESSDATA", ".bilibili.com")))
    assert not browser_cookies.has_bilibili_session(_jar(("DedeUserID", ".bilibili.com")))
    assert not browser_cookies.has_bilibili_session(_jar(("SESSDATA", ".example.com")))
    assert not browser_cookies.has_bilibili_session(None)


def test_list_profiles_orders_by_cookie_db_mtime(tmp_path, monkeypatch):
    root = _make_edge(tmp_path / "Edge", ["Default", "Profile 2"])
    old = root / "Default" / "Cookies"
    new = root / "Profile 2" / "Cookies"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    import os
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    monkeypatch.setattr(browser_cookies, "browser_root", lambda browser="edge": root)

    names = [path.name for path in browser_cookies.list_profiles()]
    assert names == ["Profile 2", "Default"]


def test_list_profiles_finds_profile_missing_from_local_state(tmp_path, monkeypatch):
    root = _make_edge(tmp_path / "Edge", ["Default"])
    (root / "Profile 9").mkdir()
    (root / "Profile 9" / "Cookies").write_bytes(b"x")
    monkeypatch.setattr(browser_cookies, "browser_root", lambda browser="edge": root)

    names = [path.name for path in browser_cookies.list_profiles()]
    assert "Profile 9" in names


def test_extract_cookies_picks_the_profile_that_has_the_login(tmp_path, monkeypatch):
    """最新 mtime 的 profile 没登录态时,必须继续找,而不是就此认输。"""
    root = _make_edge(tmp_path / "Edge", ["Default", "Profile 2"])
    for name in ("Default", "Profile 2"):
        (root / name / "Cookies").write_bytes(b"x")
    # 复刻真实故障:Default 的 Cookies 库更新(浏览器最后停在这个 profile),
    # 但登录态在旧的 Profile 2 里。yt-dlp 默认只会读 Default → 未登录。
    import os
    os.utime(root / "Default" / "Cookies", (2000, 2000))
    os.utime(root / "Profile 2" / "Cookies", (1000, 1000))
    monkeypatch.setattr(browser_cookies, "browser_root", lambda browser="edge": root)

    tried = []

    def fake_extract(browser, profile):
        tried.append(profile)
        if profile == "Profile 2":
            return _jar(("SESSDATA", ".bilibili.com"), ("DedeUserID", ".bilibili.com"))
        return _jar(("DedeUserID", ".bilibili.com"))

    monkeypatch.setattr(browser_cookies, "_extract", fake_extract)

    jar, profile = browser_cookies.extract_cookies()
    assert profile == "Profile 2"
    assert "Default" in tried and "Profile 2" in tried
    assert browser_cookies.has_bilibili_session(jar)
    assert browser_cookies.cached_profile() == "Profile 2"


def test_extract_cookies_falls_back_to_newest_when_nobody_logged_in(tmp_path, monkeypatch):
    root = _make_edge(tmp_path / "Edge", ["Default"])
    (root / "Default" / "Cookies").write_bytes(b"x")
    monkeypatch.setattr(browser_cookies, "browser_root", lambda browser="edge": root)
    monkeypatch.setattr(
        browser_cookies, "_extract",
        lambda browser, profile: _jar(("DedeUserID", ".bilibili.com")))

    jar, profile = browser_cookies.extract_cookies()
    assert profile == "Default"
    assert jar is not None


def test_resolved_profile_is_cached_and_reused(tmp_path, monkeypatch):
    root = _make_edge(tmp_path / "Edge", ["Default"])
    (root / "Default" / "Cookies").write_bytes(b"x")
    monkeypatch.setattr(browser_cookies, "browser_root", lambda browser="edge": root)

    calls = []

    def fake_extract(browser, profile):
        calls.append(profile)
        return _jar(("SESSDATA", ".bilibili.com"))

    monkeypatch.setattr(browser_cookies, "_extract", fake_extract)
    browser_cookies._profile_cache.update(profile="", ts=0.0)

    assert browser_cookies.resolved_profile() == "Default"
    assert browser_cookies.resolved_profile() == "Default"
    assert len(calls) == 1  # 第二次命中缓存,不再重复解密
