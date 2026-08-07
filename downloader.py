"""yt-dlp 封装:解析、下载、进度。"""
import threading
import uuid
from typing import Optional

import yt_dlp

from quality import classify_formats
from bilibili_auth import get_ytdlp_cookie_arg
from config import DOWNLOAD_DIR

# 任务状态表:task_id -> {status, progress, speed, eta, title, filepath, error}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


def _ydl_opts(extra: Optional[dict] = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    opts.update(get_ytdlp_cookie_arg())
    if extra:
        opts.update(extra)
    return opts


def parse_video(url: str) -> dict:
    """解析视频,返回标题等信息与归一化清晰度选项。"""
    opts = _ydl_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"解析失败:{e}")

    options = classify_formats(info.get("formats", []))
    # 给前端一个推荐画质:最高视频轨的 label
    video_opts = [o for o in options if o["kind"] == "video"]
    av_opts = [o for o in options if o["kind"] == "av"]
    best = (video_opts[0]["label"] if video_opts
            else (av_opts[0]["label"] if av_opts else ""))
    return {
        "title": info.get("title", ""),
        "uploader": info.get("uploader", ""),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail", ""),
        "formats": options,
        "best_label": best,
    }
