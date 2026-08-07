"""yt-dlp 封装:解析、下载、进度。"""
import threading
import uuid
from typing import Optional

import yt_dlp

from quality import classify_formats, best_video_audio_choice
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


def _sanitize(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip().rstrip(".") or "video"


def _progress_hook(task_id: str):
    def hook(d):
        with _tasks_lock:
            t = _tasks.get(task_id)
            if not t:
                return
            if d["status"] == "downloading":
                # yt-dlp 这些字段可能为 None,统一兜底为 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                speed = d.get("speed") or 0
                t["progress"] = round(downloaded / total * 100, 1) if total else 0
                t["speed"] = round(speed / 1024, 1)  # KB/s
                t["eta"] = d.get("eta") or 0
            elif d["status"] == "finished":
                t["progress"] = 100
                t["status"] = "merging"  # yt-dlp 合并阶段
            elif d["status"] == "error":
                t["status"] = "failed"
                t["error"] = "下载出错"
    return hook


def _run_download(task_id: str, url: str, label: str, formats: list[dict]) -> None:
    fmt_str = best_video_audio_choice(formats, label)
    outtmpl = str(DOWNLOAD_DIR / "%(title).200B.%(ext)s")
    opts = _ydl_opts({
        "format": fmt_str,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook(task_id)],
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t and t["status"] != "failed":
                t["status"] = "done"
    except Exception as e:
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t["status"] = "failed"
                t["error"] = str(e)


def start_download(url: str, label: str) -> str:
    """启动异步下载。先解析拿到 formats,再后台下载。返回 task_id。"""
    info = parse_video(url)
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0, "speed": 0, "eta": 0,
            "title": info["title"], "url": url, "label": label, "error": "",
        }
    threading.Thread(target=_run_download, args=(task_id, url, label, info["formats"]),
                     daemon=True).start()
    return task_id


def get_task(task_id: str) -> Optional[dict]:
    with _tasks_lock:
        return _tasks.get(task_id)


def list_tasks() -> list[dict]:
    with _tasks_lock:
        return list(_tasks.values())
