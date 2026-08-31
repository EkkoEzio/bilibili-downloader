"""yt-dlp 下载执行:线程编排、进度/取消钩子、重试、批量并发。

任务状态与持久化在 taskstore.py,解析在 parsing.py,文件名对齐在 files.py;
本模块只保留「跑下载」的逻辑,并再导出下游(app.py/auto_refresh/测试)惯用的
名字——测试在 downloader 命名空间对 _ydl_opts/_persist 等打补丁,依赖这里的
模块级绑定。"""
import logging
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import yt_dlp

import extras
import config
from quality import best_video_audio_choice

from taskstore import (_tasks, _tasks_lock, _persist, _final_paths,
                       _load_persisted, _task_files, TERMINAL,
                       get_task, list_tasks, find_active_by_url, is_downloaded,
                       find_task_files, delete_tasks, clear_tasks,
                       clean_invalid_records)
from parsing import parse_video, parse_part, resolve_playlist, _ydl_opts
from files import (build_filename, tmpl_literal, _MEDIA_EXTS,
                   align_task_file as _align_task_file,
                   resolve_rename_conflict, reveal_path, open_dir)

log = logging.getLogger("bd.downloader")

# 已下载判定复用的 bvid 提取(extras 也用它定位弹幕)
bvid_from_url = extras.bvid_from_url


def _to_mid(v) -> int:
    """宽松转 mid(yt-dlp 给的是字符串,批量项可能给 int,脏数据给 0)。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _base_opts(fmt_str: str, page: int, task_id: str) -> dict:
    """构造下载基础 opts:format、输出模板(可选按 UP 归档)、附件开关、进度钩子。

    落盘命名权在 Python 侧(设计 A.2):outtmpl 用任务权威 title 组装好的
    字面文件名 + `.%(ext)s`,不再让 yt-dlp 按字节截断 %(title).200B——
    CJK 下字节/字符截断边界错位是「记录与文件对不上」的根因之一。
    任务无权威 title 时(仅防御,正常流程到不了)退回旧模板。

    仅音频/单体格式不设 merge_output_format;组合轨(DASH 视频+音频)或
    best 需要合并时才要求输出 mp4。
    """
    with _tasks_lock:
        t = dict(_tasks.get(task_id) or {})
    title = (t.get("title") or "").strip()
    uploader = (t.get("uploader") or "").strip()
    if title and title != "(解析中)":
        stem = tmpl_literal(build_filename(title, page))
        name_tmpl = f"{stem}.%(ext)s"
        # UP 子目录也用清洗后的字面名;批量任务没透传 UP 名时退回 live 字段
        if config.SORT_BY_UP:
            sub = (f"{tmpl_literal(extras.sanitize_filename(uploader))}/"
                   if uploader else "%(uploader)s/")
        else:
            sub = ""
    else:
        name_tmpl = ("%(title).150B - P%(playlist_index)s.%(ext)s" if page > 1
                     else "%(title).200B.%(ext)s")
        sub = "%(uploader)s/" if config.SORT_BY_UP else ""
    opts = {
        "format": fmt_str,
        "outtmpl": str(config.DOWNLOAD_DIR / (sub + name_tmpl)),
        "progress_hooks": [_progress_hook(task_id)],
        "postprocessor_hooks": [_pp_hook(task_id)],
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


def _pp_hook(task_id: str):
    def hook(d):
        if d.get("status") != "finished":
            return
        fp = (d.get("info_dict") or {}).get("filepath") or ""
        if fp and Path(fp).suffix.lower() in _MEDIA_EXTS:
            _final_paths[task_id] = fp
    return hook


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
                # 权威 title 缺失(批量未透传且预解析没拿到)时,
                # 用 yt-dlp 最终解析结果兜底一次(A.1 例外分支)
                if not t.get("title") or t["title"] == "(解析中)":
                    ft = ((d.get("info_dict") or {}).get("title") or "").strip()
                    if ft:
                        t["title"] = ft
                persist = True
            elif d["status"] == "error":
                t["status"] = "failed"
                t["error"] = "下载出错"
                persist = True
        if persist:
            _persist()
    return hook


def _finalize_cancelled(task_id: str) -> None:
    """把未终结的任务落为已取消(取消发生在下载回调之外时的兜底)。

    终态判断走 taskstore.TERMINAL 唯一来源。曾硬编码 ("done","failed","cancelled")
    漏了 interrupted:那会把一个已终结的 interrupted 覆盖成 cancelled,与「已终结的
    任务状态不被覆盖」相悖。改用 TERMINAL 后 interrupted 同样被视为已终结。
    """
    with _tasks_lock:
        t = _tasks.get(task_id)
        if t and t["status"] not in TERMINAL:
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
                    # 入队即锚定(A.1):title 只在缺失/占位时补,绝不二次覆盖,
                    # 否则 UP 改标题会造成「页面 A、磁盘 B」漂移
                    if not t.get("title") or t["title"] == "(解析中)":
                        t["title"] = summary["title"]
                    # 封面/UP/时长补进任务,供「下载记录」页像观看历史一样渲染卡片
                    if not t.get("thumbnail"):
                        t["thumbnail"] = summary.get("thumbnail", "")
                    if not t.get("uploader"):
                        t["uploader"] = summary.get("uploader", "")
                    if not t.get("uploader_mid"):
                        t["uploader_mid"] = _to_mid(summary.get("uploader_mid"))
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
            time.sleep(2 + attempt * 2)  # 递增退避
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
        _final_paths.pop(task_id, None)  # 上一次尝试捕获的路径作废
        opts = _ydl_opts(base_opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            # 成功:先把落盘文件名对齐任务 title(A.3),
            # 统计/弹幕/Finder 定位都用对齐后的权威路径
            _align_task_file(task_id)
            # 统计实际下载文件大小,用于 C3 统计面板
            files = []
            done_ok = False
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t and t["status"] != "failed":
                    t["status"] = "cancelled" if t.get("cancel") else "done"
                    t["error"] = ""
                    t["finished_at"] = int(time.time())
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


def start_download(url: str, label: str, page: int = 1, uploader_mid: int = 0) -> str:
    """启动异步下载。先解析拿到 formats,再后台下载。返回 task_id。

    page>1 表示多 P 视频的指定分集。uploader_mid 是调用方已知的 UP mid
    (没传时用解析结果里的 yt-dlp uploader_id)。
    """
    if page > 1:
        summary = parse_part(url, page)
    else:
        summary = parse_video(url)
        # parse_video 对多 P 不返回 formats,这里兜底拉第一 P
        if not summary.get("formats"):
            summary = parse_part(url, 1)
    if not uploader_mid:
        uploader_mid = _to_mid(summary.get("uploader_mid"))
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0, "speed": 0, "eta": 0,
            "title": summary["title"], "url": url, "label": label,
            "thumbnail": summary.get("thumbnail", ""),
            "uploader": summary.get("uploader", ""),
            "uploader_mid": uploader_mid or _to_mid(summary.get("uploader_mid")),
            "duration": summary.get("duration") or 0,
            "created_at": int(time.time()),
            "page": page, "error": "", "cancel": False,
        }
    th = threading.Thread(target=_run_download,
                          args=(task_id, url, label, summary["formats"], page),
                          daemon=True)
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
                "uploader_mid": _to_mid(item.get("uploader_mid")),
                "duration": item.get("duration") or 0,
                "created_at": int(time.time()),
                "page": page, "error": "", "cancel": False,
            }
        th = threading.Thread(target=_batch_run,
                              args=(task_id, url, label, page, title), daemon=True)
        th.start()
        task_ids.append(task_id)
    _persist()
    return task_ids


def cancel_download(task_id: str) -> bool:
    """标记任务取消。yt-dlp 线程会在下个分片边界停止(无法强杀,靠标志位)。

    终态判断走 taskstore.TERMINAL 唯一来源。曾硬编码 ("done","failed","cancelled")
    漏了 interrupted:对中断任务调取消会把它改成 cancelling(活跃态)却没有任何线程
    去收尾(interrupted 由 _load_persisted 在重启时写入,对应线程早已不存在)
    → 永久僵尸态,连带两个后果:find_active_by_url 从此拦截同 URL + 清晰度 + 分 P 的
    新下载(409,视频再也下不了),clear_tasks 又把它当活跃任务跳过(「清除全部记录」
    也删不掉这条)。
    """
    with _tasks_lock:
        t = _tasks.get(task_id)
        if not t or t["status"] in TERMINAL:
            return False
        t["cancel"] = True
        t["status"] = "cancelling"
    _persist()
    return True
