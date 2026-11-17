"""在线播放:按登录态解析 B 站 DASH 直链,并代理两条媒体轨。

为什么不用 B 站 iframe:iframe 是第三方环境,浏览器默认拦跨站 Cookie,
B 站认不出登录态,只给 360P/480P;而且 iframe 里的进度条、倍速都由对方
控制,自己拿不到。这里改成「后端用应用已登录的 Cookie 解析直链 + 前端自己
播」,拿到的是同一条登录态下的 1080P,还顺带拿到了 Range(能拖进度)。

DASH 的音视频是分开的两条流,浏览器 <video> 不会自动把两条合起来,所以
前端用 <video> + <audio> 同步播放;后端只负责把两条流按需转发。

安全:前端拿不到也不会构造 CDN URL——只拿到一个内存 token,映射关系
只存在于本进程。转发时固定补上 B 站要求的 Referer/UA(实测视频轨缺
Referer 直接 403),因此不存在拿本接口当任意 URL 代理的可能。
"""
import logging
import secrets
import threading
import time
from typing import Optional

import requests

import parsing
from quality import classify_formats

log = logging.getLogger("bd.online")

# CDN 直链自带 e= 过期参数(约 2 小时),再留出余量。
TOKEN_TTL_SECONDS = 30 * 60
# 解析结果缓存:同一个视频反复点播放不必每次跑 yt-dlp(要几秒)。
PARSE_TTL_SECONDS = 10 * 60

_CHUNK = 64 * 1024
_UPSTREAM_TIMEOUT = (10, 60)

_lock = threading.Lock()
_tokens: dict[str, dict] = {}          # token -> {"url", "headers", "expires"}
_parse_cache: dict[str, dict] = {}     # url -> {"payload", "expires"}


def _cleanup(now: float) -> None:
    for store in (_tokens, _parse_cache):
        for key in [k for k, v in store.items() if v.get("expires", 0) <= now]:
            store.pop(key, None)


def _register(url: str, headers: dict) -> str:
    token = secrets.token_urlsafe(24)
    with _lock:
        _tokens[token] = {"url": url, "headers": headers,
                          "expires": time.time() + TOKEN_TTL_SECONDS}
    return token


def lookup(token: str) -> Optional[dict]:
    """token → 上游 URL 与请求头;过期即视为不存在。"""
    now = time.time()
    with _lock:
        item = _tokens.get(token)
        if not item or item["expires"] <= now:
            _tokens.pop(token, None)
            return None
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


def resolve(url: str) -> dict:
    """解析出各清晰度的视频轨与音频轨 token。结果按 URL 缓存一小段时间。"""
    now = time.time()
    with _lock:
        cached = _parse_cache.get(url)
        if cached and cached["expires"] > now:
            return cached["payload"]

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
                                    audio_format.get("http_headers") or {})

    qualities = []
    for label, option in _pick_video_quality(formats):
        raw = fmts.get(option["format_id"]) or {}
        if not raw.get("url"):
            continue
        qualities.append({
            "label": label,
            "height": raw.get("height") or 0,
            "token": _register(raw["url"], raw.get("http_headers") or {}),
        })

    payload = {
        "ok": True,
        "duration": info.get("duration") or summary.get("duration") or 0,
        "title": summary.get("title") or "",
        "qualities": qualities,
        "audio": audio_token,
    }
    with _lock:
        _cleanup(now)
        _parse_cache[url] = {"payload": payload, "expires": now + PARSE_TTL_SECONDS}
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
    try:
        upstream = requests.get(item["url"], headers=headers, stream=True,
                                timeout=_UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("在线播放上游请求失败: %s", exc)
        return None, ("上游请求失败", "upstream_error")
    if upstream.status_code not in (200, 206):
        upstream.close()
        log.warning("在线播放上游返回 %s", upstream.status_code)
        return None, ("上游拒绝了这个播放地址", "upstream_rejected")
    return upstream, None
