# 下载文件名与 b 站官方标题对齐 — 设计

日期：2026-08-19
范围：b 站视频下载器 (`/Users/hushen/Desktop/b站视频下载`)

## 背景

下载器当前存在"页面上看到标题 A、下载下来本地文件名是 B"的问题。
具体表现：
- 在下载记录卡片上点击封面/Reveal 时，弹窗提示「没找到该视频的文件」，但磁盘文件夹里其实有文件。
- 「清除失败/失效记录」时按 `_task_files()` 判定失效，可能误删本应保留的记录。

根因探查（详见附录）：A/B 的成因主要有 4 个结构性因素叠加，不是单一 bug。

## 目标

让"任务记录的 title"与"磁盘文件名"在**新下载**与**重新解析**之后**完全一致**，
并且从根源上防止后续发生新的 A/B 漂移。

## 非目标（明确不做）

- **不动磁盘上既有的已下载文件**——存量文件保持原名。
- 不做启动时扫盘对账，不做"修复名称"按钮，不做迁移脚本。
- 不改 `_task_files()` 的失效检测逻辑（旧路径保留，用于真失效场景）。
- 不改 UI 大结构，只在下载记录卡片上加 2 个轻量状态提示（命名冲突 / 改名失败）。

## 设计（方案 A-精简）

### A.1 入队即锚定 title

- `start_download` / `start_batch` 在创建任务时，**必须**已经完成一次
  `parse_video` / `parse_part`（带 `skip_download`），并把 `summary["title"]` 写进
  任务字典的 `title` 字段。
- `_run_download` 不再在过程中二次调用 `parse_video` / `parse_part`。
  已经计算好的 `summary["title"]` 是权威值，下载过程中即便 yt-dlp 内部再次
  `extract_info`，**也不回写**到任务 title。
- 例外：`title` 为空（如解析异常、`info["title"]` 缺失）时，下载完成 hook
  会从 yt-dlp 的最终 `info_dict` 兜底取一次，写回任务 title。
- 任务字段新增 `actual_filename`（字符串，最终磁盘路径），
  由 A.3 在下载完成时写入。

### A.2 落盘命名权交回 Python 侧

- 新增 `extras.sanitize_filename(name: str) -> str`：
  - 替换 `\\/:*?"<>|` → `_`
  - 合并连续空白
  - 去首尾空白与 `.`
  - 兜底空字符串返回 `video`
  - Windows 保留名前缀加 `_`（CON / PRN / AUX / NUL / COM1..9 / LPT1..9）
- 新增 `downloader.build_filename(title, uploader, page) -> str`：
  - 走 `sanitize_filename` 双重过滤（title + uploader）
  - 多 P 时返回 `{title} - P{n}`
- `_base_opts` 的 `outtmpl`：
  - `SORT_BY_UP=True`：`{sanitized_uploader}/{sanitized_title}[ - P{n}].%(ext)s`
  - `SORT_BY_UP=False`：`{sanitized_title}[ - P{n}].%(ext)s`
  - 全部用 f-string 在 Python 侧组装好再传入，不再让 yt-dlp 用 `.200B` 字节截断。

### A.3 下载完成时强制对齐

`_progress_hook` 收到 `status == "finished"` 时执行：

1. 取 `t["title"]`、`uploader`、`page` 三个权威字段。
2. 调 `build_filename` 算出期望文件名 `expected`。
3. 用 `os.path.basename(d["filename"])` 取实际文件名 `actual`。
4. 若 `actual != expected`：
   - `actual` 是 yt-dlp 实际落盘的最终路径（来自 `d["filename"]`）。
   - 调 A.4 的同名冲突处理。
5. 同时把同目录下与原文件同 stem 的副文件
   （`.infojson` / `.description` / 封面 `.jpg`/`.webp` / 字幕 `.srt`/`.vtt` 等）
   一并改名——遍历 `parent.glob(actual_stem + ".*")`，逐个 rename。
6. 成功后写 `t["actual_filename"] = str(expected_path)`；
   失败写 `t["rename_error"] = "<reason>"`，
   不阻塞 UI、不影响下载"完成"状态。
7. 改名操作幂等：相同 `actual == expected` 直接返回 ok，不做事。

### A.4 同名冲突：拒绝覆盖、由用户选

A.3 改名时若目标路径已存在：

1. 取 `os.stat(target)` 与 `os.stat(source)` 比较 `st_ino` 和 `st_size`：
   - 同 inode + 同 size → 幂等，跳过。
   - 同 size 不同 inode → 跳过，写 `t["rename_skipped"] = "duplicate"`,
     UI 提示「已有同名同大小文件，未覆盖」。
   - 不同 size → 不改名，写
     `t["rename_conflict"] = {"expected": ..., "existing": ...}`，
     UI 卡片展示"名称冲突"提示 + 按钮「处理」。
2. 用户点「处理」→ 后端路由 `POST /api/task/<id>/resolve-conflict`，
   body `{action: "rename_with_index" | "cancel" | "open_existing"}`：
   - `rename_with_index`：在 `expected` 文件名 stem 后加 ` (1)`，递归尝试直到没冲突或达到上限（最多 ` (9)`），超过后报错。
   - `cancel`：清掉 `t["rename_conflict"]`，不做任何动作。
   - `open_existing`：macOS `open -R <existing>` 在 Finder 中定位现有文件。

### A.5 _sanitize 真正接线

把 `downloader._sanitize` 删除，由 `extras.sanitize_filename` 取代，
统一一处实现。A.2 / A.3 都走 `extras.sanitize_filename`。

## 数据流（成功路径）

```
用户点击下载
  ↓
start_download(url, label, page=1, uploader_mid=0)
  ↓ parse_video / parse_part (skip_download)
summary["title"] = "漫士沉思录 第 12 集"
  ↓
_tasks[task_id] = {title: "漫士沉思录 第 12 集", page: 1, uploader: "...", ...}
  ↓
_run_download(summary, ...)  # 不再二次解析
  ↓
yt-dlp 用 outtmpl="{sanitized_title}.%(ext)s" 落盘
  → 假设落盘成 "漫士沉思录 第12集.mp4"（yt-dlp sanitize 后）
  ↓
_progress_hook 收到 finished
  ↓
expected = build_filename(summary["title"], ...) → "漫士沉思录 第 12 集.mp4"
actual   = "漫士沉思录 第12集.mp4"
actual != expected → os.rename
  ↓
磁盘文件 = "漫士沉思录 第 12 集.mp4"
t["actual_filename"] = ".../漫士沉思录 第 12 集.mp4"
t["title"]           = "漫士沉思录 第 12 集"
```

## 数据流（冲突路径）

```
yt-dlp 落盘:  "漫士沉思录 第 12 集.mp4"
用户磁盘已有: "漫士沉思录 第 12 集.mp4" (用户手动改名/老文件)
os.rename → FileExistsError
  ↓
st_size 不一致 → 写 t["rename_conflict"]
UI 卡片展示 "名称冲突" + 「处理」按钮
  ↓
用户点「加 (1) 后缀并改名」
  ↓
expected_with_index = "漫士沉思录 第 12 集 (1).mp4"
递归到无冲突为止 → 改名成功
```

## UI 改动（最小化）

在下载记录卡片上：

- 已有 `title` 显示不变。
- 新增：当 `rename_conflict` 不为空时，标题下方一行：
  「名称冲突 · [处理]」
- 新增：当 `rename_error` 不为空时，标题下方一行：
  「自动改名失败：{reason}」
- 卡片右上角加一个「处理」按钮，调用 `resolveTaskConflict(task_id)`。

不引入新页面，不动侧边栏，不动其它卡片字段。

## 错误处理

| 场景 | 处理 |
|------|------|
| 解析异常，无 title | 任务 title 写 `(解析中)`，下载完成 hook 兜底取 yt-dlp final title |
| yt-dlp 落盘名与 build_filename 不一致但无冲突 | 改名成功，写 actual_filename |
| 同名同 inode | 幂等跳过 |
| 同名不同 inode 同 size | 跳过，写 rename_skipped="duplicate" |
| 同名不同 inode 不同 size | 不改名，写 rename_conflict，等用户处理 |
| rename 抛其它异常（权限等） | 写 rename_error，不阻塞 UI |
| 用户点击「加 (1)」递归超过 (9) | 返回 400，提示已达上限，建议手动改名 |

## 测试计划

新增单元测试 `tests/test_filename_alignment.py`：

1. `sanitize_filename`：
   - 替换特殊字符
   - 合并空白
   - `.` 结尾处理
   - 空字符串兜底
   - Windows 保留名
2. `build_filename`：
   - 单 P
   - 多 P 后缀 `- P{n}`
   - uploader 子目录
3. 模拟 `_progress_hook` 收到 finished：
   - actual == expected → 不做事
   - actual != expected 无冲突 → 改名成功，副文件一起改
   - actual != expected 同名冲突 → 写 rename_conflict
   - 同 inode → 幂等
4. `POST /api/task/<id>/resolve-conflict`：
   - action=rename_with_index → 加 (1) 改名
   - action=cancel → 清字段
   - action=open_existing → 调 open 命令（mock）

冒烟测试：
- 选一个 UP 一条新视频走完整下载流程。
- 检查磁盘文件名 = 任务 title。
- 检查 `t["actual_filename"]` 与磁盘一致。

## 实施步骤（概要）

1. 在 `extras.py` 新增 `sanitize_filename`。
2. 在 `downloader.py` 新增 `build_filename`。
3. 改 `_base_opts`：移除 `.200B`，改用 Python 侧组装好的 outtmpl。
4. 改 `_run_download`：移除内部二次 `parse_video/parse_part` 的 title 回写分支
   （保留 formats 缺失时的兜底，但 title 兜底只在为空时生效）。
5. 改 `_progress_hook`：A.3 / A.4 改名 + 冲突检测逻辑。
6. `app.py` 新增 `POST /api/task/<id>/resolve-conflict` 路由。
7. `templates/index.html`：
   - 卡片 DOM 增加两个 `<div class="task-warn">` 节点（冲突 / 改名失败）。
   - 增加 `resolveTaskConflict(task_id, action)` 函数 + 「处理」按钮。
8. 跑 `tests/test_filename_alignment.py` + 既有测试套件（应 100% 过）。
9. 冒烟：选一个新视频走完整下载。

## 风险

- yt-dlp 对 Python 侧组装好的 outtmpl 字符串可能再做一次
  `prepare_filename` 处理（special chars / 信息缺失 / 非法字符）。A.3 的改名
  步骤是兜底保险——即便 A.2 不完美，最终落盘文件名也会被 A.3 强制对齐到
  title。
- 改名失败时（权限、文件被占用、副文件找不到等），用户感知延迟——只在 UI
  卡片上提示，不影响"完成"状态。
- `_summarize` 与 `_run_download` 之间若 summary 字段缺失（如 formats 空），
  `_run_download` 仍可能重新解析。这种回退路径**必须保持原规则**：
  title 仅在原 title 为空时才覆盖，不能引入新的漂移来源。
- 多 P 视频的分 P 标题在批量场景下可能来自前端透传，与 yt-dlp 解析到的
  分 P 标题不一致。本次设计不修这条（前端透传保留），但 A.3 会把落盘名
  强制对齐到**任务 title**（即前端透传值），导致与 yt-dlp 解析值不同。
  需要在实施时确认这条边界——是否允许用户后续选择「以 yt-dlp 解析为准」。

## 附录：A/B 根因探查（参考）

1. **两次解析漂移**：任务 title 来自首次 `parse_video(skip_download)`，
   落盘文件名来自 yt-dlp 真正下载时的 `extract_info`。两次之间 UP 改标题
   或分 P 列表变化即不一致。→ A.1 修。
2. **截断不一致**：`_task_files()` 用 `title[:30]` 字符前缀匹配磁盘，
   yt-dlp 用 `.200B` 字节截断，CJK 下边界完全错位。→ 本次设计不改
   `_task_files`（存量兼容），但通过 A.3 让磁盘名字完全等于 title，
   后续即便有人调 `_task_files` 也不会因截断错位。
3. **`_sanitize` 未接线**：已定义的清洗函数从未调用。→ A.5 修。
4. **无启动对账**：磁盘与记录长期脱钩。→ 本次设计明确不做。