"""yt-dlp 封装:解析、下载、进度、取消。"""
import json
import threading
import uuid
from typing import Optional

import yt_dlp

from quality import classify_formats, best_video_audio_choice
from bilibili_auth import get_ytdlp_cookie_arg
from config import DOWNLOAD_DIR, TASKS_FILE

# 任务状态表:task_id -> {status, progress, speed, eta, title, filepath, error, cancel}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
# 下载线程句柄:task_id -> threading.Thread
_threads: dict[str, threading.Thread] = {}


def _persist() -> None:
    """把当前所有任务快照写盘。去掉 cancel 标志,保留其余展示字段。"""
    with _tasks_lock:
        snapshot = [{k: v for k, v in t.items() if k != "cancel"} for t in _tasks.values()]
    try:
        TASKS_FILE.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_persisted() -> None:
    """启动时加载历史任务(进行中的统一标记为中断,避免僵尸态)。"""
    if not TASKS_FILE.exists():
        return
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    with _tasks_lock:
        for t in data:
            # 上次中断的活跃任务标记为已中断
            if t.get("status") in ("queued", "downloading", "merging", "cancelling"):
                t["status"] = "interrupted"
                t["error"] = "服务重启,任务中断"
            t["cancel"] = False
            _tasks[t["task_id"]] = t


# 模块加载时恢复历史
_load_persisted()


def _ydl_opts(extra: Optional[dict] = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    opts.update(get_ytdlp_cookie_arg())
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
            parts.append({
                "page": idx,
                "title": e.get("title", f"P{idx}"),
                "duration": e.get("duration", 0),
            })
        summary = {
            "title": info.get("title", ""),
            "uploader": info.get("uploader", ""),
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


def _sanitize(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip().rstrip(".") or "video"


def _progress_hook(task_id: str):
    state_changed = False

    def hook(d):
        nonlocal state_changed
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
                state_changed = False  # downloading 是高频更新,节流:每 5% 存一次
                if int(t["progress"]) % 5 == 0:
                    state_changed = True
            elif d["status"] == "finished":
                t["progress"] = 100
                t["status"] = "merging"  # yt-dlp 合并阶段
                state_changed = True
            elif d["status"] == "error":
                t["status"] = "failed"
                t["error"] = "下载出错"
                state_changed = True
        if state_changed:
            _persist()
    return hook


def _is_cancelled(task_id: str) -> bool:
    with _tasks_lock:
        t = _tasks.get(task_id)
        return bool(t and t.get("cancel"))


def _run_download(task_id: str, url: str, label: str, formats: list[dict], page: int) -> None:
    fmt_str = best_video_audio_choice(formats, label)
    # 分 P:文件名带 P 序号,避免覆盖
    tmpl = "%(title).150B - P%(playlist_index)s.%(ext)s" if page > 1 else "%(title).200B.%(ext)s"
    outtmpl = str(DOWNLOAD_DIR / tmpl)
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
                t["status"] = "cancelled" if t.get("cancel") else "done"
        _persist()
    except Exception as e:
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                if t.get("cancel"):
                    t["status"] = "cancelled"
                else:
                    t["status"] = "failed"
                    t["error"] = str(e)
        _persist()


def start_download(url: str, label: str, page: int = 1) -> str:
    """启动异步下载。先解析拿到 formats,再后台下载。返回 task_id。

    page>1 表示多 P 视频的指定分集。
    """
    if page > 1:
        summary = parse_part(url, page)
    else:
        summary = parse_video(url)
        # parse_video 对多 P 不返回 formats,这里兜底拉第一 P
        if not summary.get("formats"):
            summary = parse_part(url, 1)
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0, "speed": 0, "eta": 0,
            "title": summary["title"], "url": url, "label": label,
            "page": page, "error": "", "cancel": False,
        }
    th = threading.Thread(target=_run_download,
                          args=(task_id, url, label, summary["formats"], page),
                          daemon=True)
    with _tasks_lock:
        _threads[task_id] = th
    th.start()
    _persist()
    return task_id


def cancel_download(task_id: str) -> bool:
    """标记任务取消。yt-dlp 线程会在下个分片边界停止(无法强杀,靠标志位)。"""
    with _tasks_lock:
        t = _tasks.get(task_id)
        if not t or t["status"] in ("done", "failed", "cancelled"):
            return False
        t["cancel"] = True
        t["status"] = "cancelling"
    _persist()
    return True


def get_task(task_id: str) -> Optional[dict]:
    with _tasks_lock:
        return _tasks.get(task_id)


def list_tasks() -> list[dict]:
    with _tasks_lock:
        return list(_tasks.values())


def find_active_by_url(url: str, label: str) -> Optional[str]:
    """查找同 URL+清晰度 且仍在进行中的任务,用于重复下载拦截。返回 task_id 或 None。"""
    with _tasks_lock:
        for tid, t in _tasks.items():
            if (t.get("url") == url and t.get("label") == label
                    and t["status"] in ("queued", "downloading", "merging", "cancelling")):
                return tid
    return None
