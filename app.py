"""Flask 路由层。薄,业务委托给 bilibili_auth / downloader。"""
import functools
import io
import ipaddress
import json
import logging
import math
import re
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Optional
from urllib.parse import urlsplit

# 必须早于任何会读取/迁移 data 状态的模块。否则第二实例即使最终因端口冲突
# 退出，也可能已经把第一实例的活跃任务改写为 interrupted。
from instance_lock import AlreadyRunningError, acquire_instance_lock

try:
    acquire_instance_lock()
except AlreadyRunningError as exc:
    raise SystemExit(f"启动失败：{exc}") from exc

from flask import Flask, request, jsonify, render_template, Response, make_response, stream_with_context
from werkzeug.exceptions import HTTPException

import qrcode
import auto_refresh
import pagecache
import parsing
import up_list
import bilibili_auth as auth
import bilibili_api as bapi
import downloader
import taskstore
from config import PORT, BROWSER_COOKIE_SOURCE
import config  # DOWNLOAD_DIR 运行时可变,用 config.DOWNLOAD_DIR 实时读
from request_governor import UpstreamRateLimitError, governor

# 日志:控制台 + 有界轮转文件，避免无人值守运行时 app.log 无限增长。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(
            config.DATA_DIR / "app.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bd.app")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_URL_LENGTH = 8192
_MAX_TEXT_LENGTH = 256
_MAX_BATCH_ITEMS = parsing.MAX_PLAYLIST_ITEMS
_MAX_COOKIE_BYTES = 1024 * 1024
_MAX_SSE_CONNECTIONS = 16
_sse_slots = threading.BoundedSemaphore(_MAX_SSE_CONNECTIONS)
_manual_check_lock = threading.Lock()
_manual_check_thread: Optional[threading.Thread] = None


class ApiError(ValueError):
    """可安全回显的 API 输入/状态错误。"""

    def __init__(self, status: int, code: str, message: str, *, field: str = None):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.field = field


def _json_error(message: str, status: int, code: str, *, field: str = None,
                **extra):
    body = {"ok": False, "error": str(message), "code": str(code), **extra}
    if field:
        body["field"] = field
    response = jsonify(body)
    response.status_code = int(status)
    return response


def _json_object() -> dict:
    """严格读取 JSON object；不再把空 body、坏 JSON、数组根混成 ``{}``。"""
    if not request.is_json:
        raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
    return body


def _string(value, field: str, *, required: bool = False,
            max_length: int = _MAX_TEXT_LENGTH, strip: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_field", f"{field} 必须是字符串", field=field)
    result = value.strip() if strip else value
    if required and not result:
        raise ApiError(400, "missing_field", f"缺少 {field}", field=field)
    if len(result) > max_length:
        raise ApiError(400, "field_too_long",
                       f"{field} 长度不能超过 {max_length}", field=field)
    return result


def _integer(value, field: str, *, default=None, minimum=None, maximum=None) -> int:
    if value is None or value == "":
        if default is None:
            raise ApiError(400, "missing_field", f"缺少 {field}", field=field)
        value = default
    if isinstance(value, bool):
        raise ApiError(400, "invalid_field", f"{field} 必须是整数", field=field)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        result = int(value.strip())
    else:
        raise ApiError(400, "invalid_field", f"{field} 必须是整数", field=field)
    if minimum is not None and result < minimum:
        raise ApiError(400, "invalid_field", f"{field} 不能小于 {minimum}", field=field)
    if maximum is not None and result > maximum:
        raise ApiError(400, "invalid_field", f"{field} 不能大于 {maximum}", field=field)
    return result


def _number(value, field: str, *, default=None, minimum=None, maximum=None):
    """读取有限数值；整数保持 int，小数保持 float。"""
    if value is None or value == "":
        if default is None:
            raise ApiError(400, "missing_field", f"缺少 {field}", field=field)
        value = default
    if isinstance(value, bool):
        raise ApiError(400, "invalid_field", f"{field} 必须是有限数值", field=field)
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        result = value
    elif isinstance(value, str):
        token = value.strip()
        try:
            result = int(token) if re.fullmatch(r"[+-]?\d+", token) else float(token)
        except (TypeError, ValueError, OverflowError):
            raise ApiError(
                400, "invalid_field", f"{field} 必须是有限数值", field=field)
    else:
        raise ApiError(400, "invalid_field", f"{field} 必须是有限数值", field=field)
    if isinstance(result, float) and not math.isfinite(result):
        raise ApiError(400, "invalid_field", f"{field} 必须是有限数值", field=field)
    if minimum is not None and result < minimum:
        raise ApiError(400, "invalid_field", f"{field} 不能小于 {minimum}", field=field)
    if maximum is not None and result > maximum:
        raise ApiError(400, "invalid_field", f"{field} 不能大于 {maximum}", field=field)
    return result


def _boolean(value, field: str, *, default=None) -> bool:
    if value is None:
        if default is None:
            raise ApiError(400, "missing_field", f"缺少 {field}", field=field)
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("true", "1"):
            return True
        if token in ("false", "0"):
            return False
    raise ApiError(400, "invalid_field",
                   f"{field} 必须是 true、false、1 或 0", field=field)


def _string_list(value, field: str, *, max_items: int = _MAX_BATCH_ITEMS,
                 item_max_length: int = _MAX_TEXT_LENGTH) -> list[str]:
    if not isinstance(value, list):
        raise ApiError(400, "invalid_field", f"{field} 必须是数组", field=field)
    if len(value) > max_items:
        raise ApiError(400, "too_many_items",
                       f"{field} 最多允许 {max_items} 项", field=field)
    return [_string(item, f"{field}[{index}]", required=True,
                    max_length=item_max_length)
            for index, item in enumerate(value)]


def _request_auth_context():
    """生产请求绑定账号快照；测试维持既有零参数 monkeypatch 契约。"""
    if app.testing:
        return None
    context, _cookiejar = auth.get_auth_snapshot()
    return context


def _account_call(func, *args, context=None, **kwargs):
    if context is None:
        return func(*args, **kwargs)
    return func(*args, context=context, **kwargs)


def _loopback_authority(authority: str):
    """严格解析 Host/URL authority，只接受本机三个显式名称。

    不用 ``startswith('localhost')``，避免 localhost.evil、userinfo、路径和
    非法端口绕过；IPv6 必须使用方括号形式。返回规范化 host 与可选端口。
    """
    raw = str(authority or "")
    if not raw or raw != raw.strip():
        return None
    if any(ch.isspace() for ch in raw) or any(ch in raw for ch in "/\\?#"):
        return None
    try:
        parsed = urlsplit("//" + raw, allow_fragments=False)
        if (not parsed.netloc or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            return None
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if not host:
        return None

    # urlsplit 会把少数不完整的 ``host:`` 当作“没有端口”，这里补上语法约束。
    if raw.startswith("["):
        close = raw.find("]")
        if close < 0:
            return None
        tail = raw[close + 1:]
        if tail and (not tail.startswith(":") or not tail[1:].isdigit()):
            return None
    else:
        if raw.count(":") > 1:  # 未加 [] 的 IPv6
            return None
        if ":" in raw and not raw.rsplit(":", 1)[1].isdigit():
            return None
    if port is not None and not 1 <= port <= 65535:
        return None

    host_lower = host.lower()
    if host_lower == "localhost":
        normalized = "localhost"
    else:
        try:
            address = ipaddress.ip_address(host_lower)
        except ValueError:
            return None
        if address not in (ipaddress.ip_address("127.0.0.1"),
                           ipaddress.ip_address("::1")):
            return None
        normalized = address.compressed
    return normalized, port


def _origin_key(value: str, *, allow_path: bool = False):
    """把 Origin/Referer 归一为 (scheme, host, effective_port)。"""
    original = str(value or "")
    raw = original.strip()
    if not raw or raw != original:
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in _DEFAULT_PORTS or not parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if not allow_path and (parsed.path not in ("", "/")
                               or parsed.query or parsed.fragment):
            return None
        authority = _loopback_authority(parsed.netloc)
    except (TypeError, ValueError):
        return None
    if authority is None:
        return None
    host, port = authority
    return scheme, host, port or _DEFAULT_PORTS[scheme]


def _request_origin_key():
    scheme = str(request.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    authority = _loopback_authority(request.headers.get("Host", ""))
    if authority is None:
        return None
    host, port = authority
    return scheme, host, port or _DEFAULT_PORTS[scheme]


@app.before_request
def local_request_guard():
    """阻断 DNS rebinding 与跨站页面调用本机有副作用的 API。

    Host 对所有请求都强制为 loopback；浏览器来源头存在时必须严格同源。
    三类来源头都缺失仍允许，以兼容 Flask test_client、curl 和本机脚本。
    """
    expected = _request_origin_key()
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if expected is None:
        return _json_error("仅允许本机访问", 403, "local_only")
    if fetch_site is not None and fetch_site.strip().lower() not in ("same-origin", "none"):
        return _json_error("拒绝跨站请求", 403, "cross_site_request")

    origin = request.headers.get("Origin")
    if origin is not None and _origin_key(origin) != expected:
        return _json_error("请求来源与本机服务不同源", 403, "origin_mismatch")
    referer = request.headers.get("Referer")
    if referer is not None and _origin_key(referer, allow_path=True) != expected:
        return _json_error("请求来源与本机服务不同源", 403, "origin_mismatch")
    return None


def _capacity_error(exc, code: str):
    return _json_error(
        str(exc), 409, code,
        capacity=getattr(exc, "capacity", ""),
        limit=getattr(exc, "limit", 0),
        current=getattr(exc, "current", 0),
        requested=getattr(exc, "requested", 0),
    )


def api_errors(f):
    """统一异常出口：输入/认证竞态/上游风控均使用稳定 JSON code。"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ApiError as exc:
            return _json_error(exc.message, exc.status, exc.code, field=exc.field)
        except taskstore.TaskCapacityError as exc:
            return _capacity_error(exc, "task_capacity_exceeded")
        except up_list.UpListCapacityError as exc:
            return _capacity_error(exc, "up_list_capacity_exceeded")
        except auth.AuthContextChangedError as exc:
            return _json_error(str(exc), 409, "auth_context_changed")
        except bapi.NotLoggedInError as exc:
            # 未登录/凭据缺失是预期态:回 401 让前端提示「请先登录」,
            # 不能与真正的未知异常一起折叠成 500 + log.exception 噪音。
            return _json_error(str(exc), 401, "auth_required")
        except UpstreamRateLimitError as exc:
            seconds = float(exc.retry_after or 0.0)
            if not math.isfinite(seconds) or seconds <= 0:
                seconds = 1.0
            retry_after = max(1, math.ceil(seconds))
            log.warning("路由 %s 遇到 B 站风控 status=%s retry_after=%ss",
                        f.__name__, exc.status_code, retry_after)
            response = _json_error(str(exc), 429, "upstream_rate_limited",
                                   retry_after=retry_after)
            response.headers["Retry-After"] = str(retry_after)
            return response
        except Exception:
            log.exception("路由 %s 异常", f.__name__)
            return _json_error("服务器处理失败，请查看应用日志", 500,
                               "internal_error")
    return wrapper


@app.errorhandler(ApiError)
def handle_api_error(exc):
    return _json_error(exc.message, exc.status, exc.code, field=exc.field)


@app.errorhandler(taskstore.TaskCapacityError)
def handle_task_capacity_error(exc):
    return _capacity_error(exc, "task_capacity_exceeded")


@app.errorhandler(up_list.UpListCapacityError)
def handle_up_list_capacity_error(exc):
    return _capacity_error(exc, "up_list_capacity_exceeded")


@app.errorhandler(auth.AuthContextChangedError)
def handle_auth_context_changed(exc):
    return _json_error(str(exc), 409, "auth_context_changed")


@app.errorhandler(HTTPException)
def handle_http_error(exc):
    if not request.path.startswith("/api/"):
        return exc
    codes = {400: "bad_request", 404: "not_found", 405: "method_not_allowed",
             413: "payload_too_large"}
    message = "请求体过大" if exc.code == 413 else (exc.description or exc.name)
    return _json_error(message, exc.code or 500,
                       codes.get(exc.code, "http_error"))


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if request.path.startswith("/api/"):
        log.exception("未捕获的 API 异常", exc_info=exc)
        return _json_error("服务器处理失败，请查看应用日志", 500,
                           "internal_error")
    log.exception("页面请求异常", exc_info=exc)
    return "Internal Server Error", 500


@app.after_request
def disable_api_caching(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@app.route("/")
def index():
    # 首页必须每次重新渲染:模板改动(结构/类名/资源版本号)要立刻生效。
    # 原来没有任何缓存头,浏览器按启发式缓存直接复用旧文档,新模板与新的
    # ?v= 资源版本都对不上,表现为「改了没反应」。
    response = make_response(render_template("index.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- 登录 ----------
@app.route("/api/login/status")
@api_errors
def login_status():
    return jsonify(auth.get_login_status())


@app.route("/api/login/qrcode")
@api_errors
def login_qrcode():
    return jsonify({"ok": True, **auth.qrcode_generate()})


@app.route("/api/governor/status")
def governor_status():
    """只暴露请求治理器的非敏感运行指标，便于判断是否仍在冷却。"""
    snapshot = governor.snapshot()
    response = jsonify({
        "ok": True,
        "cooldown_until": snapshot.cooldown_until,
        "cooldown_seconds": snapshot.cooldown_seconds,
        "in_flight": snapshot.in_flight,
        "total_requests": snapshot.total_requests,
        "rate_limit_events": snapshot.rate_limit_events,
        "last_status": snapshot.last_status,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/auto-refresh/status")
def auto_refresh_status():
    """只读生命周期状态；不会触发 B 站请求或排队新一轮检查。"""
    return jsonify({"ok": True, **auto_refresh.get_status()})


@app.route("/api/login/qrcode-image")
@api_errors
def login_qrcode_image():
    """使用 generate 返回的同一 URL 画码，并严格限制为 B 站护照域名。"""
    url = _string(request.args.get("url"), "url", required=True,
                  max_length=_MAX_URL_LENGTH)
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https"
                 and (parsed.hostname or "").lower() == "passport.bilibili.com"
                 and parsed.username is None and parsed.password is None
                 and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise ApiError(400, "invalid_url", "缺少/非法 url", field="url")
    try:
        img = qrcode.make(url)
    except Exception as exc:
        # 合法域名但超出二维码编码容量(qrcode 库抛 ValueError):是请求问题,
        # 必须回 400,不能落进兜底 500 还记一条异常日志。
        raise ApiError(400, "invalid_url",
                       "url 过长，无法编码为二维码", field="url") from exc
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png")


@app.route("/api/login/qrcode/poll")
@api_errors
def login_qrcode_poll():
    key = _string(request.args.get("key"), "key", required=True, max_length=512)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ApiError(400, "invalid_field", "二维码 key 格式无效", field="key")
    result = auth.qrcode_poll(key)
    status = str(result.get("status") or "unknown")
    message = str(result.get("message") or "未知状态")
    if status == "success":
        sessdata = result.get("sessdata")
        if not sessdata:
            raise ApiError(502, "invalid_upstream_response",
                           "扫码成功但上游未返回登录凭据")
        if not auth.save_session(sessdata, result.get("dede_userid", ""),
                                 result.get("bili_jct", "")):
            raise ApiError(500, "credential_persist_failed",
                           "登录凭据保存失败，请检查磁盘后重试")
    # 只白名单输出状态；SESSDATA / DedeUserID / bili_jct 永不进入 HTTP 响应。
    return jsonify({"status": status, "message": message})


@app.route("/api/login/use-browser", methods=["POST"])
@api_errors
def login_use_browser():
    if not auth.select_browser():
        raise ApiError(500, "auth_persist_failed", "切换浏览器登录态失败")
    return jsonify({"ok": True, "mode": BROWSER_COOKIE_SOURCE})


@app.route("/api/login/cookie-txt", methods=["POST"])
@api_errors
def login_cookie_txt():
    content_length = request.content_length
    if content_length is not None and content_length > _MAX_COOKIE_BYTES:
        raise ApiError(413, "payload_too_large", "cookies.txt 不能超过 1 MiB")
    text = request.get_data(as_text=True)
    if len(text.encode("utf-8")) > _MAX_COOKIE_BYTES:
        raise ApiError(413, "payload_too_large", "cookies.txt 不能超过 1 MiB")
    if "SESSDATA" not in text:
        raise ApiError(400, "missing_cookie", "未找到 SESSDATA", field="SESSDATA")
    if not auth.import_cookies_txt(text):
        raise ApiError(500, "credential_persist_failed", "Cookie 导入失败，凭据未写入")
    return jsonify({"ok": True})


@app.route("/api/login/logout", methods=["POST"])
@api_errors
def login_logout():
    if not auth.logout():
        raise ApiError(500, "credential_persist_failed", "退出失败，登录状态未完整清理")
    return jsonify({"ok": True})


# ---------- 解析/下载 ----------
@app.route("/api/parse", methods=["POST"])
@api_errors
def parse():
    body = _json_object()
    url = _string(body.get("url"), "url", required=True,
                  max_length=_MAX_URL_LENGTH)
    info = downloader.parse_video(url)
    status = auth.get_login_status()
    return jsonify({"ok": True, "info": info,
                    "logged_in": status.get("logged_in", False)})


@app.route("/api/parse-part", methods=["POST"])
@api_errors
def parse_part():
    """多 P 视频选了具体分 P 后，拉该 P 的清晰度。"""
    body = _json_object()
    url = _string(body.get("url"), "url", required=True,
                  max_length=_MAX_URL_LENGTH)
    page = _integer(body.get("page"), "page", default=1,
                    minimum=1, maximum=1_000_000)
    return jsonify({"ok": True, "info": downloader.parse_part(url, page)})


@app.route("/api/download", methods=["POST"])
@api_errors
def download():
    body = _json_object()
    url = _string(body.get("url"), "url", required=True,
                  max_length=_MAX_URL_LENGTH)
    label = _string(body.get("label"), "label", max_length=_MAX_TEXT_LENGTH)
    page = _integer(body.get("page"), "page", default=1,
                    minimum=1, maximum=1_000_000)
    uploader_mid = _integer(body.get("uploader_mid"), "uploader_mid", default=0,
                            minimum=0, maximum=10 ** 20)
    existing = downloader.find_active_by_url(url, label, page)
    if existing:
        return _json_error("该视频相同清晰度正在下载中", 409,
                           "duplicate_task", task_id=existing)
    try:
        task_id = downloader.start_download(
            url, label, page, uploader_mid=uploader_mid)
    except downloader.DuplicateActiveTask as exc:
        return _json_error(str(exc), 409, "duplicate_task", task_id=exc.task_id)
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/cancel/<task_id>", methods=["POST"])
@api_errors
def cancel(task_id):
    task_id = _string(task_id, "task_id", required=True, max_length=_MAX_TEXT_LENGTH)
    ok = downloader.cancel_download(task_id)
    return jsonify({"ok": ok})


@app.route("/api/task/delete", methods=["POST"])
@api_errors
def task_delete():
    """删除任务。{ids:[...], delete_file:bool}"""
    body = _json_object()
    task_ids = _string_list(body.get("ids"), "ids", item_max_length=_MAX_TEXT_LENGTH)
    delete_file = _boolean(body.get("delete_file"), "delete_file", default=False)
    if not task_ids:
        raise ApiError(400, "missing_field", "未选择任务", field="ids")
    result = downloader.delete_tasks_detailed(task_ids, delete_file)
    return jsonify({"ok": True, **result})


@app.route("/api/tasks/clear", methods=["POST"])
@api_errors
def tasks_clear():
    """清除所有已完成/失败记录。{delete_file:bool}"""
    body = _json_object()
    delete_file = _boolean(body.get("delete_file"), "delete_file", default=False)
    count = downloader.clear_tasks(delete_file)
    return jsonify({"ok": True, "count": count})


@app.route("/api/tasks/clean-invalid", methods=["POST"])
@api_errors
def tasks_clean_invalid():
    """删除失败/失效记录:下载失败的任务 + 已完成但本地文件已不存在(被删/改名)的任务。
    只删记录,不碰文件(失效的本就没有文件)。"""
    return jsonify({"ok": True, **downloader.clean_invalid_records()})


# ---------- 批量 ----------
@app.route("/api/batch/parse", methods=["POST"])
@api_errors
def batch_parse():
    """批量解析：输入及累计输出都最多允许共享边界数量。"""
    body = _json_object()
    urls_value = body.get("urls")
    single_url = _string(body.get("url"), "url", max_length=_MAX_URL_LENGTH)
    urls = None
    if urls_value is not None:
        urls = _string_list(urls_value, "urls", max_items=_MAX_BATCH_ITEMS,
                            item_max_length=_MAX_URL_LENGTH)

    items = []
    if urls:
        for url in urls:
            try:
                result = downloader.resolve_playlist(url)
                additions = result["items"]
            except UpstreamRateLimitError:
                raise
            except Exception as exc:
                additions = [{"title": f"(解析失败: {str(exc)[:40]})",
                              "url": url, "duration": 0,
                              "error": str(exc)[:80]}]
            # 必须在宽泛 except 之外检查，避免本路由抛出的 ApiError 被当成
            # 某个 URL 的普通解析失败吞掉后继续扩张输出。
            if len(items) + len(additions) > _MAX_BATCH_ITEMS:
                raise ApiError(
                    400, "too_many_items",
                    f"累计解析结果超过 {_MAX_BATCH_ITEMS} 项，请分批处理")
            items.extend(additions)
    elif single_url:
        result = downloader.resolve_playlist(single_url)
        resolved = result.get("items") or []
        if len(resolved) > _MAX_BATCH_ITEMS:
            raise ApiError(400, "too_many_items",
                           f"解析结果超过 {_MAX_BATCH_ITEMS} 项，请分批处理")
        return jsonify({"ok": True, "items": resolved,
                        "title": result.get("title", "")})
    else:
        raise ApiError(400, "missing_field", "请提供 url 或 urls")
    return jsonify({"ok": True, "items": items})


@app.route("/api/batch/download", methods=["POST"])
@api_errors
def batch_download():
    """批量启动下载。{items:[{url,title,page}], label}"""
    body = _json_object()
    raw_items = body.get("items")
    if not isinstance(raw_items, list):
        raise ApiError(400, "invalid_field", "items 必须是数组", field="items")
    if not raw_items:
        raise ApiError(400, "missing_field", "没有待下载项", field="items")
    if len(raw_items) > _MAX_BATCH_ITEMS:
        raise ApiError(400, "too_many_items",
                       f"items 最多允许 {_MAX_BATCH_ITEMS} 项", field="items")
    label = _string(body.get("label"), "label", max_length=_MAX_TEXT_LENGTH)
    items = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ApiError(400, "invalid_field",
                           f"items[{index}] 必须是对象", field=f"items[{index}]")
        item = {
            "url": _string(raw.get("url"), f"items[{index}].url", required=True,
                           max_length=_MAX_URL_LENGTH),
            "title": _string(raw.get("title"), f"items[{index}].title",
                             max_length=_MAX_TEXT_LENGTH),
            "page": _integer(raw.get("page"), f"items[{index}].page", default=1,
                             minimum=1, maximum=1_000_000),
            "thumbnail": _string(raw.get("thumbnail"),
                                 f"items[{index}].thumbnail", max_length=_MAX_URL_LENGTH),
            "uploader": _string(raw.get("uploader"), f"items[{index}].uploader",
                                max_length=_MAX_TEXT_LENGTH),
            "uploader_mid": _integer(raw.get("uploader_mid"),
                                     f"items[{index}].uploader_mid", default=0,
                                     minimum=0, maximum=10 ** 20),
            "duration": _number(raw.get("duration"), f"items[{index}].duration",
                                default=0, minimum=0, maximum=10 ** 9),
            "bvid": _string(raw.get("bvid"), f"items[{index}].bvid", max_length=128),
            "source": _string(raw.get("source"), f"items[{index}].source",
                              max_length=128),
            "auto_attempt": _integer(raw.get("auto_attempt"),
                                     f"items[{index}].auto_attempt", default=0,
                                     minimum=0, maximum=100),
        }
        items.append(item)
    task_ids = downloader.start_batch(items, label)
    return jsonify({"ok": True, "task_ids": task_ids, "count": len(task_ids)})


# ---------- 内容浏览(慢数据源统一「缓存优先 + 后台拉新」) ----------

@app.route("/api/cache/<key>")
@api_errors
def cache_get(key):
    """读取当前账号 exact 完整认证上下文的上次结果；miss 仍保持 HTTP 200。"""
    context = _request_auth_context()
    entry = pagecache.load(key, context=context)
    if not entry:
        return jsonify({"ok": False})
    return jsonify({"ok": True, "saved_at": entry.get("saved_at"),
                    "data": entry.get("data")})


@app.route("/api/history")
@api_errors
def history():
    """观看历史。?page=1&size=50"""
    page = _integer(request.args.get("page"), "page", default=1,
                    minimum=1, maximum=1_000_000)
    size = _integer(request.args.get("size"), "size", default=50,
                    minimum=1, maximum=100)
    context = _request_auth_context()
    items = _account_call(bapi.fetch_history, page, size, context=context)
    return jsonify({"ok": True, "items": items})


@app.route("/api/follow-updates")
@api_errors
def follow_updates():
    """关注 UP 最近投稿。?force=true 跳过内存缓存。"""
    force = _boolean(request.args.get("force"), "force", default=False)
    context = _request_auth_context()
    items = _account_call(bapi.fetch_follow_updates, force=force, context=context)
    pagecache.save("follow_updates", {"ok": True, "items": items},
                   context=context)
    return jsonify({"ok": True, "items": items})


@app.route("/api/home-feed")
@api_errors
def home_feed():
    """首页推荐流（登录态个性化）。?fresh_idx=1 翻页递增，?ps=12 每页条数(1..30)。

    推荐内容即时性强，翻页不落缓存；但 B 站风控时 rcmd 接口不报错、只返回
    空列表，裸等现拉会让首屏停在空态。fresh_idx=1 拿到非空结果时落一份
    账号级磁盘缓存，供下次打开先出内容再后台刷新。
    ps 供前端按视口高度首屏一次要够铺满整页的条数，避免多次往返。
    """
    fresh_idx = _integer(request.args.get("fresh_idx"), "fresh_idx", default=1,
                         minimum=1, maximum=10_000)
    ps = _integer(request.args.get("ps"), "ps", default=12, minimum=1, maximum=30)
    context = _request_auth_context()
    items = _account_call(bapi.fetch_home_feed, fresh_idx, ps=ps, context=context)
    if fresh_idx == 1 and items:
        pagecache.save("home_feed", {"ok": True, "items": items}, context=context)
    return jsonify({"ok": True, "items": items})


# ---------- A3: 自定义 UP 清单 ----------

def _parse_mid_from_input(s: str) -> Optional[int]:
    """从主页 URL 或纯 mid 数字中提取 mid。支持:
       - https://space.bilibili.com/12345
       - 12345
    """
    s = (s or "").strip()
    m = re.search(r"space\.bilibili\.com/(\d+)", s)
    if m:
        return int(m.group(1))
    if s.isdigit():
        return int(s)
    return None


@app.route("/api/up-list")
def up_list_get():
    ups = up_list.list_ups()
    # 一次读回全部游标:逐 UP 调 get_last_seen 是「文件大小 × UP 数」的重复解析
    last_seen_map = up_list.get_last_seen_all()
    for u in ups:
        last = last_seen_map.get(str(u["mid"]))
        u["fetched_at"] = (last or {}).get("fetched_at")
    collections = up_list.list_collections()
    for item in collections:
        # 已见集合不下发给前端(可能几百条),只给规模与检查时间
        item["seen_count"] = len(item.pop("seen_bvids", []) or [])
    return jsonify({"ok": True, "ups": ups, "collections": collections})


@app.route("/api/up-list/add", methods=["POST"])
@api_errors
def up_list_add():
    """添加 UP：input 可为主页 URL 或纯 mid。"""
    body = _json_object()
    value = _string(body.get("input"), "input", required=True,
                    max_length=_MAX_URL_LENGTH)
    mid = _parse_mid_from_input(value)
    if not mid:
        raise ApiError(400, "invalid_field",
                       "请输入 B 站主页链接或 UP 的 mid", field="input")
    user = bapi.register_up(mid)
    return jsonify({"ok": True, "up": user})


@app.route("/api/up-list/remove", methods=["POST"])
@api_errors
def up_list_remove():
    body = _json_object()
    mid = _integer(body.get("mid"), "mid", minimum=1, maximum=10 ** 20)
    ok = up_list.remove_up(mid)
    return jsonify({"ok": ok})


@app.route("/api/up-list/set-quality", methods=["POST"])
@api_errors
def up_list_set_quality():
    """设置某 UP 新视频自动下载的默认清晰度；空串表示自动最高。"""
    body = _json_object()
    mid = _integer(body.get("mid"), "mid", minimum=1, maximum=10 ** 20)
    quality = _string(body.get("quality"), "quality", max_length=_MAX_TEXT_LENGTH)
    ok = up_list.set_quality(mid, quality)
    return jsonify({"ok": ok})


@app.route("/api/up-list/reset-seen", methods=["POST"])
@api_errors
def up_list_reset_seen():
    body = _json_object()
    mid = _integer(body.get("mid"), "mid", minimum=1, maximum=10 ** 20)
    up_list.reset_seen(mid)
    return jsonify({"ok": True})


def _collection_args(body: dict):
    """合集订阅路由共用的参数校验。"""
    kind = _string(body.get("kind"), "kind", required=True, max_length=16)
    if kind not in up_list.COLLECTION_KINDS:
        raise ApiError(400, "invalid_field", "kind 必须是 season 或 fav",
                       field="kind")
    cid = _integer(body.get("id"), "id", minimum=1, maximum=10 ** 20)
    return kind, cid


@app.route("/api/collection/add", methods=["POST"])
@api_errors
def collection_add():
    """订阅合集/收藏夹的新入库视频自动下载。{kind, id, title, cover, quality}

    首次订阅以当前全部条目为基线(只下之后新增的),与 UP 订阅语义一致。
    """
    body = _json_object()
    kind, cid = _collection_args(body)
    title = _string(body.get("title"), "title", max_length=_MAX_TEXT_LENGTH)
    cover = _string(body.get("cover"), "cover", max_length=_MAX_URL_LENGTH)
    quality = _string(body.get("quality"), "quality", max_length=32)
    # 已订阅:直接返回,不再拉一次全集内容(省一次上游请求,也避免拉取失败误报 502)
    existing = next((c for c in up_list.list_collections()
                     if c["kind"] == kind and c["id"] == cid), None)
    if existing is not None:
        existing.pop("seen_bvids", None)
        return jsonify({"ok": True, "collection": existing, "baseline_count": 0,
                        "already": True})

    context = _request_auth_context()
    try:
        items = _account_call(
            bapi.fetch_season_content if kind == "season"
            else bapi.fetch_fav_content, cid, context=context)
    except UpstreamRateLimitError:
        # 限流是账号级冷却,必须原样透出(前端按 429 提示),不能伪装成"读不到内容"
        raise
    except auth.AuthContextChangedError:
        raise
    except Exception:
        # 拉不到基线时明确失败:没有基线的订阅会把整个合集一次性下完
        log.exception("合集订阅建立基线失败 kind=%s id=%s", kind, cid)
        raise ApiError(502, "baseline_failed",
                       "无法读取该合集/收藏夹内容，未建立订阅（避免把历史内容全部下载）")
    baseline = [it.get("bvid") for it in items if it.get("bvid")]
    if not baseline:
        # 空基线 = 该订阅此后永远把「当前条目」重建为基线、一条不下:
        # 之后新增的视频会被当成基线吞掉,追更静默失效且用户无自救路径。
        # 明确拒绝,让用户在内容非空时再订阅。
        raise ApiError(400, "empty_collection",
                       "该合集/收藏夹当前没有可读取的视频条目，无法建立基线；"
                       "请确认内容非空后再订阅")
    try:
        entry = up_list.add_collection(kind, cid, title=title, cover=cover,
                                       seen_bvids=baseline, quality=quality)
    except up_list.UpListCapacityError as exc:
        # 容量错误已由全局 handler 转成明确提示;这里补上"未建立订阅"的语境
        raise ApiError(400, "collection_too_large", str(exc))
    entry.pop("seen_bvids", None)
    return jsonify({"ok": True, "collection": entry,
                    "baseline_count": len(baseline)})


@app.route("/api/collection/remove", methods=["POST"])
@api_errors
def collection_remove():
    body = _json_object()
    kind, cid = _collection_args(body)
    ok = up_list.remove_collection(kind, cid)
    return jsonify({"ok": ok})


@app.route("/api/collection/set-quality", methods=["POST"])
@api_errors
def collection_set_quality():
    body = _json_object()
    kind, cid = _collection_args(body)
    quality = _string(body.get("quality"), "quality", max_length=32)
    ok = up_list.set_collection_quality(kind, cid, quality)
    return jsonify({"ok": ok})


@app.route("/api/collection/reset-seen", methods=["POST"])
@api_errors
def collection_reset_seen():
    """清空某合集的已见基线(条目超限或想重新对齐时用)。

    清空后下一轮检查会以当前全部条目重建基线,不会立刻重下历史内容。
    """
    body = _json_object()
    kind, cid = _collection_args(body)
    ok = up_list.reset_collection_seen(kind, cid)
    return jsonify({"ok": ok})


@app.route("/api/up-list/check", methods=["POST"])
def up_list_check():
    """异步触发一轮追更；在返回 started=True 前原子预约业务锁。"""
    global _manual_check_thread

    with _manual_check_lock:
        current = _manual_check_thread
        if current is not None and current.is_alive():
            return jsonify({"ok": True, "started": False,
                            "message": "已有检查在进行中,本次未启动新一轮"})
        _manual_check_thread = None

        reservation = auto_refresh.try_reserve_refresh()
        if reservation is None:
            return jsonify({"ok": True, "started": False,
                            "message": "已有检查在进行中,本次未启动新一轮"})

        def _run():
            global _manual_check_thread
            try:
                n = auto_refresh.run_refresh_with_status(
                    "manual", _reservation=reservation)
                log.info("手动检查完成,入队 %d 条", n)
            except Exception:
                log.exception("手动检查异常")
            finally:
                # run_refresh_with_status 已先释放业务预约；这里再清 worker 登记，
                # 锁序不会形成 _manual_check_lock ↔ auto_refresh._lock 反转。
                me = threading.current_thread()
                with _manual_check_lock:
                    if _manual_check_thread is me:
                        _manual_check_thread = None

        worker = None
        try:
            worker = threading.Thread(
                target=_run, daemon=True, name="bd-manual-check")
            _manual_check_thread = worker
            worker.start()
        except Exception:
            if _manual_check_thread is worker:
                _manual_check_thread = None
            reservation.release()
            raise
    return jsonify({"ok": True, "started": True, "message": "已在后台开始检查"})


# ---------- A4: 收藏夹浏览 ----------
@app.route("/api/fav-folders")
@api_errors
def fav_folders():
    context = _request_auth_context()
    folders = _account_call(bapi.fetch_fav_folders, context=context)
    pagecache.save("fav_folders", {"ok": True, "folders": folders},
                   context=context)
    return jsonify({"ok": True, "folders": folders})


@app.route("/api/fav-seasons")
@api_errors
def fav_seasons():
    context = _request_auth_context()
    seasons = _account_call(bapi.fetch_collected_seasons, context=context)
    pagecache.save("fav_seasons", {"ok": True, "seasons": seasons},
                   context=context)
    return jsonify({"ok": True, "seasons": seasons})


@app.route("/api/season-content")
@api_errors
def season_content():
    season_id = _integer(request.args.get("season_id"), "season_id",
                         minimum=1, maximum=10 ** 20)
    context = _request_auth_context()
    items = _account_call(bapi.fetch_season_content, season_id, context=context)
    return jsonify({"ok": True, "items": items})


@app.route("/api/fav-content")
@api_errors
def fav_content():
    raw_media_id = request.args.get("media_id")
    if raw_media_id in (None, ""):
        raw_media_id = request.args.get("fid")
    media_id = _integer(raw_media_id, "media_id", minimum=1, maximum=10 ** 20)
    context = _request_auth_context()
    items = _account_call(bapi.fetch_fav_content, media_id, context=context)
    return jsonify({"ok": True, "items": items})


@app.route("/api/followings")
@api_errors
def followings():
    force = _boolean(request.args.get("force"), "force", default=False)
    context = _request_auth_context()
    result = _account_call(bapi.fetch_followings_all, force=force, context=context)
    # 本地自动下载清单与站内关注合并：前端「关注 UP」视图一页看全两种状态。
    # added_at 是「加入自动下载清单」的时间,手动加入且未关注时用它当时间键;
    # last_pub_at 是本地游标里最近一次扫到的投稿时间(未跟踪的 UP 没有)。
    local_ups = []
    last_seen_map = up_list.get_last_seen_all()
    for up in up_list.list_ups():
        mid = int(up["mid"])
        last = last_seen_map.get(str(mid)) or {}
        local_ups.append({
            "mid": mid,
            "uname": up.get("uname", ""),
            "face": (up.get("face") or "").replace("http:", "https:"),
            "quality": up.get("quality", ""),
            "added_at": bapi._int_or_zero(up.get("added_at")),
            "last_pub_at": bapi._int_or_zero(last.get("ts")),
        })
    body = {"ok": True, "items": result["items"], "total": result["total"],
            "local_ups": local_ups, "truncated": bool(result.get("truncated"))}
    pagecache.save("followings", body, context=context)
    return jsonify(body)


@app.route("/api/relation/modify", methods=["POST"])
@api_errors
def relation_modify():
    """关注/取消关注。{mid:int, action:"follow"|"unfollow"}

    写操作直接作用于登录账号；重复关注按成功处理，其余错误透出 B 站 message。
    """
    body = _json_object()
    mid = _integer(body.get("mid"), "mid", minimum=1, maximum=10 ** 20)
    action = _string(body.get("action"), "action", required=True, max_length=16)
    if action not in ("follow", "unfollow"):
        raise ApiError(400, "invalid_field",
                       "action 必须是 follow 或 unfollow", field="action")
    context = _request_auth_context()
    _account_call(bapi.modify_relation, mid, action, context=context)
    return jsonify({"ok": True, "mid": mid, "action": action})


@app.route("/api/watchlater")
@api_errors
def watchlater():
    context = _request_auth_context()
    items = _account_call(bapi.fetch_watchlater, context=context)
    pagecache.save("watchlater", {"ok": True, "items": items}, context=context)
    return jsonify({"ok": True, "items": items})


def _task_map(tasks_snapshot):
    """把深拷贝任务快照整理为稳定 ID Map；忽略无 ID 的损坏记录。"""
    result = {}
    for task in tasks_snapshot:
        task_id = str(task.get("task_id") or "")
        if task_id:
            result[task_id] = task
    return result


def _sse_message(event: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _reserve_sse_slot():
    """取得共享 SSE 槽并返回幂等释放函数。"""
    if not _sse_slots.acquire(blocking=False):
        raise ApiError(
            503, "sse_capacity_exceeded",
            f"实时连接已达 {_MAX_SSE_CONNECTIONS} 条上限，请稍后重试")
    guard = threading.Lock()
    released = False

    def release() -> None:
        nonlocal released
        with guard:
            if released:
                return
            released = True
        _sse_slots.release()

    return release


@app.route("/api/tasks/events")
def task_events():
    """单一任务事件流：首包 snapshot，随后按任务 ID 推 patch。"""
    release_slot = _reserve_sse_slot()

    def gen():
        try:
            revision = 1
            previous = _task_map(downloader.list_tasks())
            last_emit = time.monotonic()
            yield _sse_message("snapshot", {
                "revision": revision,
                "tasks": list(previous.values()),
            })
            while True:
                time.sleep(1.0)
                current = _task_map(downloader.list_tasks())
                upserts = [task for task_id, task in current.items()
                           if previous.get(task_id) != task]
                removed = [task_id for task_id in previous
                           if task_id not in current]
                if upserts or removed:
                    revision += 1
                    yield _sse_message("patch", {
                        "revision": revision,
                        "upserts": upserts,
                        "removed": removed,
                    })
                    previous = current
                    last_emit = time.monotonic()
                elif time.monotonic() - last_emit >= 15.0:
                    yield _sse_message("heartbeat", {"revision": revision})
                    last_emit = time.monotonic()
        except GeneratorExit:
            return
        except Exception:
            log.exception("任务聚合事件流异常")
            yield _sse_message("stream-error", {
                "code": "task_stream_failed",
                "message": "任务事件流中断",
            })
        finally:
            release_slot()

    try:
        response = Response(stream_with_context(gen()), mimetype="text/event-stream")
    except Exception:
        release_slot()
        raise
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    response.call_on_close(release_slot)
    return response


@app.route("/api/progress/<task_id>")
def progress(task_id):
    """兼容一个周期的单任务 SSE；与聚合流共享连接容量。"""
    release_slot = _reserve_sse_slot()

    def gen():
        try:
            while True:
                task = downloader.get_task(task_id)
                if not task:
                    yield "event: end\ndata: {}\n\n"
                    break
                yield f"data: {json.dumps(task, ensure_ascii=False)}\n\n"
                if task["status"] in taskstore.TERMINAL:
                    break
                time.sleep(1)
        finally:
            release_slot()

    try:
        response = Response(stream_with_context(gen()), mimetype="text/event-stream")
    except Exception:
        release_slot()
        raise
    response.call_on_close(release_slot)
    return response


@app.route("/api/tasks")
def tasks():
    return jsonify({"ok": True, "tasks": downloader.list_tasks()})


@app.route("/api/stats")
@api_errors
def stats():
    """统计面板:总下载数、总字节数、本月/今日(自然月)。计算在 taskstore.compute_stats。"""
    return jsonify({"ok": True, **taskstore.compute_stats()})


@app.route("/api/config")
def get_config():
    return jsonify({"ok": True, **config.get_config()})


@app.route("/api/config", methods=["POST"])
@api_errors
def set_config():
    """部分更新配置。"""
    body = _json_object()
    if not body:
        raise ApiError(400, "missing_field", "无更新字段")
    result = config.set_config(body)
    if not result.get("ok"):
        status = 500 if result.get("persist_error") else 400
        return _json_error(result.get("error", "配置更新失败"), status,
                           "config_persist_failed" if status == 500
                           else "invalid_config")
    return jsonify(result)


@app.route("/api/task/<task_id>/reveal", methods=["POST"])
@api_errors
def task_reveal(task_id):
    """在文件管理器中定位任务产物；找不到时打开下载目录。"""
    task_id = _string(task_id, "task_id", required=True, max_length=_MAX_TEXT_LENGTH)
    task_files = downloader.find_task_files(task_id)
    if task_files:
        downloader.reveal_path(task_files[0])
        return jsonify({"ok": True, "found": True})
    downloader.open_dir(config.DOWNLOAD_DIR)
    return jsonify({"ok": True, "found": False})


@app.route("/api/task/<task_id>/resolve-conflict", methods=["POST"])
@api_errors
def task_resolve_conflict(task_id):
    task_id = _string(task_id, "task_id", required=True, max_length=_MAX_TEXT_LENGTH)
    body = _json_object()
    action = _string(body.get("action"), "action", required=True, max_length=32)
    allowed = {"rename_with_index", "cancel", "open_existing"}
    if action not in allowed:
        raise ApiError(400, "invalid_field", "非法处理动作", field="action")
    result = downloader.resolve_rename_conflict(task_id, action)
    if not result.get("ok"):
        return _json_error(result.get("error", "处理失败"), 400,
                           "conflict_action_failed")
    if action == "open_existing" and result.get("path"):
        downloader.reveal_path(result["path"])
    return jsonify(result)


if __name__ == "__main__":
    print(f"\n  浏览器打开: http://localhost:{PORT}\n")
    pagecache.purge_stale()  # 清掉超 30 天没更新的缓存文件
    auto_refresh.start_loop()  # 订阅追更轮询(间隔见 config.AUTO_REFRESH_MINUTES)
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True,
            use_reloader=False)
