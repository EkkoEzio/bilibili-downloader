"""任务状态存储:内存任务表 + tasks.json 持久化 + 记录级查询/删除。

任务表是全应用共享的可变状态(下载线程写进度、路由读快照、自动追更查重),
单独成模块让 downloader.py 专注下载执行。锁纪律:
- _tasks_lock 罩任务表本身;快照在锁内取,慢 IO(写盘/删文件)一律锁外。
- _persist_lock 独立写盘锁,防多线程并发写坏 tasks.json。

_final_paths 是下载引擎(postprocessor 钩子)与文件名对齐(files.py)之间的
交接状态:task_id -> 最终落盘路径。放这里让两个方向都单向依赖本模块。
"""
import threading
import time
from glob import escape as glob_escape
from pathlib import Path
from typing import Optional

import storage
from config import TASKS_FILE
import config
import extras

bvid_from_url = extras.bvid_from_url

# 任务状态表:task_id -> {status, progress, speed, eta, title, filepath, error, cancel}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()
# 写盘锁独立于任务锁:快照在任务锁内取,写盘(含 IO)不阻塞任务状态更新
_persist_lock = threading.Lock()

# postprocessor 钩子捕获的最终落盘路径(设计 A.3):task_id -> str
# MoveFiles 是最后一个后处理器,其 finished 载荷里的 filepath 即合并/搬运完成的最终文件
_final_paths: dict[str, str] = {}

# 任务状态机的两个集合,全项目唯一来源(曾有三份后端副本各自漏写 "retrying",
# 导致重试窗口内:重复下载拦截失效、「清除全部记录」把在跑的任务记录删掉、
# 重启后 retrying 变僵尸态)。前端 pages/downloads.js 的 TASK_ACTIVE 必须与
# ACTIVE 一致,tests/test_app.py 有一致性断言。
ACTIVE = ("queued", "downloading", "merging", "cancelling", "retrying")
TERMINAL = ("done", "failed", "cancelled", "interrupted")
_ACTIVE = ACTIVE  # 旧名保留,避免下游 import 断裂


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
            if t.get("status") in ACTIVE:
                t["status"] = "interrupted"
                t["error"] = "服务重启,任务中断"
            t["cancel"] = False
            _tasks[t["task_id"]] = t


# 模块加载时恢复历史(与旧 downloader.py 行为一致)
_load_persisted()


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
                    and t["status"] in ACTIVE):
                return tid
    return None


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


# 前缀匹配时允许的后缀起始字符。落盘名可以是「期望名 + ` - P3` / `.zh-Hans`
# / ` (1)`」这类后缀,但不允许期望名后面直接接别的字——否则任务「第01期」会
# 匹配上另一个视频「第01期特别版」的文件,delete_file=True 就删了邻居的文件。
_STEM_SUFFIX_STARTS = (" ", ".", "-", "_", "(", "[")


def _stem_belongs(stem: str, expected: str) -> bool:
    """磁盘文件的 stem 是否属于期望名 expected(精确相等,或带分隔符后缀)。"""
    if stem == expected:
        return True
    if not stem.startswith(expected):
        return False
    return stem[len(expected):].startswith(_STEM_SUFFIX_STARTS)


def _file_family(main: Path) -> list:
    """主文件 + 同 stem 的副文件(封面/字幕/弹幕)。

    删除与体积统计都应覆盖整族:只返回主文件会在「删除记录 + 文件」后留下
    孤儿 .xml/.srt/.jpg,downloaded_bytes 也会少算附件。
    """
    try:
        fam = sorted(f for f in main.parent.glob(glob_escape(main.stem) + ".*")
                     if f.is_file())
        return fam or ([main] if main.is_file() else [])
    except OSError:
        return [main] if main.is_file() else []


def _task_files(t: dict) -> list:
    """找任务对应的落盘文件(主文件 + 封面/字幕/弹幕等副文件)。

    优先用 A.3 写入的 actual_filename——权威路径,免扫描(也根治了
    「前缀截断错位 → Reveal 找不到文件」的老问题)。老任务没有这个字段时,
    按 build_filename 算出的期望名扫下载目录及一层子目录(SORT_BY_UP 归档)。

    匹配规则见 _stem_belongs。早先是拿**未清洗**的 title[:30] 做
    name.startswith,有两个后果:标题含 / : ? 等非法字符的任务永远匹配不上
    (磁盘上这些字符已被换成 _),以及任何共享前 30 字的两个视频互相误配——
    后者叠加 delete_file=True 会删掉邻居的文件。
    """
    af = t.get("actual_filename")
    if af:
        p = Path(af)
        if p.is_file():
            return _file_family(p)
    title = (t.get("title") or "").strip()
    if not title or title == "(解析中)":
        return []
    expected = extras.build_filename(title, int(t.get("page") or 1))
    try:
        dirs = [config.DOWNLOAD_DIR]
        dirs += [d for d in config.DOWNLOAD_DIR.iterdir() if d.is_dir()]
    except OSError:
        return []
    files = []
    for d in dirs:
        try:
            files += [f for f in d.iterdir()
                      if f.is_file() and _stem_belongs(f.stem, expected)]
        except OSError:
            continue
    return files


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


def compute_stats() -> dict:
    """统计面板数据:总下载数、总字节数、本月/今日 数。本月=自然月。

    历史 done 任务没有 downloaded_bytes 字段时,总字节用下载目录磁盘实测值兜底。
    """
    import datetime
    now = int(time.time())
    today = datetime.date.today()
    month_start = int(time.mktime(datetime.date(today.year, today.month, 1).timetuple()))
    day_start = now - 86400

    total_count = 0
    total_bytes = 0
    month_count = 0
    day_count = 0
    for t in list_tasks():
        if t.get("status") != "done":
            continue
        total_count += 1
        total_bytes += t.get("downloaded_bytes") or 0
        finished = t.get("finished_at")
        if finished:
            if finished >= day_start:
                day_count += 1
            if finished >= month_start:
                month_count += 1

    # 递归统计:开了「按 UP 建子目录」后文件都在子目录里,只扫顶层会算成 0
    try:
        disk_bytes = sum(f.stat().st_size
                         for f in config.DOWNLOAD_DIR.rglob("*") if f.is_file())
    except Exception:
        disk_bytes = 0

    return {
        "total_count": total_count,
        "total_bytes": total_bytes if total_bytes else disk_bytes,  # 用磁盘实测值兜底
        "month_count": month_count,
        "day_count": day_count,
        "disk_bytes": disk_bytes,
    }
