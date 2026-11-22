"""从本机浏览器读 B 站登录态(多 profile 感知)。

为什么不能直接用 yt-dlp 的 cookiesfrombrowser:
yt-dlp 在不指定 profile 时,会在浏览器用户目录里递归找 mtime 最新的那个
「Cookies」数据库——也就是只读一个 profile。同一台 Edge 里通常有多个
profile(本机实测:Default「娱乐」/ Profile 2「工作」),B 站的 SESSDATA
一般只落在其中一个。读错 profile 时接口照样 200,但没有登录态,界面上就
显示「未登录」,而浏览器里明明是登录的(2026-11-22 用户报障)。

分工:解密仍然交给 yt-dlp(它对 macOS 钥匙串、Chromium v10/v20 的处理最
完整,自己重写风险高),这里只负责「把对的 profile 选出来」。

规则:按 Cookies 库 mtime 从新到旧依次尝试每个 profile,取第一个真的带
B 站 SESSDATA 的;都没有就退回 yt-dlp 的默认行为(读最新的那个),保证
不比原来更差。

平台说明:macOS/Linux 的 Chromium 系 Cookie 用钥匙串里的 Safe Storage
密码解密,yt-dlp 可解;Windows 上 Edge 自 127 起对 Cookie 做了 App-Bound
加密,解密需要提权,yt-dlp 多数情况下拿不到——Windows 的浏览器免登录因此
不可靠,扫码 / cookies.txt 才是那边的正路。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("bd.browser_cookies")

BROWSER = "edge"

# 解析结果缓存:界面状态与下载/解析路径必须用同一个 profile,
# 否则会出现「界面显示已登录、下载却带着另一个 profile 的 Cookie」。
_PROFILE_TTL = 300.0
_profile_cache: dict = {"profile": "", "ts": 0.0}


def browser_root(browser: str = BROWSER) -> Path:
    """浏览器用户数据目录(当前只支持 Edge)。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Microsoft Edge"
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(local) / "Microsoft" / "Edge" / "User Data"
    return Path.home() / ".config" / "microsoft-edge"


def cookie_db(profile_dir: Path) -> Path | None:
    """profile 目录下的 Chromium Cookies 库(新旧两种布局都认)。"""
    for candidate in (profile_dir / "Cookies", profile_dir / "Network" / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def _profiles_from_local_state(root: Path) -> list[str]:
    """Local State 的 profile.info_cache 是浏览器自己维护的 profile 清单。"""
    try:
        data = json.loads((root / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cache = data.get("profile", {}).get("info_cache", {})
    return list(cache.keys()) if isinstance(cache, dict) else []


def list_profiles(browser: str = BROWSER) -> list[Path]:
    """列出所有带 Cookies 库的 profile 目录,按 Cookies 库 mtime 从新到旧。"""
    root = browser_root(browser)
    names = _profiles_from_local_state(root)
    found: list = []
    seen = set()
    for name in names:
        path = root / name
        db = cookie_db(path)
        if db is None:
            continue
        seen.add(name)
        found.append((db.stat().st_mtime, path))
    # 兜底:Local State 里没有、但目录里确实躺着 Cookies 库的 profile
    if root.is_dir():
        try:
            children = sorted(root.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir() or child.name in seen:
                continue
            db = cookie_db(child)
            if db is None:
                continue
            seen.add(child.name)
            found.append((db.stat().st_mtime, child))
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def has_bilibili_session(jar) -> bool:
    """jar 里有没有 B 站的 SESSDATA(有才说明这个 profile 是登录态)。"""
    if jar is None:
        return False
    try:
        for cookie in jar:
            if getattr(cookie, "name", "") == "SESSDATA" and \
                    "bilibili.com" in (getattr(cookie, "domain", "") or ""):
                return True
    except TypeError:
        return False
    return False


def _extract(browser: str, profile: str | None):
    import yt_dlp
    args = (browser, profile) if profile else (browser,)
    with yt_dlp.YoutubeDL({"quiet": True, "cookiesfrombrowser": args}) as ydl:
        return ydl.cookiejar


def _remember_profile(profile: str) -> None:
    _profile_cache["profile"] = profile
    _profile_cache["ts"] = time.monotonic()


def cached_profile() -> str:
    """最近一次解析到的 profile(只读缓存,不触发探测)。"""
    return _profile_cache["profile"]


def extract_cookies(browser: str = BROWSER):
    """返回 (cookiejar, 命中的 profile 名);全都没有登录态时退回默认行为。"""
    fallback = None
    for profile_dir in list_profiles(browser):
        profile = profile_dir.name
        try:
            jar = _extract(browser, profile)
        except Exception as exc:
            log.debug("profile %s 提取失败: %s", profile, exc)
            continue
        if has_bilibili_session(jar):
            log.info("浏览器登录态来自 profile: %s", profile)
            _remember_profile(profile)
            return jar, profile
        if fallback is None:
            fallback = (jar, profile)
    if fallback is not None:
        _remember_profile(fallback[1])
        return fallback
    # 没有可用 profile(浏览器没装/没数据):退回 yt-dlp 默认行为
    try:
        jar = _extract(browser, None)
    except Exception as exc:
        log.debug("默认浏览器 cookie 提取失败: %s", exc)
        return None, ""
    _remember_profile("")
    return jar, ""


def resolved_profile(browser: str = BROWSER) -> str:
    """当前应该使用的 profile(带缓存);空串表示用 yt-dlp 默认行为。"""
    if time.monotonic() - _profile_cache["ts"] < _PROFILE_TTL:
        return _profile_cache["profile"]
    try:
        extract_cookies(browser)
    except Exception as exc:
        log.debug("解析浏览器 profile 失败: %s", exc)
    return _profile_cache["profile"]
