"""yt-dlp 下载执行:线程编排、进度/取消钩子、重试、批量并发。

任务状态与持久化在 taskstore.py,解析在 parsing.py,文件名对齐在 files.py;
本模块只保留「跑下载」的逻辑,并再导出下游(app.py/auto_refresh/测试)惯用的
名字——测试在 downloader 命名空间对 _ydl_opts/_persist 等打补丁,依赖这里的
模块级绑定。"""
import importlib.metadata
import logging
import queue
import shlex
import shutil
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
from request_governor import UpstreamRateLimitError, governor

from taskstore import (_tasks, _tasks_lock, _persist, _final_paths,
                       _final_paths_lock, _load_persisted, _task_files,
                       _task_files_state,
                       ACTIVE, TERMINAL, TaskStoreWriteError,
                       get_task, list_tasks, find_active_by_url, is_downloaded,
                       find_task_files, delete_tasks as _store_delete_tasks,
                       delete_tasks_detailed as _store_delete_tasks_detailed,
                       delete_invalid_tasks as _store_delete_invalid_tasks,
                       clear_tasks as _store_clear_tasks,
                       clean_invalid_records as _store_clean_invalid_records,
                       create_tasks_if_absent, request_task_cancellation,
                       resolve_download_path, UnsafeDownloadPathError,
                       task_download_root,
                       artifact_lifecycle, refresh_task_manifest,
                       manifest_media_path, mark_persistence_pending,
                       mark_persistence_success, mark_persistence_failure,
                       new_artifact_set_id,
                       TASK_SCHEMA_VERSION, MANIFEST_VERSION)
from parsing import parse_video, parse_part, resolve_playlist, _ydl_opts
from files import (build_filename, tmpl_literal, _MEDIA_EXTS,
                   align_task_file as _align_task_file,
                   resolve_rename_conflict, reveal_path, open_dir)

log = logging.getLogger("bd.downloader")

# 已下载判定复用的 bvid 提取(extras 也用它定位弹幕)
bvid_from_url = extras.bvid_from_url


class DuplicateActiveTask(RuntimeError):
    """原子创建阶段发现同 URL/清晰度/分 P 的活跃任务。"""

    def __init__(self, task_id: str) -> None:
        super().__init__("该视频相同清晰度正在下载中")
        self.task_id = task_id


def _persist_task_state(task_ids, context: str, *, raise_on_failure: bool = False) -> bool:
    """持久化任务状态并把写盘健康暴露到任务本身。

    兼容旧测试桩：只有显式 ``False`` 表示失败，返回 ``None`` 的 monkeypatch
    仍按既有成功语义处理。同步取消会要求 ``raise_on_failure``。
    """
    ids = [str(task_id) for task_id in task_ids]
    token = mark_persistence_pending(ids)
    result = _persist()
    if result is not False:
        mark_persistence_success(token)
        return True
    message = f"{context}未能写入 tasks.json"
    mark_persistence_failure(token, message)
    log.error("%s task_ids=%s", message, ids)
    if raise_on_failure:
        raise TaskStoreWriteError(message)
    return False


class _RuntimeJob:
    """队列任务的运行时句柄；不进入 tasks.json。"""

    def __init__(self, args: tuple) -> None:
        self.args = args
        self.task_id = str(args[0])
        self.quiesced = threading.Event()
        # worker 真正开始执行 _run_download 前置位。区分「还在队列里排队」与
        # 「已进入执行」:前者从未碰过文件,删除记录无需等待 worker 静默。
        self.started = threading.Event()


# 单一全局队列 + 固定 worker 数。运行时并发值由 config.MAX_CONCURRENT 控制，
# 因此单下载、任意批次和自动追更共享同一个上限，不再创建等待线程。
_download_jobs: queue.Queue[_RuntimeJob] = queue.Queue()
_scheduler_condition = threading.Condition()
_scheduler_active = 0
_workers_lock = threading.Lock()
_workers: list[threading.Thread] = []
_runtime_jobs_lock = threading.RLock()  # 兼作任务发布/registry 生命周期门
_runtime_jobs: dict[str, _RuntimeJob] = {}
DELETE_QUIESCE_TIMEOUT = 10.0


def _acquire_download_slot() -> None:
    global _scheduler_active
    with _scheduler_condition:
        while True:
            limit = max(1, min(config.MAX_CONCURRENT_LIMIT,
                               int(config.MAX_CONCURRENT)))
            if _scheduler_active < limit:
                _scheduler_active += 1
                return
            # 定时重读运行时配置；调高并发无需重启应用。
            _scheduler_condition.wait(timeout=0.5)


def _release_download_slot() -> None:
    global _scheduler_active
    with _scheduler_condition:
        _scheduler_active = max(0, _scheduler_active - 1)
        _scheduler_condition.notify_all()


def _mark_tasks_failed(task_ids, error: str) -> None:
    """把调度器边界异常落为 failed；写盘失败会留在任务健康字段中。"""
    changed: list[str] = []
    with _tasks_lock:
        for task_id in task_ids:
            task = _tasks.get(task_id)
            if not task or task.get("status") in TERMINAL:
                continue
            task["status"] = "failed"
            task["error"] = error
            task["finished_at"] = int(time.time())
            changed.append(str(task_id))
    if changed:
        _persist_task_state(changed, "调度异常终态")


def _download_worker() -> None:
    while True:
        runtime = _download_jobs.get()
        acquired = False
        try:
            _acquire_download_slot()
            acquired = True
            runtime.started.set()
            # 运行时解析模块绑定，保留测试/调用方 monkeypatch _run_download 的能力。
            _run_download(*runtime.args)
        except Exception as exc:
            log.exception("下载 worker 未捕获异常 task_id=%s", runtime.task_id)
            _mark_tasks_failed([runtime.task_id], f"下载执行异常: {exc}")
        finally:
            try:
                # monkeypatch 的 _run_download 也可能没有收尾；在 quiesced 前确保
                # 已接受的取消至少落到 cancelled。该调用不持 registry/store 锁。
                with _tasks_lock:
                    task = _tasks.get(runtime.task_id)
                    needs_cancel_finalize = bool(
                        task and task.get("cancel") and task.get("status") in ACTIVE)
                if needs_cancel_finalize:
                    _finalize_cancelled(runtime.task_id)
            finally:
                if acquired:
                    _release_download_slot()
                runtime.quiesced.set()
                with _runtime_jobs_lock:
                    if _runtime_jobs.get(runtime.task_id) is runtime:
                        _runtime_jobs.pop(runtime.task_id, None)
                _download_jobs.task_done()


def _ensure_workers() -> None:
    """回收已退出的 worker，并补足固定池；线程名在存活池内保持唯一。"""
    with _workers_lock:
        _workers[:] = [worker for worker in _workers if worker.is_alive()]
        used_names = {worker.name for worker in _workers}
        for index in range(1, config.MAX_CONCURRENT_LIMIT + 1):
            if len(_workers) >= config.MAX_CONCURRENT_LIMIT:
                break
            name = f"bd-download-{index}"
            if name in used_names:
                continue
            th = threading.Thread(target=_download_worker, daemon=True, name=name)
            th.start()
            _workers.append(th)
            used_names.add(name)


def _enqueue_downloads(jobs: list[tuple]) -> None:
    if not jobs:
        return
    _ensure_workers()
    for args in jobs:
        runtime = _RuntimeJob(args)
        with _runtime_jobs_lock:
            previous = _runtime_jobs.get(runtime.task_id)
            if previous and not previous.quiesced.is_set():
                raise RuntimeError(f"任务已有运行时作业:{runtime.task_id}")
            _runtime_jobs[runtime.task_id] = runtime
        try:
            _download_jobs.put(runtime)
        except Exception:
            with _runtime_jobs_lock:
                if _runtime_jobs.get(runtime.task_id) is runtime:
                    _runtime_jobs.pop(runtime.task_id, None)
            runtime.quiesced.set()
            raise


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
    download_root = task_download_root(t)
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
    # 流数决定进度聚合的分母与「全部完成」判据。组合轨 a+b → 2 流;
    # "best" 必须按 2 流计:它可能被 yt-dlp 解析成 bestvideo+bestaudio(下面
    # merge_output_format 正是为此而设),只数 "+" 会把它当单流——第一条流
    # finished 就置 merging/100%,第二条流一开始状态又退回 downloading,
    # 进度卡在 100% 直到音轨下完,正是本次聚合要消灭的回跳/闪烁。
    stream_count = 2 if fmt_str == "best" else fmt_str.count("+") + 1
    opts = {
        "format": fmt_str,
        "outtmpl": str(download_root / (sub + name_tmpl)),
        # DASH 组合轨会按流依次触发进度回调,聚合进度需要知道流数。
        "progress_hooks": [_progress_hook(task_id, stream_count)],
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


def _terminal_notifier_path() -> str | None:
    """返回支持点击动作的 terminal-notifier；优先使用用户安装的新版本。

    pync 2.0.3 的 Python 包装层会导入一个未声明的 dateutil 依赖，因此不导入
    它，只把该固定版本当作 terminal-notifier.app 的发行载体。定位发行文件走
    importlib.metadata，不依赖虚拟环境目录名或 Python 小版本。
    """
    installed = shutil.which("terminal-notifier")
    if installed:
        return installed
    try:
        dist = importlib.metadata.distribution("pync")
        vendor = Path(dist.locate_file("pync/vendor"))
        candidates = sorted(vendor.glob(
            "terminal-notifier-*/terminal-notifier.app/Contents/MacOS/terminal-notifier"
        ), reverse=True)
        if candidates and candidates[0].is_file():
            return str(candidates[0])
    except (importlib.metadata.PackageNotFoundError, OSError):
        pass
    return None


def _notification_media_path(actual_filename: str, files: list[Path],
                             task: dict | None = None) -> str | None:
    """选出通知应定位的唯一安全主媒体，绝不误选副文件。"""
    try:
        root = task_download_root(task or {})
    except UnsafeDownloadPathError:
        return None
    candidates: list[Path] = []
    if actual_filename:
        candidates.append(Path(actual_filename))
    candidates.extend(Path(path) for path in files)

    media: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            path = resolve_download_path(candidate, root=root)
        except UnsafeDownloadPathError:
            continue
        if (path not in seen and path.is_file()
                and path.suffix.lower() in _MEDIA_EXTS):
            seen.add(path)
            media.append(path)
    return str(media[0]) if len(media) == 1 else None


def _finder_click_action(file_path: str | None) -> str:
    """构造通知点击命令：Finder 选中文件，文件失效时打开可靠的父目录。"""
    try:
        download_dir = Path(config.DOWNLOAD_DIR).expanduser().resolve(strict=False)
    except OSError:
        download_dir = Path(config.DOWNLOAD_DIR).expanduser().absolute()

    target = None
    if file_path:
        try:
            target = resolve_download_path(file_path)
        except UnsafeDownloadPathError:
            log.warning("通知文件路径越出下载目录，降级为打开下载目录: %s", file_path)
    fallback = target.parent if target and target.parent.is_dir() else download_dir
    quoted_fallback = shlex.quote(str(fallback))
    if target is None:
        return f"/usr/bin/open {quoted_fallback}"

    # terminal-notifier 的 -execute 明确定义为 shell command；每个来自标题/配置的
    # 路径都必须逐项 quote，不能把原始文件名拼进命令。点击可能晚于文件移动，
    # 所以在执行时重新判断，并在 open -R 失败时再次降级到目录。
    quoted_target = shlex.quote(str(target))
    return (
        f"if [ -f {quoted_target} ]; then "
        f"/usr/bin/open -R {quoted_target} || /usr/bin/open {quoted_fallback}; "
        f"else /usr/bin/open {quoted_fallback}; fi"
    )


def _notify_done(task_id: str, title: str, file_path: str | None) -> None:
    """发送可点击的 macOS 完成通知；任何失败都不得影响下载状态。"""
    if not config.NOTIFY or sys.platform != "darwin":
        return

    try:
        notifier = _terminal_notifier_path()
        if not notifier:
            log.warning(
                "未找到 terminal-notifier，跳过下载完成通知；请重新运行启动脚本安装依赖"
            )
            return

        safe_title = " ".join((title or "视频").split())[:80]
        argv = [
            notifier,
            "-title", "bilibili download",
            "-subtitle", "点击在 Finder 中显示",
            "-message", f"《{safe_title}》下载完成",
            "-group", f"bilibili-download-{task_id}",
            "-execute", _finder_click_action(file_path),
        ]
        result = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            log.warning(
                "发送下载完成通知失败 task_id=%s returncode=%s%s",
                task_id,
                result.returncode,
                f": {detail}" if detail else "",
            )
    except Exception as exc:
        # 通知是下载完成后的附加能力；二进制发现、命令构造或执行失败时只记日志。
        log.warning("发送下载完成通知失败 task_id=%s: %s", task_id, exc)


def _pp_hook(task_id: str):
    def hook(d):
        if d.get("status") != "finished":
            return
        fp = (d.get("info_dict") or {}).get("filepath") or ""
        try:
            task = get_task(task_id) or {}
            root = task_download_root(task)
            safe = resolve_download_path(fp, root=root)
        except UnsafeDownloadPathError:
            if fp:
                log.error("拒绝越界 postprocessor 路径 task_id=%s path=%s", task_id, fp)
            return
        if safe.suffix.lower() in _MEDIA_EXTS:
            with _final_paths_lock:
                _final_paths[task_id] = str(safe)
    return hook


def _progress_hook(task_id: str, expected_streams: int = 1):
    """聚合多流进度:任务进度 = 各流已下载字节总和 / 各流总字节总和。

    DASH 组合轨(如 1080P = 纯视频流 + 独立音轨)按流依次下载。旧实现直接
    取「最近一次回调」的流内百分比:视频流下完 100% 后音轨从 0% 重新爬,
    页面进度条 100%→0%→100% 回跳,状态还闪一次「合并中」再回「下载中」。
    这里按 format_id 分别记账,完成的流按满额计入,进度全程单调。

    ``expected_streams`` 是 format 串里的流数(``a+b`` → 2);全部流 finished
    才进入 merging。回调缺 format_id 时退化为单桶记账(等价旧行为)。

    两条硬约束(否则「单调」只是名义上的):
    1. 全部流完成前进度封顶 99%。第一条流下完时它独自占满分母会先到
       100%,第二条流一出现分母变大就掉回 80% 之类的值;
    2. 进度取历史最大值。回调乱序、分母变化都不得让已显示的进度回退。
    """
    state = {"bucket": None, "streams": {}, "finished": 0}

    def reset():
        """丢弃上一次尝试的记账。

        ``_base_opts`` 在重试循环之外构建,同一个 hook 闭包会跨 attempt 复用。
        不重置的话,上一次尝试残留的 ``finished`` 计数会让重试的第一个 finished
        回调立刻满足「全部流完成」——进度跳到 100%、状态误报 merging,而音轨
        其实还没下;残留的流字节也会让重试首帧算出接近 100% 的进度。

        任务上的 progress 一并清零:进度取历史最大值(只增不减),留着上一次
        的 100 会把重试期间的任何真实进度都压成 99。
        """
        state["bucket"] = None
        state["streams"].clear()
        state["finished"] = 0
        with _tasks_lock:
            task = _tasks.get(task_id)
            if task is not None:
                task["progress"] = 0

    def _stream_key(d: dict) -> str:
        return str((d.get("info_dict") or {}).get("format_id") or "?")

    def hook(d):
        persist = False
        with _tasks_lock:
            t = _tasks.get(task_id)
            # 取消或任务记录已删:抛异常让 yt-dlp 立即中止,
            # 否则下载会完整跑完才被标记(浪费带宽/磁盘)。
            if not t or t.get("cancel"):
                raise yt_dlp.utils.DownloadCancelled("任务已取消")
            status = d.get("status")
            if status == "downloading":
                # 进入下载态(yt-dlp hook 第一次回调就把 queued→downloading)
                t["status"] = "downloading"
                # yt-dlp 这些字段可能为 None,统一兜底为 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                streams = dict(state["streams"])
                streams[_stream_key(d)] = (int(downloaded), int(total))
                state["streams"] = streams
                total_sum = sum(size for _dl, size in streams.values())
                downloaded_sum = sum(dl for dl, _size in streams.values())
                pct = (downloaded_sum / total_sum * 100) if total_sum else 0.0
                expected = max(1, int(expected_streams or 1))
                if state["finished"] < expected:
                    # 还有流没完成:不许显示 100%,否则进度条会先满格再掉回
                    pct = min(pct, 99.0)
                pct = max(float(t.get("progress") or 0), pct)  # 只增不减
                # downloading 是高频更新,节流:每进入一个新的 5% 桶才落一次盘
                t["progress"] = round(pct, 1)
                speed = d.get("speed") or 0
                t["speed"] = round(speed / 1024, 1)  # KB/s
                t["eta"] = d.get("eta") or 0
                bucket = int(t["progress"]) // 5
                if bucket != state["bucket"]:
                    state["bucket"] = bucket
                    persist = True
            elif status == "finished":
                state["finished"] += 1
                # 该流完成:按当前已知量满额计入聚合,其余流继续推进。
                key = _stream_key(d)
                last = state["streams"].get(key)
                if last is not None:
                    state["streams"][key] = (last[1] or last[0],) * 2
                if state["finished"] >= max(1, int(expected_streams or 1)):
                    t["progress"] = 100
                    t["status"] = "merging"  # yt-dlp 合并阶段
                    # 权威 title 缺失(批量未透传且预解析没拿到)时,
                    # 用 yt-dlp 最终解析结果兜底一次(A.1 例外分支)
                    if not t.get("title") or t["title"] == "(解析中)":
                        ft = ((d.get("info_dict") or {}).get("title") or "").strip()
                        if ft:
                            t["title"] = ft
                    persist = True
                # 单条流完成不落 merging/100%:聚合进度仍由剩余流推进。
            elif status == "error":
                t["status"] = "failed"
                t["error"] = "下载出错"
                t["finished_at"] = int(time.time())
                persist = True
        if persist:
            # 高频进度失败只标记健康，不把异常抛回 yt-dlp 下载线程。
            _persist_task_state([task_id], "下载进度")
    hook.reset = reset
    return hook


def _finalize_cancelled(task_id: str) -> None:
    """把未终结任务落为 cancelled；持久化失败会显式标记健康状态。"""
    changed = False
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task and task["status"] not in TERMINAL:
            task["status"] = "cancelled"
            changed = True
    if changed:
        _persist_task_state([task_id], "取消终态")


def _run_download(task_id: str, url: str, label: str, formats: list[dict], page: int) -> None:
    # 队列中等待时任务可能已被取消或删除；先检查，避免仍发解析请求。
    with _tasks_lock:
        initial = _tasks.get(task_id)
        cancelled = bool(initial and initial.get("cancel"))
        already_terminal = bool(initial and initial.get("status") in TERMINAL)
    if not initial or already_terminal:
        return
    if cancelled:
        _finalize_cancelled(task_id)
        return

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
                    t["finished_at"] = int(time.time())
            _persist_task_state([task_id], "解析失败终态")
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
            _persist_task_state([task_id], "重试状态")
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

        # 每次重试都重建 YoutubeDL 实例(避免内部状态污染)，并统一经过媒体启动闸门。
        with _final_paths_lock:
            _final_paths.pop(task_id, None)  # 上一次尝试捕获的路径作废
        # base_opts 在循环外构建,hook 闭包跨 attempt 复用:不清掉上一次尝试的
        # 流字节与 finished 计数,重试会误判「全部流已完成」而直接报 100%。
        for hook in base_opts.get("progress_hooks") or ():
            reset_hook = getattr(hook, "reset", None)
            if callable(reset_hook):
                reset_hook()
        opts = _ydl_opts(base_opts)
        try:
            governor.wait("media", endpoint=url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            # 成功：先对齐主媒体。align 会安全刷新一次 manifest。
            aligned_media = _align_task_file(task_id)
            files: list[Path] = []
            done_title = ""
            actual_filename = ""

            # 弹幕会新增 sidecar；在同一任务产物锁内保存并再次刷新 manifest，
            # 终态提交因此包含最终文件集和最终 downloaded_bytes。
            with artifact_lifecycle(task_id):
                snapshot = get_task(task_id)
                if not snapshot or snapshot.get("status") == "failed":
                    return
                try:
                    root = task_download_root(snapshot)
                except UnsafeDownloadPathError:
                    root = None
                media = None
                if aligned_media and root is not None:
                    try:
                        media = resolve_download_path(
                            aligned_media, must_exist=True, root=root)
                    except (UnsafeDownloadPathError, FileNotFoundError):
                        media = None
                if media is None:
                    media = manifest_media_path(snapshot)
                cancelled_before_sidecar = bool(snapshot.get("cancel"))
                if (not cancelled_before_sidecar and config.SAVE_DANMAKU
                        and media is not None):
                    try:
                        extras.save_danmaku_for(url, media, page)
                    except Exception:
                        log.exception("弹幕保存失败 %s", url)
                if media is not None:
                    files = refresh_task_manifest(task_id, media)
                else:
                    latest = get_task(task_id)
                    files = _task_files(latest) if latest else []

            latest_artifacts = get_task(task_id)
            manifest_required = bool(latest_artifacts and "manifest" in latest_artifacts)
            authoritative_media = (manifest_media_path(latest_artifacts)
                                   if manifest_required else None)
            manifest_ok = not manifest_required or authoritative_media is not None

            downloaded_bytes = 0
            for path in files:
                try:
                    downloaded_bytes += path.stat().st_size
                except OSError:
                    continue

            done_ok = False
            terminal_snapshot = None
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t and t["status"] != "failed":
                    if t.get("cancel"):
                        t["status"] = "cancelled"
                        t["error"] = ""
                    elif not manifest_ok:
                        t["status"] = "failed"
                        t["error"] = "下载命令成功，但无法确认安全落盘媒体"
                    else:
                        t["status"] = "done"
                        t["error"] = ""
                    t["finished_at"] = int(time.time())
                    t["downloaded_bytes"] = downloaded_bytes
                    done_ok = t["status"] == "done"
                    done_title = str(t.get("title") or "视频")
                    actual_filename = str(t.get("actual_filename") or "")
                    terminal_snapshot = dict(t)
            if terminal_snapshot is not None:
                _persist_task_state([task_id], "下载终态与产物清单")

            # 通知不再创建产物，放在终态提交之后；失败与任务状态隔离。
            if done_ok:
                media_path = None
                try:
                    media_path = _notification_media_path(
                        actual_filename, files, terminal_snapshot)
                except Exception:
                    log.exception("通知媒体路径解析失败 task_id=%s", task_id)
                if config.NOTIFY:
                    try:
                        _notify_done(task_id, done_title, media_path)
                    except Exception:
                        log.exception("下载完成通知失败 task_id=%s", task_id)
                log.info("任务完成 %s 《%s》", task_id, done_title)
            return
        except UpstreamRateLimitError as e:
            # 账号冷却期间快速失败，不能用重试循环继续制造请求。
            last_err = e
            break
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
    changed = False
    with _tasks_lock:
        t = _tasks.get(task_id)
        if t and not t.get("cancel"):
            t["status"] = "failed"
            t["error"] = str(last_err)
            t["finished_at"] = int(time.time())
            changed = True
    if changed:
        _persist_task_state([task_id], "下载失败终态")


def _new_task(url: str, label: str, page: int, *, title: str,
              thumbnail: str = "", uploader: str = "", uploader_mid=0,
              duration=0, bvid: str = "", source: str = "",
              auto_attempt=0) -> dict:
    """构造尚未可见的 queued 任务；可见性由 create_tasks_if_absent 提交。"""
    task = {
        "task_id": uuid.uuid4().hex,
        "schema_version": TASK_SCHEMA_VERSION,
        "artifact_set_id": new_artifact_set_id(),
        "artifact_root": str(Path(config.DOWNLOAD_DIR).expanduser().resolve(strict=False)),
        "manifest": {"version": MANIFEST_VERSION, "artifacts": []},
        "durable": True,
        "status": "queued",
        "progress": 0, "speed": 0, "eta": 0,
        "title": title, "url": url, "label": label,
        "thumbnail": thumbnail,
        "uploader": uploader,
        "uploader_mid": _to_mid(uploader_mid),
        "duration": duration or 0,
        "created_at": int(time.time()),
        "page": page, "error": "", "cancel": False,
    }
    if bvid:
        task["bvid"] = str(bvid)
    if source:
        task["source"] = str(source)
        if source == "auto_refresh":
            task["auto_attempt"] = max(0, _to_mid(auto_attempt))
    return task


def start_download(url: str, label: str, page: int = 1, uploader_mid: int = 0) -> str:
    """解析并可靠入队单个下载，返回 task_id。

    快速预查避免已知重复任务仍发解析请求；最终正确性由
    create_tasks_if_absent 的持久化锁内原子去重保证。任务只有在完整候选快照
    成功写盘后才进入内存和全局下载队列，因此磁盘写失败不会产生幽灵下载。
    """
    existing = find_active_by_url(url, label, page)
    if existing:
        raise DuplicateActiveTask(existing)

    if page > 1:
        summary = parse_part(url, page)
    else:
        summary = parse_video(url)
        # parse_video 对多 P 不返回 formats,这里兜底拉第一 P
        if not summary.get("formats"):
            summary = parse_part(url, 1)

    task = _new_task(
        url, label, page,
        title=summary["title"],
        thumbnail=summary.get("thumbnail", ""),
        uploader=summary.get("uploader", ""),
        uploader_mid=uploader_mid or summary.get("uploader_mid"),
        duration=summary.get("duration"),
    )
    with _runtime_jobs_lock:
        created, duplicates = create_tasks_if_absent([task])
        if not created:
            raise DuplicateActiveTask(duplicates[task["task_id"]])

        task_id = created[0]["task_id"]
        try:
            _enqueue_downloads([
                (task_id, url, label, summary.get("formats") or [], page),
            ])
        except Exception as e:
            _mark_tasks_failed([task_id], f"下载入队失败: {e}")
            raise
    return task_id


def start_batch(items: list[dict], label: str, max_concurrent: int = None) -> list[str]:
    """原子创建并入队批量下载，返回本次真正创建的 task_id 列表。

    ``max_concurrent`` 为兼容自动追更和旧调用保留；实际并发统一由运行时
    ``config.MAX_CONCURRENT`` 的全局 gate 控制，避免不同批次各自放大并发。
    """
    candidates: list[dict] = []
    for item in items:
        url = str(item.get("url") or "")
        if not url:
            continue
        try:
            page = int(item.get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        candidates.append(_new_task(
            url, label, page,
            title=item.get("title", "") or "(解析中)",
            thumbnail=item.get("thumbnail", ""),
            uploader=item.get("uploader", ""),
            uploader_mid=item.get("uploader_mid"),
            duration=item.get("duration"),
            bvid=item.get("bvid", ""),
            source=item.get("source", ""),
            auto_attempt=item.get("auto_attempt", 0),
        ))

    with _runtime_jobs_lock:
        created, _duplicates = create_tasks_if_absent(candidates)
        jobs = [
            (task["task_id"], task["url"], task["label"], [], task["page"])
            for task in created
        ]
        task_ids = [task["task_id"] for task in created]
        try:
            _enqueue_downloads(jobs)
        except Exception as e:
            _mark_tasks_failed(task_ids, f"下载入队失败: {e}")
            raise
    return task_ids


def cancel_download(task_id: str) -> bool:
    """durable-before-publish 地取消；写失败时 worker 绝不会看到取消标志。"""
    return request_task_cancellation(task_id)


def _quiesce_task_ids(task_ids: list[str], *, cancel_active: bool,
                        timeout: float | None = None) -> list[str]:
    """取得可安全交给存储层删除的任务；等待期间绝不持 registry/store 锁。"""
    ready: list[str] = []
    waiting: list[tuple[str, _RuntimeJob]] = []

    for task_id in dict.fromkeys(str(value) for value in task_ids):
        # 发布任务与注册 runtime 由同一 RLock 包围；这里拿到的是一致观察点。
        with _runtime_jobs_lock:
            runtime = _runtime_jobs.get(task_id)
            snapshot = get_task(task_id)
        if not snapshot:
            continue

        active = snapshot.get("status") in ACTIVE
        if active and cancel_active:
            # 返回前已确保持久化；worker 即使在此间终结，下面仍持有其句柄并等待。
            cancel_download(task_id)
        elif active:
            continue

        if runtime is not None:
            if runtime.started.is_set():
                waiting.append((task_id, runtime))
                continue
            # 从未开始执行的排队作业：取消意图已持久化，同步落为 cancelled
            # 终态后即可安全删除记录。worker 之后取到该作业时，_run_download
            # 顶部的「任务缺失/已取消/已终结」检查会让它空跑返回，不碰任何
            # 文件；不再为它等 10 秒 quiesce——worker 全忙时那是永远等不到的。
            latest = get_task(task_id)
            if latest and latest.get("status") in ACTIVE:
                if not cancel_active:
                    log.error("未启动任务无法安全取消，拒绝删除 task_id=%s",
                              task_id)
                    continue
                _finalize_cancelled(task_id)
            ready.append(task_id)
        elif active:
            # 兼容手工插入/异常遗留的活跃记录：取消意图已落盘，但没有可证明
            # quiesced 的句柄，因而完整保留记录与文件。
            log.error("活动任务没有 runtime 句柄，拒绝删除 task_id=%s", task_id)
        else:
            ready.append(task_id)

    wait_budget = DELETE_QUIESCE_TIMEOUT if timeout is None else max(0.0, float(timeout))
    deadline = time.monotonic() + wait_budget
    for task_id, runtime in waiting:
        remaining = max(0.0, deadline - time.monotonic())
        if not runtime.quiesced.wait(remaining):
            log.warning("等待下载线程静默超时，保留任务和文件 task_id=%s", task_id)
            continue
        latest = get_task(task_id)
        if latest and latest.get("status") not in ACTIVE:
            ready.append(task_id)
        else:
            log.error("worker 已静默但任务仍是活动态，保留记录 task_id=%s", task_id)
    return list(dict.fromkeys(ready))


def delete_tasks(task_ids: list, delete_file: bool, *, timeout: float = None) -> int:
    """同步持久化取消，等待所有关联 runtime 静默，再删除最新记录/manifest。"""
    wanted = list(dict.fromkeys(str(task_id) for task_id in task_ids))
    ready = _quiesce_task_ids(wanted, cancel_active=True, timeout=timeout)
    return _store_delete_tasks(ready, delete_file)


def delete_tasks_detailed(task_ids: list, delete_file: bool,
                          *, timeout: float = None) -> dict:
    """兼容删除编排之上追加部分成功与文件处理明细。"""
    wanted = list(dict.fromkeys(str(task_id) for task_id in task_ids))
    ready = _quiesce_task_ids(wanted, cancel_active=True, timeout=timeout)
    result = _store_delete_tasks_detailed(ready, delete_file)
    result["requested_count"] = len(wanted)
    result["retained_count"] = max(0, len(wanted) - int(result.get("count", 0)))
    result["partial"] = bool(
        result["retained_count"] or result.get("file_failed_count", 0))
    return result


def clear_tasks(delete_file: bool) -> int:
    """清除终态记录；终态若仍有 worker，也必须先越过 quiescence 屏障。"""
    candidates = [task["task_id"] for task in list_tasks()
                  if task.get("status") not in ACTIVE]
    ready = _quiesce_task_ids(candidates, cancel_active=False)
    latest_ready = [task_id for task_id in ready
                    if (get_task(task_id) or {}).get("status") not in ACTIVE]
    return _store_delete_tasks(latest_ready, delete_file)


def clean_invalid_records() -> dict:
    """仅把“可检查且确认缺失”的 done 视为失效，并在 artifact lock 内复核。"""
    candidates: list[str] = []
    for task in list_tasks():
        if task.get("status") == "failed":
            candidates.append(task["task_id"])
        elif task.get("status") == "done":
            inspectable, files = _task_files_state(task)
            if inspectable and not files:
                candidates.append(task["task_id"])
    ready = _quiesce_task_ids(candidates, cancel_active=False)
    return _store_delete_invalid_tasks(ready)
