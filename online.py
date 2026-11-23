"""在线播放:按登录态解析 B 站 DASH 直链,并代理两条媒体轨。

为什么不用 B 站 iframe:iframe 是第三方环境,浏览器默认拦跨站 Cookie,
B 站认不出登录态,只给 360P/480P;而且 iframe 里的进度条、倍速都由对方
控制,自己拿不到。这里改成「后端用应用已登录的 Cookie 解析直链 + 前端自己
播」,拿到的是同一条登录态下的 1080P,还顺带拿到了 Range(能拖进度)。

DASH 的音视频是分开的两条流,浏览器 <video> 不会自动把两条合起来,所以
前端用 <video> + <audio> 同步播放;后端只负责把两条流按需转发。

解析走两条路:普通 BV 视频优先直连 view + playurl 两个轻接口(合计约
0.5s);yt-dlp 完整提取要 2s+ 且是秒级 CPU 大户,只作番剧/异常时的回落,
行为与旧路径一致。

安全:前端拿不到也不会构造 CDN URL——只拿到一个内存 token,映射关系
只存在于本进程。转发时固定补上 B 站要求的 Referer/UA(实测视频轨缺
Referer 直接 403),因此不存在拿本接口当任意 URL 代理的可能。
"""
import logging
import re
import secrets
import threading
import time
from typing import Optional

import requests

import bilibili_api
import bilibili_auth
import parsing
import quality
from bilibili_auth import BROWSER_HEADERS

log = logging.getLogger("bd.online")

# CDN 直链自带 e= 过期参数(约 2 小时),再留出余量。
# TTL 按「最近一次取用」滑动续期:正在看的视频不会在 30 分钟处断流;
# 绝对上限防止续过 CDN 直链自身的约 2 小时有效期(续过了上游会 403)。
TOKEN_TTL_SECONDS = 30 * 60
TOKEN_MAX_SECONDS = 105 * 60
# 解析结果缓存:同一个视频反复点播放不必每次跑 yt-dlp(要几秒)。
PARSE_TTL_SECONDS = 10 * 60

_UPSTREAM_TIMEOUT = (10, 60)

# 顶层 requests.api.get 的原始实现:测试把顶层 requests.get 换成桩时让路,
# 生产才走连接池会话(与 bilibili_api._get 同一套兼容模式)。
_requests_get = requests.get
# CDN 转发用带连接池的会话:拖进度条会产生大量 Range 请求,复用 TCP/TLS
# 连接,省去每次 0.1s 级的握手(2026-11-17 对 API 会话的同类实测)。
_CDN_SESSION = requests.Session()

_lock = threading.Lock()
_tokens: dict[str, dict] = {}          # token -> {"url", "headers", "expires"}
_parse_cache: dict[str, dict] = {}     # url -> {"payload", "expires"}


def _cleanup(now: float) -> None:
    for store in (_tokens, _parse_cache):
        for key in [k for k, v in store.items() if v.get("expires", 0) <= now]:
            store.pop(key, None)
    # 认证已失效的 token 一并淘汰:token 里锁的是解析时的上游直链,切换
    # 账号/登出后不该再被取用(2026-11-23 审查)。切换期间旧 token 只保留
    # 到 TTL,不做强制全局清空,避免正在播的会话被硬断。
    try:
        current = bilibili_auth.get_auth_context()
    except Exception:
        return
    for token, item in list(_tokens.items()):
        ctx = item.get("context")
        if ctx is not None and ctx != _context_key_of(current):
            _tokens.pop(token, None)


def _context_key_of(context) -> str:
    """认证上下文的可比较键;无账号时给 "anon"。"""
    try:
        key = bilibili_api._context_key(context)
    except Exception:
        key = None
    return repr(key) if key is not None else "anon"


def _register(url: str, headers: dict, context=None) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _tokens[token] = {"url": url, "headers": headers, "created": now,
                          "expires": now + TOKEN_TTL_SECONDS,
                          "context": _context_key_of(context)}
    return token


def lookup(token: str) -> Optional[dict]:
    """token → 上游 URL 与请求头;过期即视为不存在。

    每次取流/取段都会经过这里,顺带把 TTL 续到「现在 + 30 分钟」:
    播放中的视频源源不断发 Range 请求,token 就一直活着——这正是
    「播到一半自动暂停」(30 分钟整 token 死透、下一取段 404)的修复。
    """
    now = time.time()
    with _lock:
        item = _tokens.get(token)
        if not item or item["expires"] <= now:
            _tokens.pop(token, None)
            return None
        item["expires"] = min(now + TOKEN_TTL_SECONDS,
                              item.get("created", now) + TOKEN_MAX_SECONDS)
        return dict(item)


def _pick_video_quality(formats: list[dict]) -> list[tuple[str, dict]]:
    """按高度归档视频轨,同高度只保留码率最高的一条(编码差异交给浏览器)。

    入参是 parsing.parse_video 已经归一化过的选项(带 kind/label/tbr),这里
    不能再过一遍 classify_formats——那是给 yt-dlp 原始 formats 用的,
    对归一化结果二次分类会因为取不到 vcodec/acodec 而全部丢掉。
    """
    by_label: dict[str, dict] = {}
    for option in formats:
        if option.get("kind") != "video":
            continue
        label = option["label"]
        tbr = option.get("tbr") or 0
        if label not in by_label or tbr > (by_label[label].get("tbr") or 0):
            by_label[label] = option
    # 档位从高到低:1080P 在 720P 前面,前端默认取第一个
    order = sorted(by_label.items(),
                   key=lambda kv: kv[1].get("height") or 0, reverse=True)
    return order


def _pick_audio(formats: list[dict]) -> Optional[dict]:
    best = None
    for option in formats:            # 入参同上:已归一化,不再二次分类
        if option.get("kind") != "audio":
            continue
        tbr = option.get("tbr") or 0
        if best is None or tbr > (best.get("tbr") or 0):
            best = option
    return best


def _format_map(info: dict) -> dict:
    return {f.get("format_id"): f for f in info.get("formats", [])
            if isinstance(f, dict)}


# 直连解析只覆盖普通 BV 视频;番剧(/bangumi/)、课程(/cheese/)等 URL
# 不匹配,直接走 yt-dlp 老路。
_BV_RE = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10})")
_PAGE_RE = re.compile(r"[?&]p=(\d+)")


def _cdn_headers() -> dict:
    """CDN 直链要求的请求头(缺 Referer 视频轨 403),与 yt-dlp http_headers 等效。"""
    return {
        "User-Agent": BROWSER_HEADERS.get("User-Agent", ""),
        "Referer": "https://www.bilibili.com/",
    }


def _resolve_via_api(url: str) -> Optional[dict]:
    """快速路径:直连 view + playurl 解析普通 BV 视频,实测合计约 0.5s。

    yt-dlp 完整提取一次要 2.4-2.6s(2026-11-20 实测)且是进程级 CPU 大户,
    是在线起播等待的主因。这里任何失败(番剧/充电/风控/未登录/结构意外)都
    返回 None,由 resolve 回落到 yt-dlp,行为与旧路径完全一致。

    全程使用同一认证快照:两个接口各取一次快照会在切换期间混合身份,
    结果也可能属于已过期的旧账号(2026-11-23 审查)。
    """
    try:
        context, _cookiejar = bilibili_auth.get_auth_snapshot()
    except Exception:
        context = None
    try:
        return _resolve_via_api_with_context(url, context)
    finally:
        if context is not None:
            bilibili_api._ensure_context_current(context)


def _resolve_via_api_with_context(url: str, context) -> Optional[dict]:
    match = _BV_RE.search(url or "")
    if not match:
        return None
    bvid = match.group(1)
    page_match = _PAGE_RE.search(url or "")
    page = int(page_match.group(1)) if page_match else 1
    try:
        view = bilibili_api.fetch_video_view(bvid, context=context)
        if not view:
            return None
        pages = [p for p in (view.get("pages") or []) if isinstance(p, dict)]
        cid = 0
        part = {}
        if pages and 1 <= page <= len(pages):
            part = pages[page - 1]
            cid = part.get("cid") or 0
        cid = cid or view.get("cid") or (pages[0].get("cid") if pages else 0)
        if not cid:
            return None

        playurl = bilibili_api.fetch_playurl(bvid, cid, context=context)
        dash = (playurl or {}).get("dash") or {}
        video_entries = [v for v in (dash.get("video") or [])
                         if isinstance(v, dict) and (v.get("baseUrl") or v.get("base_url"))]
        audio_entries = [a for a in (dash.get("audio") or [])
                         if isinstance(a, dict) and (a.get("baseUrl") or a.get("base_url"))]
        if not video_entries:
            return None

        headers = _cdn_headers()
        # 同标签(同高度)只留码率最高的一条:与 yt-dlp 路径的归一化行为一致。
        # 杜比/FLAC 是 dash.dolby/dash.flac 里的独立轨,这里不取——<audio> 对
        # 它们的解码支持不稳,标准 AAC 轨才是旧路径的实际口径。
        by_label: dict[str, tuple[int, dict]] = {}
        for entry in video_entries:
            label = quality._format_label({"quality": entry.get("id")},
                                          entry.get("height"))
            bandwidth = entry.get("bandwidth") or 0
            current = by_label.get(label)
            if current is None or bandwidth > current[0]:
                by_label[label] = (bandwidth, entry)

        qualities = []
        for label, (_bandwidth, entry) in by_label.items():
            qualities.append({
                "label": label,
                "height": entry.get("height") or 0,
                "token": _register(entry.get("baseUrl") or entry.get("base_url") or "",
                                   headers, context=context),
            })
        qualities.sort(key=lambda q: (-(q["height"] or 0),
                                      0 if "P60" in q["label"] else 1))

        audio_token = ""
        best_audio = None
        for entry in audio_entries:
            if best_audio is None or (entry.get("bandwidth") or 0) > (best_audio.get("bandwidth") or 0):
                best_audio = entry
        if best_audio:
            audio_token = _register(best_audio.get("baseUrl") or best_audio.get("base_url") or "",
                                    headers, context=context)

        return {
            "ok": True,
            "duration": part.get("duration") or view.get("duration") or 0,
            "title": view.get("title") or "",
            "qualities": qualities,
            "audio": audio_token,
        }
    except Exception:
        log.warning("在线播放直连解析异常,回落 yt-dlp", exc_info=True)
        return None


def resolve(url: str) -> dict:
    """解析出各清晰度的视频轨与音频轨 token。结果按「URL + 认证上下文」缓存。

    先走直连 API 快速路径(约 0.5s),失败回落 yt-dlp(2.5s+,行为不变)。
    缓存不绑定认证时,A 账号解析后切到 B,B 在 TTL 内会直接拿到 A 的档位与
    token(2026-11-23 审查)。
    """
    try:
        context, _cookiejar = bilibili_auth.get_auth_snapshot()
    except bilibili_auth.AuthContextChangedError:
        context = None
    ctx_key = _context_key_of(context)
    now = time.time()
    with _lock:
        cached = _parse_cache.get((ctx_key, url))
        if cached and cached["expires"] > now:
            return cached["payload"]

    payload = _resolve_via_api(url)
    if payload is None:
        payload = _resolve_via_ytdlp(url, context=context)

    with _lock:
        _cleanup(now)
        _parse_cache[(ctx_key, url)] = {"payload": payload, "expires": now + PARSE_TTL_SECONDS}
    return payload


def _resolve_via_ytdlp(url: str, context=None) -> dict:
    """yt-dlp 路径(旧行为):番剧、直连 API 失败时的回落。"""
    # 一次提取同时拿归一化档位与直链 formats:原先这里连跑两遍 yt-dlp,
    # 每个预热请求双倍秒级 CPU,会拖慢同进程的其它接口(搜索实测被堵 23s)。
    summary, info = parsing.parse_video_with_info(url)
    formats = summary.get("formats", [])
    fmts = _format_map(info)

    audio_option = _pick_audio(formats)
    audio_token = ""
    if audio_option:
        audio_format = fmts.get(audio_option["format_id"]) or {}
        if audio_format.get("url"):
            audio_token = _register(audio_format["url"],
                                    audio_format.get("http_headers") or {},
                                    context=context)

    qualities = []
    for label, option in _pick_video_quality(formats):
        raw = fmts.get(option["format_id"]) or {}
        if not raw.get("url"):
            continue
        qualities.append({
            "label": label,
            "height": raw.get("height") or 0,
            "token": _register(raw["url"], raw.get("http_headers") or {},
                               context=context),
        })

    payload = {
        "ok": True,
        "duration": info.get("duration") or summary.get("duration") or 0,
        "title": summary.get("title") or "",
        "qualities": qualities,
        "audio": audio_token,
    }
    return payload


def open_upstream(token: str, range_header: Optional[str]):
    """把一条轨的请求转发给 CDN。返回 (response, error)。"""
    item = lookup(token)
    if not item:
        return None, ("播放地址已过期,请重新打开", "token_expired")
    headers = {k: v for k, v in (item.get("headers") or {}).items()
               if k.lower() in ("user-agent", "referer", "accept", "accept-language",
                                "origin", "cookie")}
    if not any(k.lower() == "referer" for k in headers):
        headers["Referer"] = "https://www.bilibili.com"
    if range_header:
        headers["Range"] = range_header
    getter = requests.get if requests.get is not _requests_get else _CDN_SESSION.get
    try:
        upstream = getter(item["url"], headers=headers, stream=True,
                          timeout=_UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("在线播放上游请求失败: %s", exc)
        return None, ("上游请求失败", "upstream_error")
    if upstream.status_code not in (200, 206):
        upstream.close()
        log.warning("在线播放上游返回 %s", upstream.status_code)
        return None, ("上游拒绝了这个播放地址", "upstream_rejected")
    return upstream, None
