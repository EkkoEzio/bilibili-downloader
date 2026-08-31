"""下载落盘文件管理:期望文件名组装、下载后文件名对齐、改名冲突处理、文件定位。

命名权在 Python 侧(设计 A.2/A.3):outtmpl 组装与下载后改名共用 build_filename,
保证两端算出的期望名恒一致;落盘后再把磁盘文件强制对齐任务权威 title。
"""
import logging
import subprocess
import sys
from pathlib import Path
from glob import escape as glob_escape

import config
# build_filename 住在 extras(纯命名逻辑,依赖链底部;taskstore 定位文件也要用),
# 这里再导出,保持 files.build_filename / downloader.build_filename 调用方不变。
from extras import sanitize_filename, build_filename
# stem_belongs 是「磁盘文件是否属于某个期望名」的全项目唯一来源(住在 taskstore,
# 与 _task_files 的定位/删除共用)。这里 import 而不是在本文件里再写一份判据——
# 曾各写一份,files 侧那份更宽松,酿成邻居视频文件被误改名的不可逆损坏。
from taskstore import (_tasks, _tasks_lock, _persist, _final_paths,
                       stem_belongs)

log = logging.getLogger("bd.files")


# 主文件扩展名(用于从候选中挑出视频/音频本体,排除封面/字幕等副文件)
_MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
               ".m4a", ".mp3", ".aac", ".opus", ".ogg"}


def tmpl_literal(s: str) -> str:
    """把 Python 侧清洗好的字面段安全嵌入 yt-dlp outtmpl:% 是模板元字符,须转义成 %%。"""
    return s.replace("%", "%%")


def _same_file(a: Path, b: Path) -> bool:
    """两个路径是否同一文件(大小写不敏感盘上仅大小写不同的名字也算)。"""
    try:
        return a.stat().st_ino == b.stat().st_ino
    except OSError:
        return False


def _find_actual_file(task_id: str, expected_stem: str):
    """找 yt-dlp 实际落盘的主文件。

    优先 postprocessor 钩子捕获的最终路径(权威);没捕获到(旧流程/异常)
    再扫下载目录:stem 精确匹配 → stem 归属规则(stem_belongs)。返回 Path 或 None。
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
        # 精确同名单独一级,不与下面的 belongs 合并:belongs 的结果集本就包含
        # 精确同名者,合并后 max(size) 可能挑中体积更大的带后缀文件(如另一个
        # 分 P 的 `期望名 - P3.mp4`)而不是精确同名的那个。
        exact = [f for f in media if f.stem == expected_stem]
        if exact:
            return max(exact, key=lambda f: f.stat().st_size)
        # 兜底:yt-dlp 对字面段做了意外变换时,按 stem 归属规则找(期望名后只允许
        # 紧跟 ` ` / `.` / `-` / `_` / `(` / `[` 这类分隔符)。规则只有一份,在
        # taskstore,与 _task_files 的定位/删除共用。
        #
        # 旧写法是拿期望名的前 10 个字符做无边界前缀匹配(注释还写着「截短些避免
        # 长标题误配」,方向恰好相反:截短只会放宽匹配)。失败模式:与期望名共享前
        # 10 个字的**邻居视频**文件被认作本任务的落盘文件,align_task_file 随即把
        # 它连同同 stem 的封面/字幕/弹幕副文件一起改名成本任务的期望名——改名不是
        # 复制,邻居的原文件名永久丢失,两条记录指向同一个文件。期望名不足 10 字时
        # 切片等于整名,退化成整名前缀匹配(期望名「测试」命中「测试2」)。
        #
        # 代价(已知并接受):落盘名被截断、或第 11 字之后被替换这两类异常形态从
        # 「误改邻居文件」变成不命中 → 返回 None → align_task_file 写 rename_error。
        # 可见、可恢复、不影响任务的 done 状态,换掉不可逆的数据损坏。
        belongs = [f for f in media if stem_belongs(f.stem, expected_stem)]
        if belongs:
            return max(belongs, key=lambda f: f.stat().st_size)
    return None


def _rename_with_sidecars(source: Path, new_stem: str) -> Path:
    """把 source 改名为 new_stem(保留各自扩展名),同 stem 的封面/字幕等
    副文件一并改名。返回主文件新路径;目标被占等异常向上抛。

    主文件与副文件**刻意**用不同的取法,不要「统一」掉:

    - 主文件用 `.suffix`(只取最后一段)。落盘名若是 `视频.f137.mp4` 这类
      yt-dlp 中间产物形态,我们希望改名后得到干净的 `新名.mp4`,而不是
      `新名.f137.mp4`。
    - 副文件用「source.stem 之后的整条扩展名链」。字幕由 yt-dlp 按
      subtitleslangs 落盘,名字是 `<stem>.zh-Hans.srt` 这种**两段**扩展名;
      对 Path("视频.zh-Hans.srt"),.suffix 只得 `.srt`,`.zh-Hans` 被算进了
      stem。用 .suffix 有两个后果:(a) 语言标签丢失(`旧名.zh-Hans.srt` →
      `新名.srt`);(b) 两种语言都在时,两个副文件算出**同一个**目标名,第一个
      改名成功、第二个撞上下面的 exists() 守卫被跳过,永久留在旧 stem 下变成
      孤儿 —— 而 taskstore._file_family 按**新** stem glob 找整族,再也匹配不
      到它,「删除记录 + 文件」清不掉(磁盘永久残留),体积统计也少算。
    """
    target = source.with_name(new_stem + source.suffix)
    # 切片 side.name[len(source.stem):] 的正确性依赖下面这个 glob 模式:
    # 模式是 glob_escape(source.stem) + ".*",所以每个 side.name 必然以
    # source.stem 开头、且紧跟一个 ".",切片结果就是完整的 `.xxx[.yyy]` 链。
    # 改这行时必须连 glob 模式一起看,两者是一对。
    for side in source.parent.glob(glob_escape(source.stem) + ".*"):
        if side in (source, target) or not side.is_file():
            continue
        side_target = side.with_name(new_stem + side.name[len(source.stem):])
        # 守卫保留作防御:取整条扩展名链后,正常路径上两个副文件不再撞同一个
        # 目标名,这里不该再触发。
        if not side_target.exists():
            side.rename(side_target)
    source.rename(target)
    return target


def align_task_file(task_id: str) -> None:
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


# ---------- 在系统文件管理器中打开/定位 ----------
# 早先两处路由直接硬编码 macOS 的 `open -R`。subprocess 的 check=False 只忽略
# 返回码,命令不存在照样抛 FileNotFoundError——非 macOS 上这两个接口会 500
# (被 @api_errors 兜住,用户侧表现为"打开失败")。这里按平台选命令并吞掉
# OSError:能定位就定位,不能也绝不影响主流程。

def _run_quiet(argv: list[str]) -> bool:
    """跑一条 GUI 唤起命令。成功发起返回 True;命令不存在/无权限返回 False。

    不看返回码:Windows 的 explorer 即便成功也常返回非 0。
    """
    try:
        subprocess.run(argv, check=False)
        return True
    except OSError as e:
        log.warning("唤起文件管理器失败(%s):%s", " ".join(argv[:1]), e)
        return False


def reveal_path(path) -> bool:
    """在文件管理器中定位并选中 path。返回是否成功发起。

    Linux 没有通用的"选中某文件"协议,退化为打开其所在目录。
    """
    p = Path(path)
    if sys.platform == "darwin":
        return _run_quiet(["open", "-R", str(p)])
    if sys.platform == "win32":
        # /select,<path> 必须是同一个 argv 元素
        return _run_quiet(["explorer", f"/select,{p}"])
    return open_dir(p.parent if p.is_file() else p)


def open_dir(path) -> bool:
    """在文件管理器中打开目录。返回是否成功发起。"""
    p = str(path)
    if sys.platform == "darwin":
        return _run_quiet(["open", p])
    if sys.platform == "win32":
        return _run_quiet(["explorer", p])
    return _run_quiet(["xdg-open", p])
