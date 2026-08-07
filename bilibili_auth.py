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

POLL_CODES = {"86101": "waiting", "86090": "scanned", "86038": "expired", "0": "success"}
POLL_MESSAGES = {"86101": "未扫码", "86090": "已扫码,请在手机上确认", "86038": "二维码已失效", "0": "登录成功"}

# 网络函数可被测试替换
_http_get = requests.get


def qrcode_generate() -> dict:
    """生成二维码。返回 {'url','qrcode_key'}。"""
    resp = _http_get(QR_GENERATE, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"生成二维码失败: {data.get('message')}")
    d = data["data"]
    return {"url": d["url"], "qrcode_key": d["qrcode_key"]}


def qrcode_poll(qrcode_key: str) -> dict:
    """轮询扫码状态。返回 parse_poll_response 结果。"""
    resp = _http_get(QR_POLL, params={"qrcode_key": qrcode_key}, timeout=10)
    set_cookie = resp.headers.get("Set-Cookie", "")
    return parse_poll_response(resp.json(), set_cookie)


def parse_poll_response(data: dict, set_cookie_header: str) -> dict:
    """把 poll 响应解析成统一状态。SESSDATA 从 Set-Cookie 头提取。"""
    code = str(data.get("data", {}).get("code", -1))
    status = POLL_CODES.get(code, "unknown")
    sessdata = ""
    if status == "success":
        m = re.search(r"SESSDATA=([^;]+)", set_cookie_header or "")
        sessdata = m.group(1) if m else ""
    return {"status": status, "sessdata": sessdata, "message": POLL_MESSAGES.get(code, "未知状态")}


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


def get_login_status() -> dict:
    """用当前 cookie 调 nav 接口,返回登录信息。"""
    sess = load_session()
    if not sess or not sess.get("SESSDATA"):
        return {"logged_in": False}
    try:
        resp = requests.get(NAV_API, cookies={"SESSDATA": sess["SESSDATA"]}, timeout=10)
        d = resp.json().get("data", {})
        return {
            "logged_in": d.get("isLogin", False),
            "uname": d.get("uname", ""),
            "vip": d.get("vipStatus", 0) == 1,
            "vip_type": d.get("vipType", 0),
        }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
