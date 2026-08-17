"""yt-dlp 封装:解析、下载、进度、取消。"""
import json
import logging
import subprocess
import sys
import threading
import uuid
from typing import Optional

import yt_dlp

import extras
from quality import classify_formats, best_video_audio_choice
from bilibili_auth import get_ytdlp_cookie_arg
from config import TASKS_FILE
import config  # 不用 `from config import DOWNLOAD_DIR`,要实时读 config.DOWNLOAD_DIR

log = logging.getLogger("bd.downloader")

# 已下载判定复用的 bvid 提取(extras 也用它定位弹幕)
bvid_from_url = extras.bvid_from_url

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
    # B2: 限速(字节/秒),config 关闭时 None 表示不限
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


def _sanitize(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip().rstrip(".") or "video"


def _base_opts(fmt_str: str, page: int, task_id: str) -> dict:
    """构造下载基础 opts:format、输出模板(可选按 UP 归档)、附件开关、进度钩子。

    仅音频/单体格式不设 merge_output_format;组合轨(DASH 视频+音频)或
    best 需要合并时才要求输出 mp4。
    """
    sub = "%(uploader)s/" if config.SORT_BY_UP else ""
    tmpl = "%(title).150B - P%(playlist_index)s.%(ext)s" if page > 1 else "%(title).200B.%(ext)s"
    opts = {
        "format": fmt_str,
        "outtmpl": str(config.DOWNLOAD_DIR / (sub + tmpl)),
        "progress_hooks": [_progress_hook(task_id)],
    }
    if "+" in fmt_str or fmt_str == "best":
        opts["merge_output_format"] = "mp4"
    if config.SAVE_COVER:
        opts["writethumbnail"] = True
    if config.SAVE_SUBTITLE:
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = ["zh-Hans", "zh-CN"]
        opts["subtitlesformat"] = "srt/best"
    return opts


def _notify_done(title: str) -> None:
    """下载完成发 macOS 通知(可在设置里关)。"""
    if not config.NOTIFY or sys.platform != "darwin":
        return
    safe = (title or "视频").replace('"', "'")[:60]
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "《{safe}》下载完成" with title "bilibili download"'],
            check=False)
    except Exception:
        pass


def is_downloaded(url: str) -> bool:
    """该视频(BV 号)是否已有 done 任务——列表「已下载」标记与批量跳过用。"""
    bv = bvid_from_url(url)
    if not bv:
        return False
    with _tasks_lock:
        for t in _tasks.values():
            if t.get("status") == "done" and bvid_from_url(t.get("url") or "") == bv:
                return True
    return False


def _progress_hook(task_id: str):
    state = {"bucket": None}  # 上次落盘所在的 5% 进度桶

    def hook(d):
        persist = False
        with _tasks_lock:
            t = _tasks.get(task_id)
            # 取消或任务记录已删:抛异常让 yt-dlp 立即中止,
            # 否则下载会完整跑完才被标记(浪费带宽/磁盘)。
            if not t or t.get("cancel"):
                raise yt_dlp.utils.DownloadCancelled("任务已取消")
            if d["status"] == "downloading":
                # 进入下载态(yt-dlp hook 第一次回调就把 queued→downloading)
                t["status"] = "downloading"
                # yt-dlp 这些字段可能为 None,统一兜底为 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                speed = d.get("speed") or 0
                t["progress"] = round(downloaded / total * 100, 1) if total else 0
                t["speed"] = round(speed / 1024, 1)  # KB/s
                t["eta"] = d.get("eta") or 0
                # downloading 是高频更新,节流:每进入一个新的 5% 桶才落一次盘
                bucket = int(t["progress"]) // 5
                if bucket != state["bucket"]:
                    state["bucket"] = bucket
                    persist = True
            elif d["status"] == "finished":
                t["progress"] = 100
                t["status"] = "merging"  # yt-dlp 合并阶段
                persist = True
            elif d["status"] == "error":
                t["status"] = "failed"
                t["error"] = "下载出错"
                persist = True
        if persist:
            _persist()
    return hook


def _is_cancelled(task_id: str) -> bool:
    with _tasks_lock:
        t = _tasks.get(task_id)
        return bool(t and t.get("cancel"))


def _finalize_cancelled(task_id: str) -> None:
    """把未终结的任务落为已取消(取消发生在下载回调之外时的兜底)。"""
    with _tasks_lock:
        t = _tasks.get(task_id)
        if t and t["status"] not in ("done", "failed", "cancelled"):
            t["status"] = "cancelled"
    _persist()


def _run_download(task_id: str, url: str, label: str, formats: list[dict], page: int) -> None:
    # formats 为空时(批量任务未预解析),自己解析补上
    if not formats:
        try:
            summary = parse_part(url, page) if page > 1 else parse_video(url)
            if not summary.get("formats"):
                summary = parse_part(url, 1)
            formats = summary["formats"]
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    if not t.get("title"):
                        t["title"] = summary["title"]
                    # 封面/UP/时长补进任务,供「下载记录」页像观看历史一样渲染卡片
                    if not t.get("thumbnail"):
                        t["thumbnail"] = summary.get("thumbnail", "")
                    if not t.get("uploader"):
                        t["uploader"] = summary.get("uploader", "")
                    if not t.get("duration"):
                        t["duration"] = summary.get("duration") or 0
        except Exception as e:
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["status"] = "failed"
                    t["error"] = str(e)
            _persist()
            return

    fmt_str = best_video_audio_choice(formats, label)
    base_opts = _base_opts(fmt_str, page, task_id)

    max_retries = 2  # 失败后最多重试 2 次,共尝试 3 次
    last_err = None
    for attempt in range(max_retries + 1):
        # 用户主动取消 → 立刻退出,不算失败也不重试
        # (注意:_persist/_finalize_cancelled 会抢 _tasks_lock,不能在锁内调用)
        with _tasks_lock:
            t = _tasks.get(task_id)
            cancelled = bool(t and t.get("cancel"))
        if not t:
            return
        if cancelled:
            _finalize_cancelled(task_id)
            return
        # 第二次起把进度回退一点,便于 UI 显示「正在重试」
        if attempt > 0:
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["status"] = "retrying"
                    t["error"] = f"第 {attempt} 次重试..."
                    t["progress"] = 0
            _persist()
            import time as _t; _t.sleep(2 + attempt * 2)  # 递增退避
            # 退避后再次检查取消(用户在 sleep 期间点了取消)
            with _tasks_lock:
                t = _tasks.get(task_id)
                cancelled = bool(t and t.get("cancel"))
            if not t:
                return
            if cancelled:
                _finalize_cancelled(task_id)
                return

        # 每次重试都重建 YoutubeDL 实例(避免内部状态污染)
        opts = _ydl_opts(base_opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            # 成功: 统计实际下载文件大小,用于 C3 统计面板
            import time as _t
            files = []
            done_ok = False
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t and t["status"] != "failed":
                    t["status"] = "cancelled" if t.get("cancel") else "done"
                    t["error"] = ""
                    t["finished_at"] = int(_t.time())
                    done_ok = t["status"] == "done"
                    try:
                        files = _task_files(t)
                        t["downloaded_bytes"] = sum(f.stat().st_size for f in files if f.is_file())
                    except Exception:
                        t["downloaded_bytes"] = 0
            _persist()
            # 弹幕落盘与系统通知是慢操作,放锁外;尽力而为,不影响任务状态
            if done_ok:
                if config.SAVE_DANMAKU and files:
                    try:
                        extras.save_danmaku_for(url, files[0], page)
                    except Exception:
                        log.exception("弹幕保存失败 %s", url)
                if t:
                    if config.NOTIFY:
                        _notify_done(t["title"])
                    log.info("任务完成 %s 《%s》", task_id, t.get("title", ""))
            return
        except Exception as e:
            last_err = e
            # 取消标志置位 → 不再重试(锁内只读标志,收尾放锁外,避免重入死锁)
            with _tasks_lock:
                t = _tasks.get(task_id)
                cancelled = bool(t and t.get("cancel"))
            if cancelled:
                _finalize_cancelled(task_id)
                return

    # 全部尝试失败
    with _tasks_lock:
        t = _tasks.get(task_id)
        if t and not t.get("cancel"):
            t["status"] = "failed"
            t["error"] = str(last_err)
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
    import time as _t
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0, "speed": 0, "eta": 0,
            "title": summary["title"], "url": url, "label": label,
            "thumbnail": summary.get("thumbnail", ""),
            "uploader": summary.get("uploader", ""),
            "duration": summary.get("duration") or 0,
            "created_at": int(_t.time()),
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


def start_batch(items: list[dict], label: str, max_concurrent: int = None) -> list[str]:
    """批量启动下载。items: [{url, title?, page?}]。返回 task_id 列表。

    max_concurrent 不传则用 config.MAX_CONCURRENT(默认 2)。
    """
    mc = max_concurrent if max_concurrent is not None else config.MAX_CONCURRENT
    sem = threading.Semaphore(max(1, int(mc)))
    task_ids = []

    def _batch_run(task_id, url, label, page, title):
        sem.acquire()
        try:
            _run_download(task_id, url, label, [], page)
        finally:
            sem.release()

    import time as _t
    for item in items:
        url = item.get("url", "")
        page = int(item.get("page", 1))
        title = item.get("title", "") or "(解析中)"
        # 跳过重复(同 url+page 在跑)
        if find_active_by_url(url, label) and page == 1:
            continue
        task_id = uuid.uuid4().hex[:12]
        with _tasks_lock:
            _tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "progress": 0, "speed": 0, "eta": 0,
                "title": title, "url": url, "label": label,
                "thumbnail": item.get("thumbnail", ""),
                "uploader": item.get("uploader", ""),
                "duration": item.get("duration") or 0,
                "created_at": int(_t.time()),
                "page": page, "error": "", "cancel": False,
            }
        th = threading.Thread(target=_batch_run,
                              args=(task_id, url, label, page, title), daemon=True)
        with _tasks_lock:
            _threads[task_id] = th
        th.start()
        task_ids.append(task_id)
    _persist()
    return task_ids


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
    """返回任务快照(拷贝)。调用方(SSE/jsonify)序列化时不受下载线程并发写影响。"""
    with _tasks_lock:
        t = _tasks.get(task_id)
        return dict(t) if t else None


def list_tasks() -> list[dict]:
    """返回全部任务的快照列表(每项为拷贝)。"""
    with _tasks_lock:
        return [dict(t) for t in _tasks.values()]


def find_active_by_url(url: str, label: str, page: int = 1) -> Optional[str]:
    """查找同 URL+清晰度+分P 且仍在进行中的任务,用于重复下载拦截。返回 task_id 或 None。"""
    with _tasks_lock:
        for tid, t in _tasks.items():
            if (t.get("url") == url and t.get("label") == label
                    and t.get("page", 1) == page
                    and t["status"] in ("queued", "downloading", "merging", "cancelling")):
                return tid
    return None


_ACTIVE = ("queued", "downloading", "merging", "cancelling")


def _task_files(t: dict) -> list:
    """根据任务标题在下载目录找对应文件(yt-dlp 文件名以标题开头)。"""
    from pathlib import Path
    title = (t.get("title") or "").strip()
    if not title:
        return []
    prefix = title[:30]  # CJK 标题原样保留,30字符(~90B)在截断范围内,足够唯一
    try:
        return [f for f in config.DOWNLOAD_DIR.iterdir()
                if f.is_file() and f.name.startswith(prefix)]
    except Exception:
        return []


def delete_tasks(task_ids: list, delete_file: bool) -> int:
    """删除任务记录。delete_file=True 时连已下载文件一起删。进行中任务先取消。"""
    removed = []
    with _tasks_lock:
        for tid in task_ids:
            t = _tasks.get(tid)
            if not t:
                continue
            if t["status"] in _ACTIVE:
                t["cancel"] = True  # 进行中的先标记取消
            removed.append(t)
            del _tasks[tid]
    if delete_file:
        for t in removed:
            for f in _task_files(t):
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
    _persist()
    return len(removed)


def clear_tasks(delete_file: bool) -> int:
    """清除所有非活跃任务的记录(可选连文件删除)。"""
    with _tasks_lock:
        ids = [tid for tid, t in _tasks.items() if t["status"] not in _ACTIVE]
    return delete_tasks(ids, delete_file)
