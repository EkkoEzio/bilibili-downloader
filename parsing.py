"""yt-dlp 解析:单视频(含多 P)、收藏夹/合集/UP 主页等列表,及清晰度选项归一化。
只读不下载;下载执行在 downloader.py。"""
import logging
from typing import Optional

import yt_dlp

from bilibili_auth import get_ytdlp_cookie_arg
from quality import classify_formats

import config

log = logging.getLogger("bd.parsing")


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
    options = classify_formats(info.get("formats", []))
    video_opts = [o for o in options if o["kind"] == "video"]
    av_opts = [o for o in options if o["kind"] == "av"]
    best = (video_opts[0]["label"] if video_opts
            else (av_opts[0]["label"] if av_opts else ""))
    # 给每个清晰度选项补预估大小(MB)
    fmt_map = {f.get("format_id"): f for f in info.get("formats", [])}
    for o in options:
        fid = o["format_id"]
        f = fmt_map.get(fid, {})
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if not size and f.get("tbr"):
            # 用码率×时长估算(字节)
            dur = info.get("duration") or 0
            size = int(f["tbr"] * 1024 / 8 * dur)
        o["size_mb"] = round(size / 1024 / 1024, 1) if size else 0
    return {
        "title": info.get("title", ""),
        "uploader": info.get("uploader", ""),
        # yt-dlp 对 B 站把 UP 的 mid 放在 uploader_id,下载记录页 UP 名跳转用
        "uploader_mid": info.get("uploader_id") or "",
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail", ""),
        "formats": options,
        "best_label": best,
    }


def parse_video(url: str) -> dict:
    """解析视频。

    支持分 P:若是多 P 视频(不带 ?p=),返回 parts 列表供前端选择,
    并默认用第一 P 的清晰度信息。单 P 则 parts 为空。
    """
    opts = _ydl_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"解析失败:{e}")

    # 多 P:yt-dlp 返回 _type=playlist,entries 为各分 P
    if info.get("_type") == "playlist" and info.get("entries"):
        parts = []
        for idx, e in enumerate(info["entries"], 1):
            # 真实标题优先;若无,fallback 用 playlist 标题 + 第N集,避免同名
            et = (e.get("title") or "").strip()
            if not et or et == info.get("title", ""):
                et = f"{info.get('title','')} 第{idx}集"
            parts.append({
                "page": idx,
                "title": et,
                "duration": e.get("duration", 0),
            })
        summary = {
            "title": info.get("title", ""),
            "uploader": info.get("uploader", ""),
            "uploader_mid": info.get("uploader_id") or "",
            "duration": parts[0]["duration"] if parts else 0,
            "thumbnail": info.get("thumbnail", "") or (info["entries"][0].get("thumbnail", "") if info["entries"] else ""),
            "formats": [],      # 多 P 时清晰度要选了 P 再查
            "best_label": "",
        }
        return {**summary, "is_multi_part": True, "parts": parts}

    summary = _summarize(info)
    return {**summary, "is_multi_part": False, "parts": []}


def parse_part(url: str, page: int) -> dict:
    """多 P 视频:解析指定分 P 的清晰度。"""
    part_url = url.split("?")[0] + f"?p={page}"
    opts = _ydl_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(part_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"解析分 P 失败:{e}")
    summary = _summarize(info)
    summary["title"] = info.get("title", "")
    return summary


def resolve_playlist(url: str) -> dict:
    """解析收藏夹/合集/UP主页等多视频链接,返回待下载项列表。

    用 yt-dlp flat 模式快速列出所有视频(不逐个拉清晰度),统一返回
    {kind:'list', title, items:[{title,url,duration}]}。
    单视频 URL 也能进,返回单项列表。
    """
    opts = _ydl_opts({"skip_download": True, "extract_flat": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"解析失败:{e}")

    items = []
    title = info.get("title", "")
    if info.get("_type") == "playlist" and info.get("entries"):
        for idx, e in enumerate(info["entries"], 1):
            # flat 模式下 entry 的 url 才是可下载的完整链接
            item_url = e.get("url") or e.get("id") or ""
            # bilibili 的 entry.id 可能是裸 BV,补全
            if item_url and not item_url.startswith("http"):
                item_url = f"https://www.bilibili.com/video/{item_url}"
            # flat 模式元数据稀疏:标题/时长大多拿不到,用兜底
            item_title = e.get("title") or ""
            if not item_title:
                # 分P链接 ?p=N 用 P 序号兜底
                if "?p=" in item_url:
                    pnum = item_url.split("?p=")[-1].split("&")[0]
                    item_title = f"第 {pnum} 集"
                else:
                    item_title = f"第 {idx} 项"
            items.append({
                "title": item_title,
                "url": item_url,
                "duration": e.get("duration") or 0,
            })
    else:
        # 单视频
        items.append({
            "title": info.get("title", "(无标题)"),
            "url": url,
            "duration": info.get("duration") or 0,
        })
    return {"kind": "list", "title": title, "items": items}
