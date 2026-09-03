"""B 站认证与显式登录上下文。

登录来源由持久化 auth-context.json 明确选择，不能再靠文件存在与否隐式抢占：
qr / cookie_file / browser / none。状态 schema v2 用随机 incarnation 区分每次
可恢复的认证状态生命期，generation 只在同一 incarnation 内单调；所有运行时
缓存绑定 (fingerprint, incarnation, generation)，旧请求只能完成，不能回写新身份。
"""
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

import requests
import yt_dlp

import storage
from config import SESSION_COOKIE_FILE, COOKIE_FILE, BROWSER_COOKIE_SOURCE
from request_governor import UpstreamRateLimitError, governed_get, governor

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

POLL_CODES = {"86101": "waiting", "86090": "scanned", "86038": "expired", "0": "success"}
POLL_MESSAGES = {"86101": "未扫码", "86090": "已扫码,请在手机上确认", "86038": "二维码已失效", "0": "登录成功"}
AUTH_SOURCES = frozenset(("qr", "cookie_file", "browser", "none"))
AUTH_STATE_VERSION = 2

log = logging.getLogger("bd.bilibili_auth")
_http_get = requests.get

# 会话域锁同时保护认证快照、cookiejar 与 bilibili_api 的 WBI signer。
# 慢 cookie 提取仍整体持锁，使 clear/logout 与提取严格排序；切换后再在锁外清
# API 数据缓存，避免持 session lock 回调 bilibili_api 形成死锁。
_session_lock = threading.Lock()
_switch_lock = threading.Lock()
_auth_state_cache = {"path": None, "context": None}
_cj_cache = {"cj": None, "ts": 0.0, "context": None}


class AuthContextChangedError(RuntimeError):
    """请求执行期间认证世代改变；调用方应丢弃旧结果。"""


def _auth_state_file() -> Path:
    # 动态派生，既兼容测试 monkeypatch SESSION_COOKIE_FILE，也不要求改 config.py。
    return SESSION_COOKIE_FILE.with_name("auth-context.json")


def _qr_cookie_file() -> Path:
    # QR 导出与用户导入 COOKIE_FILE 严格分离，绝不覆盖用户文件。
    return SESSION_COOKIE_FILE.with_name("qr-cookies.txt")


def _account_fingerprint(identifier) -> str:
    raw = str(identifier or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(("bilibili-account:" + raw).encode("utf-8")).hexdigest()


def _source_fingerprint(source: str) -> str:
    return hashlib.sha256(("bilibili-source:" + source).encode("utf-8")).hexdigest()


def _valid_hex(value, length: int) -> bool:
    text = str(value or "").lower()
    return (len(text) == length
            and all(ch in "0123456789abcdef" for ch in text))


def _new_incarnation() -> str:
    return secrets.token_hex(16)


def _make_context(source: str, generation: int, *, incarnation: str = "",
                  identifier="", fingerprint: str = "",
                  authenticated: bool = False) -> dict:
    source = source if source in AUTH_SOURCES else "none"
    account_fp = _account_fingerprint(identifier)
    stored_fp = str(fingerprint or "").lower()
    valid_stored_fp = _valid_hex(stored_fp, 64)
    verified = bool(account_fp or (authenticated and valid_stored_fp))
    fp = account_fp or (stored_fp if valid_stored_fp else _source_fingerprint(source))
    incarnation = str(incarnation or "").lower()
    if not _valid_hex(incarnation, 32):
        incarnation = _new_incarnation()
    return {
        "version": AUTH_STATE_VERSION,
        "source": source,
        "fingerprint": fp,
        "incarnation": incarnation,
        "generation": max(0, int(generation or 0)),
        "authenticated": verified,
    }


def _same_context(left, right) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    try:
        return (left.get("source") == right.get("source")
                and left.get("fingerprint") == right.get("fingerprint")
                and left.get("incarnation") == right.get("incarnation")
                and int(left.get("generation", -1)) == int(right.get("generation", -2)))
    except (TypeError, ValueError):
        return False


def _valid_session(session) -> bool:
    return isinstance(session, dict) and bool(str(session.get("SESSDATA") or ""))


def _cookie_value_from_text(text: str, name: str) -> str:
    for line in str(text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[-2] == name:
            return parts[-1].strip()
    match = re.search(rf"(?:^|[;\s]){re.escape(name)}(?:=|\s+)\s*([^;\s]+)",
                      str(text or ""), re.M)
    return match.group(1).strip() if match else ""


def _read_cookie_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _valid_cookie_file(path: Path) -> bool:
    return bool(_cookie_value_from_text(_read_cookie_file(path), "SESSDATA"))


def _cookiejar_value(cookiejar, name: str) -> str:
    try:
        for cookie in cookiejar:
            if getattr(cookie, "name", "") == name:
                return str(getattr(cookie, "value", "") or "")
    except Exception:
        return ""
    return ""


def _normalize_stored_context(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict) or raw.get("source") not in AUTH_SOURCES:
        return None
    try:
        version = int(raw.get("version", 1))
        generation = int(raw.get("generation", 0))
    except (TypeError, ValueError):
        return None
    if version not in (1, AUTH_STATE_VERSION) or generation < 0:
        return None

    incarnation = str(raw.get("incarnation") or "").lower()
    if version == AUTH_STATE_VERSION:
        if not _valid_hex(incarnation, 32):
            return None
    else:
        # v1 没有 incarnation；分配新生命期并立即写回 v2，因此不会认领旧缓存。
        incarnation = _new_incarnation()

    source = raw["source"]
    if source == "qr" and not _valid_session(load_session()):
        return None
    if source == "cookie_file" and not _valid_cookie_file(COOKIE_FILE):
        return None
    authenticated = raw.get("authenticated") is True
    fingerprint = str(raw.get("fingerprint") or "").lower()
    if authenticated and not _valid_hex(fingerprint, 64):
        authenticated = False
    return _make_context(
        source, generation, incarnation=incarnation, fingerprint=fingerprint,
        authenticated=authenticated)


def _legacy_context(previous_generation: int = 0, *,
                    incarnation: str = "") -> dict:
    incarnation = (str(incarnation or "").lower()
                   if _valid_hex(incarnation, 32) else _new_incarnation())
    session = load_session()
    if _valid_session(session):
        return _make_context(
            "qr", previous_generation + 1, incarnation=incarnation,
            identifier=session.get("DedeUserID", ""))
    if _valid_cookie_file(COOKIE_FILE):
        text = _read_cookie_file(COOKIE_FILE)
        return _make_context(
            "cookie_file", previous_generation + 1, incarnation=incarnation,
            identifier=_cookie_value_from_text(text, "DedeUserID"))
    return _make_context(
        "browser", previous_generation + 1, incarnation=incarnation)


def _get_auth_context_locked() -> dict:
    path = _auth_state_file()
    if (_auth_state_cache["path"] == path
            and isinstance(_auth_state_cache["context"], dict)):
        return _auth_state_cache["context"]

    raw = storage.read_json(path, default=None)
    context = _normalize_stored_context(raw)
    try:
        raw_version = int(raw.get("version", 1)) if isinstance(raw, dict) else None
    except (TypeError, ValueError):
        raw_version = None
    needs_write = context is None or raw_version != AUTH_STATE_VERSION
    if context is None:
        # 缺失、损坏或不可恢复的状态开启全新 incarnation；旧 generation
        # 不再具有全局意义，不能据此重新认领历史缓存目录。
        context = _legacy_context()
    if needs_write and not storage.write_json(path, context, indent=1):
        raise RuntimeError("认证上下文初始化失败:状态未写入磁盘")

    _auth_state_cache.update(path=path, context=context)
    return context


def get_auth_context() -> dict:
    """返回不含 Cookie 的可序列化认证快照。"""
    with _session_lock:
        return dict(_get_auth_context_locked())


def _is_auth_context_current_locked(context) -> bool:
    """调用方已持 _session_lock 时使用的无重入比较。"""
    return _same_context(_get_auth_context_locked(), context)


def is_auth_context_current(context) -> bool:
    with _session_lock:
        return _is_auth_context_current_locked(context)


def commit_if_current(context: dict, commit):
    """仅当认证上下文仍完全一致时执行短提交回调。

    网络请求、JSON 序列化等慢工作必须在调用前完成；``commit`` 只做内存
    替换或原子 rename。这样账号切换与旧请求提交在同一把会话锁上排序，
    不存在“检查通过后再晚写”的 TOCTOU 窗口。
    """
    with _session_lock:
        if not _is_auth_context_current_locked(context):
            raise AuthContextChangedError("登录状态已变化，请重试")
        return commit()


def get_auth_snapshot() -> tuple[dict, object]:
    """返回彼此属于同一完整认证上下文的 ``(context, cookiejar)`` 快照。

    首次从 Cookie 识别出账号时会升级 fingerprint 并在当前 incarnation 内
    递增 generation，因此最多重取三次；外部持续切换登录态时明确失败，而
    不是拼出旧 Cookie + 新 context 的混合快照。
    """
    for _ in range(3):
        context = get_auth_context()
        cookiejar = get_cookiejar(context)
        if is_auth_context_current(context):
            return context, cookiejar
    raise AuthContextChangedError("登录状态正在切换，请稍后重试")


def _write_state_and_commit_locked(current: dict, source: str, *, identifier=""):
    """调用方持 _switch_lock；内部短持 _session_lock 完成持久化 + CAS。"""
    global _cj_cache
    with _session_lock:
        live = _get_auth_context_locked()
        if not _same_context(live, current):
            return None
        candidate = _make_context(
            source, int(live["generation"]) + 1,
            incarnation=live["incarnation"], identifier=identifier)
        if not storage.write_json(_auth_state_file(), candidate, indent=1):
            return None
        _auth_state_cache.update(path=_auth_state_file(), context=candidate)
        _cj_cache = {"cj": None, "ts": 0.0, "context": None}
        return candidate


def _reset_identity_data_caches() -> None:
    import bilibili_api
    bilibili_api.reset_data_caches()


def _reset_runtime_caches() -> None:
    global _cj_cache
    with _session_lock:
        _cj_cache = {"cj": None, "ts": 0.0, "context": None}
    _reset_identity_data_caches()


def _atomic_write_bytes(path: Path, payload: bytes) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log.exception("原子写凭据文件 %s 失败", path)
        return False


def _atomic_write_text(path: Path, text: str) -> bool:
    return _atomic_write_bytes(path, text.encode("utf-8"))


def _restore_file(path: Path, previous: Optional[bytes]) -> None:
    if previous is None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.exception("回滚删除 %s 失败", path)
    elif not _atomic_write_bytes(path, previous):
        log.error("回滚凭据文件 %s 失败", path)


def _read_bytes_or_none(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


def qrcode_generate() -> dict:
    """生成二维码。返回 {'url','qrcode_key'}。"""
    resp = governed_get(_http_get, QR_GENERATE, kind="auth",
                        headers=BROWSER_HEADERS, timeout=10)
    data = resp.json()
    governor.note_api_code(data.get("code"), endpoint=QR_GENERATE)
    if data.get("code") != 0:
        raise RuntimeError(f"生成二维码失败: {data.get('message')}")
    d = data["data"]
    return {"url": d["url"], "qrcode_key": d["qrcode_key"]}


def qrcode_poll(qrcode_key: str) -> dict:
    """轮询扫码状态。内部结果含凭据，路由层必须白名单输出。"""
    resp = governed_get(_http_get, QR_POLL, kind="auth",
                        params={"qrcode_key": qrcode_key},
                        headers=BROWSER_HEADERS, timeout=10)
    data = resp.json()
    governor.note_api_code(data.get("code"), endpoint=QR_POLL)
    set_cookie = resp.headers.get("Set-Cookie", "")
    return parse_poll_response(data, set_cookie)


def parse_poll_response(data: dict, set_cookie_header: str) -> dict:
    """把 poll 响应解析成内部统一状态（凭据绝不可直接回显）。"""
    code = str(data.get("data", {}).get("code", -1))
    status = POLL_CODES.get(code, "unknown")
    sessdata = ""
    dede_userid = ""
    bili_jct = ""
    if status == "success":
        from urllib.parse import parse_qs, urlparse
        url = data.get("data", {}).get("url", "")
        qs = parse_qs(urlparse(url).query)
        if qs.get("SESSDATA"):
            sessdata = qs["SESSDATA"][0]
        dede_userid = (qs.get("DedeUserID") or [""])[0]
        bili_jct = (qs.get("bili_jct") or [""])[0]
        if not sessdata:
            match = re.search(r"SESSDATA=([^;]+)", set_cookie_header or "")
            sessdata = match.group(1) if match else ""
        if not bili_jct:
            match = re.search(r"bili_jct=([^;]+)", set_cookie_header or "")
            bili_jct = match.group(1) if match else ""
    return {"status": status, "sessdata": sessdata, "dede_userid": dede_userid,
            "bili_jct": bili_jct, "message": POLL_MESSAGES.get(code, "未知状态")}


def save_session(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> bool:
    """原子保存 QR 会话并选择 qr；任一步失败都不提交新认证上下文。"""
    if not isinstance(sessdata, str) or not sessdata:
        return False
    with _switch_lock:
        previous = _read_bytes_or_none(SESSION_COOKIE_FILE)
        current = get_auth_context()
        if not storage.write_json(SESSION_COOKIE_FILE, {
                "SESSDATA": sessdata, "DedeUserID": str(dede_userid or ""),
                "bili_jct": str(bili_jct or "")}, indent=1):
            return False
        candidate = _write_state_and_commit_locked(
            current, "qr", identifier=str(dede_userid or ""))
        if candidate is None:
            _restore_file(SESSION_COOKIE_FILE, previous)
            return False
        try:
            _qr_cookie_file().unlink(missing_ok=True)
        except OSError:
            log.exception("清理旧 QR cookie 导出失败")
    _reset_identity_data_caches()
    return True


def load_session() -> Optional[dict]:
    value = storage.read_json(SESSION_COOKIE_FILE, default=None)
    return value if isinstance(value, dict) else None


def clear_session() -> None:
    """清 QR 凭据并回退到导入 Cookie 或浏览器来源（兼容旧调用语义）。"""
    global _cj_cache
    with _switch_lock:
        current = get_auth_context()
        old_session = _read_bytes_or_none(SESSION_COOKIE_FILE)
        try:
            SESSION_COOKIE_FILE.unlink(missing_ok=True)
            _qr_cookie_file().unlink(missing_ok=True)
        except OSError as exc:
            _restore_file(SESSION_COOKIE_FILE, old_session)
            raise RuntimeError(f"清理扫码凭据失败:{exc}") from exc
        fallback = "cookie_file" if _valid_cookie_file(COOKIE_FILE) else "browser"
        identifier = (_cookie_value_from_text(_read_cookie_file(COOKIE_FILE), "DedeUserID")
                      if fallback == "cookie_file" else "")
        if _write_state_and_commit_locked(current, fallback,
                                          identifier=identifier) is None:
            _restore_file(SESSION_COOKIE_FILE, old_session)
            raise RuntimeError("切换认证来源失败:状态未写入磁盘")
    # 保留显式锁与 reset_data_caches 调用，锁外清 API 域，避免锁序反转。
    with _session_lock:
        _cj_cache = {"cj": None, "ts": 0.0, "context": None}
    import bilibili_api
    bilibili_api.reset_data_caches()


def import_cookies_txt(text: str) -> bool:
    """原子保存用户导入 Cookie 并显式选择 cookie_file。"""
    if not isinstance(text, str) or not _cookie_value_from_text(text, "SESSDATA"):
        return False
    with _switch_lock:
        current = get_auth_context()
        previous = _read_bytes_or_none(COOKIE_FILE)
        if not _atomic_write_text(COOKIE_FILE, text):
            return False
        identifier = _cookie_value_from_text(text, "DedeUserID")
        if _write_state_and_commit_locked(current, "cookie_file",
                                          identifier=identifier) is None:
            _restore_file(COOKIE_FILE, previous)
            return False
    _reset_identity_data_caches()
    return True


def select_browser() -> bool:
    """显式选择浏览器 Cookie；即使 COOKIE_FILE 存在也不会被它抢占。"""
    with _switch_lock:
        current = get_auth_context()
        if _write_state_and_commit_locked(current, "browser") is None:
            return False
    _reset_identity_data_caches()
    return True


def logout() -> bool:
    """删除 QR/导入/QR 导出凭据并持久化选择 none。"""
    paths = (SESSION_COOKIE_FILE, COOKIE_FILE, _qr_cookie_file())
    with _switch_lock:
        current = get_auth_context()
        previous = {path: _read_bytes_or_none(path) for path in paths}
        try:
            for path in paths:
                path.unlink(missing_ok=True)
        except OSError:
            for path, payload in previous.items():
                _restore_file(path, payload)
            log.exception("删除登录凭据失败")
            return False
        if _write_state_and_commit_locked(current, "none") is None:
            for path, payload in previous.items():
                _restore_file(path, payload)
            return False
    _reset_identity_data_caches()
    return True


def has_session() -> bool:
    return _valid_session(load_session())


def write_cookies_txt(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> None:
    """把 QR 凭据导出到独立 Netscape 文件，供 yt-dlp 使用。"""
    lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/docs/http-cookies.html",
        ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t" + sessdata,
    ]
    if dede_userid:
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tDedeUserID\t{dede_userid}")
    if bili_jct:
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\t{bili_jct}")
    if not _atomic_write_text(_qr_cookie_file(), "\n".join(lines) + "\n"):
        raise RuntimeError("QR Cookie 导出失败")


def get_ytdlp_cookie_arg(context: Optional[dict] = None) -> dict:
    """按显式 source 返回 yt-dlp 选项；不会按文件存在顺序猜来源。"""
    context = dict(context) if isinstance(context, dict) else get_auth_context()
    source = context.get("source")
    if source == "qr":
        session = load_session()
        if not _valid_session(session):
            return {}
        write_cookies_txt(session["SESSDATA"], session.get("DedeUserID", ""),
                          session.get("bili_jct", ""))
        return {"cookiefile": str(_qr_cookie_file())}
    if source == "cookie_file":
        return {"cookiefile": str(COOKIE_FILE)} if _valid_cookie_file(COOKIE_FILE) else {}
    if source == "browser":
        return {"cookiesfrombrowser": (BROWSER_COOKIE_SOURCE,)}
    return {}


def _upgrade_account_context(context: dict, identifier) -> bool:
    fingerprint = _account_fingerprint(identifier)
    if not fingerprint:
        return False
    if (context.get("authenticated") is True
            and context.get("fingerprint") == fingerprint
            and is_auth_context_current(context)):
        return True
    with _switch_lock:
        with _session_lock:
            live = _get_auth_context_locked()
            if not _same_context(live, context):
                return False
            candidate = _make_context(
                live["source"], int(live["generation"]) + 1,
                incarnation=live["incarnation"], identifier=identifier)
            if not storage.write_json(_auth_state_file(), candidate, indent=1):
                log.error("认证账号 fingerprint 升级未能持久化")
                return False
            global _cj_cache
            _auth_state_cache.update(path=_auth_state_file(), context=candidate)
            _cj_cache = {"cj": None, "ts": 0.0, "context": None}
    _reset_identity_data_caches()
    return True


def get_cookiejar(context: Optional[dict] = None):
    """返回绑定当前 (fingerprint,incarnation,generation) 的 cookiejar。"""
    context = dict(context) if isinstance(context, dict) else get_auth_context()
    jar = None
    dede_userid = ""
    with _session_lock:
        if not _is_auth_context_current_locked(context):
            return None
        if (_cj_cache["cj"] is not None
                and _same_context(_cj_cache.get("context"), context)
                and time.time() - _cj_cache["ts"] < 300):
            return _cj_cache["cj"]
        cookie_arg = get_ytdlp_cookie_arg(context)
        if "cookiefile" in cookie_arg:
            try:
                import http.cookiejar
                jar = http.cookiejar.MozillaCookieJar(cookie_arg["cookiefile"])
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                jar = None
        elif "cookiesfrombrowser" in cookie_arg:
            try:
                with yt_dlp.YoutubeDL({"quiet": True, **cookie_arg}) as ydl:
                    jar = ydl.cookiejar
            except Exception:
                jar = None
        if jar is not None and _is_auth_context_current_locked(context):
            _cj_cache.update(cj=jar, ts=time.time(), context=dict(context))
            dede_userid = _cookiejar_value(jar, "DedeUserID")
    if dede_userid:
        _upgrade_account_context(context, dede_userid)
    return jar


def _public_context_fields(context: dict) -> dict:
    authenticated = bool(context.get("authenticated"))
    return {
        "auth_source": context.get("source", "none"),
        "account_fingerprint": context.get("fingerprint", "") if authenticated else "",
        "auth_incarnation": str(context.get("incarnation") or ""),
        "auth_generation": int(context.get("generation", 0) or 0),
        "authenticated": authenticated,
    }


def get_login_status() -> dict:
    """调用 nav 获取登录信息；仅返回非敏感身份元数据。"""
    context, cookies = get_auth_snapshot()
    if cookies is None:
        return {"logged_in": False, **_public_context_fields(context)}
    try:
        resp = governed_get(
            requests.get, NAV_API, kind="auth", cookies=cookies,
            headers={"User-Agent": BROWSER_HEADERS["User-Agent"]}, timeout=10)
        payload = resp.json()
        governor.note_api_code(payload.get("code"), endpoint=NAV_API)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        logged_in = bool(data.get("isLogin", False))
        mid = data.get("mid") if logged_in else None
        if mid:
            upgraded = _upgrade_account_context(context, mid)
            live = get_auth_context()
            if (not upgraded
                    or live.get("fingerprint") != _account_fingerprint(mid)):
                raise AuthContextChangedError("登录状态已变化，请重试")
            context = live
        elif not is_auth_context_current(context):
            raise AuthContextChangedError("登录状态已变化，请重试")
        return {
            "logged_in": logged_in,
            "uname": data.get("uname", ""),
            "vip": data.get("vipStatus", 0) == 1,
            "vip_type": data.get("vipType", 0),
            **_public_context_fields(context),
        }
    except (UpstreamRateLimitError, AuthContextChangedError):
        raise
    except Exception as exc:
        live = get_auth_context()
        return {"logged_in": False, "error": str(exc),
                **_public_context_fields(live)}
