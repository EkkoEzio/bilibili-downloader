"""yt-dlp 封装:解析、下载、进度、取消。"""
import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from typing import Optional

import yt_dlp

from pathlib import Path
from glob import escape as glob_escape

import extras
import storage
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
# 写盘锁独立于任务锁:快照在任务锁内取,写盘(含 IO)不阻塞任务状态更新
_persist_lock = threading.Lock()
# 下载线程句柄:task_id -> threading.Thread
_threads: dict[str, dict] = {}


def _persist() -> None:
    """把当前所有任务快照写盘。去掉 cancel 标志,保留其余展示字段。
    storage.write_json 原子替换;独立写锁防多线程并发写盘
    (进度钩子/批量线程/取消删除路由都会调这里)。"""
    with _tasks_lock:
        snapshot = [{k: v for k, v in t.items() if k != "cancel"} for t in _tasks.values()]
    with _persist_lock:
        storage.write_json(TASKS_FILE, snapshot)


def _load_persisted() -> None:
    """启动时加载历史任务(进行中的统一标记为中断,避免僵尸态)。
    文件损坏由 storage.read_json 统一备份 .corrupt-<ts>,不静默清零。"""
    data = storage.read_json(TASKS_FILE, default=None)
    if not data:
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


def build_filename(title: str, page: int = 1) -> str:
    """任务权威 title → 期望的落盘文件名 stem(不含目录/扩展名)。

    outtmpl 组装(A.2)与下载后改名对齐(A.3)共用本函数,保证两端算出的
    期望名恒一致。多 P 加 ` - P{n}` 后缀。
    """
    stem = extras.sanitize_filename(title)
    return f"{stem} - P{int(page)}" if page and int(page) > 1 else stem


def _tmpl_literal(s: str) -> str:
    """把 Python 侧清洗好的字面段安全嵌入 yt-dlp outtmpl:% 是模板元字符,须转义成 %%。"""
    return s.replace("%", "%%")


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
        stem = _tmpl_literal(build_filename(title, page))
        name_tmpl = f"{stem}.%(ext)s"
        # UP 子目录也用清洗后的字面名;批量任务没透传 UP 名时退回 live 字段
        if config.SORT_BY_UP:
            sub = (f"{_tmpl_literal(extras.sanitize_filename(uploader))}/"
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


# postprocessor 钩子捕获的最终落盘路径(设计 A.3):task_id -> str
# MoveFiles 是最后一个后处理器,其 finished 载荷里的 filepath 即合并/搬运完成的最终文件
_final_paths: dict[str, str] = {}

# 主文件扩展名(用于从候选中挑出视频/音频本体,排除封面/字幕等副文件)
_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
               ".m4a", ".mp3", ".aac", ".opus", ".ogg"}


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


def _find_actual_file(task_id: str, expected_stem: str) -> Optional[Path]:
    """找 yt-dlp 实际落盘的主文件。

    优先 postprocessor 钩子捕获的最终路径(权威);没捕获到(旧流程/异常)
    再扫下载目录:stem 精确匹配 → 前缀匹配。返回 Path 或 None。
    """
    captured = _final_paths.get(task_id)
    if captured and Path(captured).is_file():
        return Path(captured)
    try:
        dirs = [config.DOWNLOAD_DIR] + [d for d in config.DOWNLOAD_DIR.iterdir() if d.is_dir()]
    except OSError:
        return None
    for d in dirs:
        try:
            files = [f for f in d.iterdir() if f.is_file()]
        except OSError:
            continue
        media = [f for f in files if f.suffix.lower() in _MEDIA_EXTS]
        exact = [f for f in media if f.stem == expected_stem]
        if exact:
            return max(exact, key=lambda f: f.stat().st_size)
        # 兜底:yt-dlp 对字面段做了意外变换时按前缀找(截短些避免长标题误配)
        prefix = [f for f in media if f.stem.startswith(expected_stem[:10])]
        if prefix:
            return max(prefix, key=lambda f: f.stat().st_size)
    return None


def _rename_with_sidecars(source: Path, new_stem: str) -> Path:
    """把 source 改名为 new_stem(保留各自扩展名),同 stem 的封面/字幕等
    副文件一并改名。返回主文件新路径;目标被占等异常向上抛。"""
    target = source.with_name(new_stem + source.suffix)
    for side in source.parent.glob(glob_escape(source.stem) + ".*"):
        if side in (source, target) or not side.is_file():
            continue
        side_target = side.with_name(new_stem + side.suffix)
        if not side_target.exists():
            side.rename(side_target)
    source.rename(target)
    return target


def _align_task_file(task_id: str) -> None:
    """下载成功后把磁盘文件名强制对齐任务 title(设计 A.3/A.4)。

    尽力而为:任何失败只写 rename_error/rename_conflict 等展示字段,
    绝不影响任务的「完成」状态。副文件(封面/字幕)随主文件一起改。
    """
    try:
        with _tasks_lock:
            t0 = _tasks.get(task_id)
            title = (t0.get("title") or "").strip() if t0 else ""
            page = int(t0.get("page") or 1) if t0 else 1
        if not title or title == "(解析中)":
            return
        expected_stem = build_filename(title, page)
        actual = _find_actual_file(task_id, expected_stem)
        if actual is None:
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["rename_error"] = "下载完成但没找到落盘文件"
            _persist()
            return
        if actual.stem == expected_stem:
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["actual_filename"] = str(actual)
            _persist()
            return
        target = actual.with_name(expected_stem + actual.suffix)
        if target.exists() and not _same_file(actual, target):
            # A.4:拒绝覆盖,由用户在 UI 上选(加后缀/打开现有/忽略)
            same_size = actual.stat().st_size == target.stat().st_size
            with _tasks_lock:
                t = _tasks.get(task_id)
                if t:
                    t["actual_filename"] = str(actual)
                    if same_size:
                        t["rename_skipped"] = "duplicate"
                    else:
                        t["rename_conflict"] = {"source": str(actual),
                                                "expected": str(target),
                                                "existing": str(target)}
            _persist()
            return
        new_path = _rename_with_sidecars(actual, expected_stem)
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t["actual_filename"] = str(new_path)
        _persist()
    except OSError as e:
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t["rename_error"] = str(e)
        _persist()
    finally:
        _final_paths.pop(task_id, None)


def _same_file(a: Path, b: Path) -> bool:
    """两个路径是否同一文件(大小写不敏感盘上仅大小写不同的名字也算)。"""
    try:
        return a.stat().st_ino == b.stat().st_ino
    except OSError:
        return False


def resolve_rename_conflict(task_id: str, action: str) -> dict:
    """用户处理改名冲突(设计 A.4)。action:
    - rename_with_index: 期望名 stem 后加 ` (1)`(递增到 (9) 封顶)改名
    - cancel: 放弃处理,清掉冲突标记
    - open_existing: 返回现有文件路径,由路由层在 Finder 定位
    """
    with _tasks_lock:
        t = _tasks.get(task_id)
        conflict = dict(t.get("rename_conflict") or {}) if t else {}
    if not conflict:
        return {"ok": False, "error": "该任务没有待处理的名称冲突"}

    if action == "cancel":
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t.pop("rename_conflict", None)
        _persist()
        return {"ok": True}

    if action == "open_existing":
        return {"ok": True, "path": conflict.get("existing", "")}

    if action == "rename_with_index":
        source = Path(conflict.get("source") or "")
        if not source.is_file():
            return {"ok": False, "error": "下载的文件已不在磁盘上,无法改名"}
        title = (t.get("title") or "").strip()
        base = build_filename(title, int(t.get("page") or 1))
        for i in range(1, 10):
            cand = source.with_name(f"{base} ({i}){source.suffix}")
            if cand.exists():
                continue
            try:
                new_path = _rename_with_sidecars(source, f"{base} ({i})")
            except OSError as e:
                return {"ok": False, "error": f"改名失败:{e}"}
            with _tasks_lock:
                t2 = _tasks.get(task_id)
                if t2:
                    t2.pop("rename_conflict", None)
                    t2["actual_filename"] = str(new_path)
            _persist()
            return {"ok": True, "actual_filename": str(new_path)}
        return {"ok": False, "error": "后缀已试到 (9) 仍冲突,请手动改名"}

    return {"ok": False, "error": f"未知操作:{action}"}


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
        _final_paths.pop(task_id, None)  # 上一次尝试捕获的路径作废
        opts = _ydl_opts(base_opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            # 成功:先把落盘文件名对齐任务 title(A.3),
            # 统计/弹幕/Finder 定位都用对齐后的权威路径
            _align_task_file(task_id)
            # 统计实际下载文件大小,用于 C3 统计面板
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
            "uploader_mid": uploader_mid or _to_mid(summary.get("uploader_mid")),
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
                "uploader_mid": _to_mid(item.get("uploader_mid")),
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
    """根据任务标题在下载目录找对应文件(yt-dlp 文件名以标题开头)。

    按 UP 归档(SORT_BY_UP)时文件在「UP名/」子目录,也扫一层子目录。
    新任务有 A.3 写入的 actual_filename 时直接命中,免前缀扫描
    (也根治了「前缀截断错位 → Reveal 找不到文件」的老问题)。
    """
    from pathlib import Path
    af = t.get("actual_filename")
    if af:
        p = Path(af)
        if p.is_file():
            return [p]
    title = (t.get("title") or "").strip()
    if not title:
        return []
    prefix = title[:30]  # CJK 标题原样保留,30字符(~90B)在截断范围内,足够唯一
    try:
        files = [f for f in config.DOWNLOAD_DIR.iterdir()
                 if f.is_file() and f.name.startswith(prefix)]
        for sub in config.DOWNLOAD_DIR.iterdir():
            if sub.is_dir():
                files += [f for f in sub.iterdir()
                          if f.is_file() and f.name.startswith(prefix)]
        return files
    except Exception:
        return []


def find_task_files(task_id: str) -> list:
    """按 task_id 找已下载文件(「在 Finder 中显示」用)。"""
    t = get_task(task_id)
    return _task_files(t) if t else []


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
            _final_paths.pop(tid, None)
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


def clean_invalid_records() -> dict:
    """删除「无效」任务记录:下载失败的 + 已完成但本地文件已不存在的(被删/改名/移动)。

    只删记录不删文件(失效任务本就没有可删文件)。磁盘检查放锁外,不阻塞下载线程。
    """
    with _tasks_lock:
        failed_ids = [tid for tid, t in _tasks.items() if t.get("status") == "failed"]
        done = [dict(t) for tid, t in _tasks.items() if t.get("status") == "done"]
    invalid_ids = [t["task_id"] for t in done if not _task_files(t)]
    removed = delete_tasks(failed_ids + invalid_ids, delete_file=False)
    return {"failed": len(failed_ids), "invalid": len(invalid_ids), "removed": removed}
