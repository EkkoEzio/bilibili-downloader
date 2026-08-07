"""B 站扫码登录 + cookie 策略。

登录方式三选一,优先级:扫码 cookie > Edge 浏览器 > cookies.txt。
"""
import json
import re
from typing import Optional

import requests

from config import SESSION_COOKIE_FILE, COOKIE_FILE, BROWSER_COOKIE_SOURCE

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# B 站 passport 接口有风控,裸请求会 412。必须带浏览器 headers。
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

POLL_CODES = {"86101": "waiting", "86090": "scanned", "86038": "expired", "0": "success"}
POLL_MESSAGES = {"86101": "未扫码", "86090": "已扫码,请在手机上确认", "86038": "二维码已失效", "0": "登录成功"}

# 网络函数可被测试替换
_http_get = requests.get


def qrcode_generate() -> dict:
    """生成二维码。返回 {'url','qrcode_key'}。"""
    resp = _http_get(QR_GENERATE, headers=BROWSER_HEADERS, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"生成二维码失败: {data.get('message')}")
    d = data["data"]
    return {"url": d["url"], "qrcode_key": d["qrcode_key"]}


def qrcode_poll(qrcode_key: str) -> dict:
    """轮询扫码状态。返回 parse_poll_response 结果。"""
    resp = _http_get(QR_POLL, params={"qrcode_key": qrcode_key},
                     headers=BROWSER_HEADERS, timeout=10)
    set_cookie = resp.headers.get("Set-Cookie", "")
    return parse_poll_response(resp.json(), set_cookie)


def parse_poll_response(data: dict, set_cookie_header: str) -> dict:
    """把 poll 响应解析成统一状态。

    SESSDATA 优先从 data.url 的 query 参数解析(社区文档验证最可靠),
    Set-Cookie 头作为兜底。同时提取 DedeUserID/bili_jct。
    """
    code = str(data.get("data", {}).get("code", -1))
    status = POLL_CODES.get(code, "unknown")
    sessdata = ""
    dede_userid = ""
    bili_jct = ""
    if status == "success":
        from urllib.parse import urlparse, parse_qs
        url = data.get("data", {}).get("url", "")
        qs = parse_qs(urlparse(url).query)
        # 优先 url query
        if qs.get("SESSDATA"):
            sessdata = qs["SESSDATA"][0]
        dede_userid = (qs.get("DedeUserID") or [""])[0]
        bili_jct = (qs.get("bili_jct") or [""])[0]
        # 兜底:Set-Cookie
        if not sessdata:
            m = re.search(r"SESSDATA=([^;]+)", set_cookie_header or "")
            sessdata = m.group(1) if m else ""
        if not bili_jct:
            m = re.search(r"bili_jct=([^;]+)", set_cookie_header or "")
            bili_jct = m.group(1) if m else ""
    return {"status": status, "sessdata": sessdata, "dede_userid": dede_userid,
            "bili_jct": bili_jct, "message": POLL_MESSAGES.get(code, "未知状态")}


def save_session(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> None:
    SESSION_COOKIE_FILE.write_text(json.dumps({
        "SESSDATA": sessdata, "DedeUserID": dede_userid, "bili_jct": bili_jct
    }, ensure_ascii=False), encoding="utf-8")


def load_session() -> Optional[dict]:
    if not SESSION_COOKIE_FILE.exists():
        return None
    try:
        return json.loads(SESSION_COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_session() -> None:
    if SESSION_COOKIE_FILE.exists():
        SESSION_COOKIE_FILE.unlink()


def has_session() -> bool:
    s = load_session()
    return bool(s and s.get("SESSDATA"))


def write_cookies_txt(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> None:
    """把扫码 cookie 写成 Netscape cookies.txt,供 yt-dlp 用。"""
    jar_lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/docs/http-cookies.html",
        ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t" + sessdata,
    ]
    if dede_userid:
        jar_lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tDedeUserID\t{dede_userid}")
    if bili_jct:
        jar_lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\t{bili_jct}")
    COOKIE_FILE.write_text("\n".join(jar_lines) + "\n", encoding="utf-8")


def get_ytdlp_cookie_arg() -> dict:
    """返回传给 yt-dlp YoutubeDL 的 cookie 选项。优先级:扫码>Edge>cookies.txt。"""
    sess = load_session()
    if sess and sess.get("SESSDATA"):
        write_cookies_txt(sess["SESSDATA"], sess.get("DedeUserID", ""), sess.get("bili_jct", ""))
        return {"cookiefile": str(COOKIE_FILE)}
    if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 50:
        return {"cookiefile": str(COOKIE_FILE)}
    return {"cookiesfrombrowser": (BROWSER_COOKIE_SOURCE,)}


_cj_cache = {"cj": None, "ts": 0.0}


def get_cookiejar():
    """返回当前生效的 cookiejar(扫码 > cookies.txt > Edge)。失败返回 None。

    缓存 5 分钟:Edge 读 cookie 较耗时,避免每次请求重建。
    """
    import time as _time
    if _cj_cache["cj"] is not None and _time.time() - _cj_cache["ts"] < 300:
        return _cj_cache["cj"]
    cookie_arg = get_ytdlp_cookie_arg()
    if "cookiefile" in cookie_arg:
        try:
            import http.cookiejar
            cj = http.cookiejar.MozillaCookieJar(cookie_arg["cookiefile"])
            cj.load(ignore_discard=True, ignore_expires=True)
            _cj_cache.update(cj=cj, ts=_time.time())
            return cj
        except Exception:
            pass
    if "cookiesfrombrowser" in cookie_arg:
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, **cookie_arg}) as ydl:
                _cj_cache.update(cj=ydl.cookiejar, ts=_time.time())
                return ydl.cookiejar
        except Exception:
            pass
    return None


def get_login_status() -> dict:
    """用当前生效的 cookie(扫码 > cookies.txt > Edge)调 nav 接口,返回登录信息。"""
    cookies = get_cookiejar()
    if cookies is None:
        return {"logged_in": False}
    try:
        resp = requests.get(NAV_API, cookies=cookies,
                            headers={"User-Agent": BROWSER_HEADERS["User-Agent"]}, timeout=10)
        d = resp.json().get("data", {})
        return {
            "logged_in": d.get("isLogin", False),
            "uname": d.get("uname", ""),
            "vip": d.get("vipStatus", 0) == 1,
            "vip_type": d.get("vipType", 0),
        }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}


def fetch_history(page: int = 1, size: int = 50) -> list[dict]:
    """拉取观看历史。返回归一化的视频列表(最新在前)。

    需要 cookiejar(扫码/Edge/cookies.txt)。size 上限 100/页。
    """
    import time as _time
    cj = get_cookiejar()
    if cj is None:
        raise RuntimeError("未登录,无法获取观看历史")
    # B 站接口偶发 SSL 断连,自动重试
    last = None
    for attempt in range(3):
        try:
            resp = requests.get("https://api.bilibili.com/x/v2/history",
                                cookies=cj, headers=BROWSER_HEADERS,
                                params={"pn": page, "ps": min(size, 100)}, timeout=12)
            data = resp.json()
            break
        except requests.RequestException as e:
            last = e
            _time.sleep(1 + attempt)
    else:
        raise RuntimeError(f"网络错误(已重试 3 次):{last}")
    if data.get("code") != 0:
        raise RuntimeError(data.get("message", "获取失败"))
    items = []
    for it in data.get("data", []) or []:
        owner = it.get("owner") or {}
        items.append({
            "bvid": it.get("bvid", ""),
            "title": it.get("title", ""),
            "uploader": owner.get("name", ""),
            "duration": it.get("duration", 0),
            "thumbnail": it.get("pic", ""),
            "view_at": it.get("view_at", 0),
            "url": f"https://www.bilibili.com/video/{it.get('bvid','')}",
        })
    return items
