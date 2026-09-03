"""yt-dlp 解析:单视频(含多 P)、收藏夹/合集/UP 主页等列表,及清晰度选项归一化。
只读不下载;下载执行在 downloader.py。"""
import logging
from typing import Optional

import yt_dlp

from bilibili_auth import get_ytdlp_cookie_arg
from quality import classify_formats
from request_governor import governor

import config

log = logging.getLogger("bd.parsing")

# 批量入口与 yt-dlp 提取阶段共享的硬边界。多取 1 项用于让路由区分“正好 100”
# 与“实际还有更多”，避免先完整展开超大播放列表再拒绝。
MAX_PLAYLIST_ITEMS = 100


def _ydl_opts(extra: Optional[dict] = None) -> dict:
    """解析/下载共用的基础 opts:静默 + cookie + 限速(字节/秒,None 表示不限)。

    名字保持 _ydl_opts:downloader.py 再导出后,测试在 downloader 命名空间
    打补丁(monkeypatch.setattr(downloader, "_ydl_opts", ...))仍生效。"""
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    opts.update(get_ytdlp_cookie_arg())
    rl = config.get_ratelimit_bytes()
    if rl is not None:
        opts["ratelimit"] = rl
    if extra:
        opts.update(extra)
    return opts


def _summarize(info: dict) -> dict:
    """从 yt-dlp 单视频 info 提取展示用字段 + 归一化清晰度 + 预估大小。"""
    raw_formats = info.get("formats", [])
    formats = raw_formats if isinstance(raw_formats, list) else []
    options = classify_formats(formats)
    video_opts = [o for o in options if o["kind"] == "video"]
    av_opts = [o for o in options if o["kind"] == "av"]
    best = (video_opts[0]["label"] if video_opts
            else (av_opts[0]["label"] if av_opts else ""))
    fmt_map = {f.get("format_id"): f for f in formats if isinstance(f, dict)}
    for o in options:
        fid = o["format_id"]
        f = fmt_map.get(fid, {})
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if not size and f.get("tbr"):
            dur = info.get("duration") or 0
            size = int(f["tbr"] * 1024 / 8 * dur)
        o["size_mb"] = round(size / 1024 / 1024, 1) if size else 0
    return {
        "title": info.get("title", ""),
        "uploader": info.get("uploader", ""),
        "uploader_mid": info.get("uploader_id") or "",
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail", ""),
        "formats": options,
        "best_label": best,
    }


def _extract_info(url: str, opts: dict, error_prefix: str) -> dict:
    """所有 yt-dlp 解析共用媒体启动闸门，限制解析/下载的突发启动。"""
    governor.wait("media", endpoint=url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"{error_prefix}:{e}") from e
    if not isinstance(info, dict):
        raise RuntimeError(f"{error_prefix}:返回数据格式无效")
    return info


def parse_video(url: str) -> dict:
    """解析单视频或多 P；坏分 P 被隔离，保留有效条目的原始页码。"""
    opts = _ydl_opts({"skip_download": True})
    info = _extract_info(url, opts, "解析失败")

    if info.get("_type") == "playlist":
        raw_entries = info.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        playlist_title = str(info.get("title") or "")
        parts = []
        fallback_thumbnail = ""
        for idx, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                continue
            if not fallback_thumbnail and entry.get("thumbnail"):
                fallback_thumbnail = str(entry.get("thumbnail") or "")
            entry_title = str(entry.get("title") or "").strip()
            if not entry_title or entry_title == playlist_title:
                entry_title = f"{playlist_title} 第{idx}集"
            parts.append({
                "page": idx,
                "title": entry_title,
                "duration": entry.get("duration") or 0,
            })
        if not parts:
            raise RuntimeError("未找到可用分 P 条目")
        summary = {
            "title": playlist_title,
            "uploader": info.get("uploader", ""),
            "uploader_mid": info.get("uploader_id") or "",
            "duration": parts[0]["duration"],
            "thumbnail": str(info.get("thumbnail") or "") or fallback_thumbnail,
            "formats": [],
            "best_label": "",
        }
        return {**summary, "is_multi_part": True, "parts": parts}

    summary = _summarize(info)
    return {**summary, "is_multi_part": False, "parts": []}


def parse_part(url: str, page: int) -> dict:
    """多 P 视频:解析指定分 P 的清晰度。"""
    part_url = url.split("?")[0] + f"?p={page}"
    opts = _ydl_opts({"skip_download": True})
    info = _extract_info(part_url, opts, "解析分 P 失败")
    summary = _summarize(info)
    summary["title"] = info.get("title", "")
    return summary


def resolve_playlist(url: str) -> dict:
    """解析收藏夹/合集/UP主页；坏 entry 跳过，全部无效时明确失败。"""
    opts = _ydl_opts({
        "skip_download": True,
        "extract_flat": True,
        "playlistend": MAX_PLAYLIST_ITEMS + 1,
    })
    info = _extract_info(url, opts, "解析失败")

    items = []
    title = str(info.get("title") or "")
    if info.get("_type") == "playlist":
        raw_entries = info.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        for idx, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                continue
            raw_url = entry.get("url") or entry.get("id")
            item_url = str(raw_url or "").strip()
            if not item_url:
                continue
            if not item_url.startswith(("http://", "https://")):
                item_url = f"https://www.bilibili.com/video/{item_url}"
            item_title = str(entry.get("title") or "").strip()
            if not item_title:
                if "?p=" in item_url:
                    pnum = item_url.split("?p=", 1)[1].split("&", 1)[0]
                    item_title = f"第 {pnum} 集"
                else:
                    item_title = f"第 {idx} 项"
            items.append({
                "title": item_title,
                "url": item_url,
                "duration": entry.get("duration") or 0,
            })
        if not items:
            raise RuntimeError("未找到可用播放列表条目")
    else:
        items.append({
            "title": str(info.get("title") or "(无标题)"),
            "url": url,
            "duration": info.get("duration") or 0,
        })
    return {"kind": "list", "title": title, "items": items}
