"""B 站登录与会话:扫码登录、cookie 策略、cookiejar 缓存。

登录方式三选一,优先级:扫码 cookie > Edge 浏览器 > cookies.txt。
B 站读接口(历史/收藏/关注等)在 bilibili_api.py,依赖本模块的
get_cookiejar / BROWSER_HEADERS。
"""
import re
import threading
import time
from typing import Optional

import requests
import yt_dlp

import storage
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
    storage.write_json(SESSION_COOKIE_FILE, {
        "SESSDATA": sessdata, "DedeUserID": dede_userid, "bili_jct": bili_jct
    })


def load_session() -> Optional[dict]:
    return storage.read_json(SESSION_COOKIE_FILE, default=None)


def clear_session() -> None:
    """退出扫码登录:删 sess.json + 清会话缓存。不动 cookies.txt(那是
    用户手工导入的登录态,归 import_cookies_txt/logout 管)。"""
    if SESSION_COOKIE_FILE.exists():
        SESSION_COOKIE_FILE.unlink()
    # 同时清掉 cookiejar 缓存,避免退出后还能访问接口(5 分钟缓存)。
    # 会话域锁在本模块;数据域缓存(关注列表/关注动态)归 bilibili_api,
    # 函数内晚导入避免循环依赖(bilibili_api 顶层 import 本模块)。
    # 各拿各的锁、不嵌套:清会话域期间不阻塞数据域(反之亦然)。
    global _cj_cache
    with _session_lock:
        _cj_cache = {"cj": None, "ts": 0.0}
    import bilibili_api
    bilibili_api.reset_data_caches()


def import_cookies_txt(text: str) -> None:
    """用户粘贴的 Netscape cookies.txt 落盘,并切到该登录态。"""
    COOKIE_FILE.write_text(text, encoding="utf-8")
    clear_session()


def logout() -> None:
    """完全退出:扫码会话 + 手工导入的 cookies.txt 一起清。"""
    clear_session()
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()


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
# 会话域锁:罩 _cj_cache/_wbi_ie(bilibili_api)。get_cookiejar 整体入锁
# (含 Edge 慢提取),与 clear_session 严格排序——登出期间并发的提取结果
# 不会写回缓存,也顺带避免并发重复提取;缓存命中时锁内只有一次微秒级检查。
_session_lock = threading.Lock()


def get_cookiejar():
    """返回当前生效的 cookiejar(扫码 > cookies.txt > Edge)。失败返回 None。

    缓存 5 分钟:Edge 读 cookie 较耗时,避免每次请求重建。
    """
    with _session_lock:
        if _cj_cache["cj"] is not None and time.time() - _cj_cache["ts"] < 300:
            return _cj_cache["cj"]
        cookie_arg = get_ytdlp_cookie_arg()
        if "cookiefile" in cookie_arg:
            try:
                import http.cookiejar
                cj = http.cookiejar.MozillaCookieJar(cookie_arg["cookiefile"])
                cj.load(ignore_discard=True, ignore_expires=True)
                _cj_cache.update(cj=cj, ts=time.time())
                return cj
            except Exception:
                pass
        if "cookiesfrombrowser" in cookie_arg:
            try:
                with yt_dlp.YoutubeDL({"quiet": True, **cookie_arg}) as ydl:
                    _cj_cache.update(cj=ydl.cookiejar, ts=time.time())
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
