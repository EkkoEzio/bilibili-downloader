# 全局缺陷修复(app-wide-bug-fixes)Bugfix Design

## Overview

需求文档定位的 6 处缺陷分布在前端列表框架、落盘文件定位、已下载判定、自动检查并发控制、任务状态机与统计口径六个互不重叠的面上。它们没有共同的技术根因,但共享同一类**成因模式**:同一条规则在代码里有第二份实现,或某个维度(分 P、原始下标、终态集合、自然日)在规则里被漏掉。

因此本次修复的总策略是**收口而非新增**:每条缺陷都落到一处已存在的判定函数上,把漏掉的维度补进去,或把重复的副本替换成对唯一来源的引用。6 条修复合计触碰 4 个后端模块与 4 个前端模块,没有新增架构层,也没有新增接口(唯一的接口变化是 `/api/up-list/check` 的响应多两个如实字段)。

| 编号 | 修复面(唯一改动点) | 收口对象 |
|------|---------------------|----------|
| B1 | `static/js/lib.js` → `renderDefaultList()` 构造 pairs 处 | 「勾选下标 = `state.items` 原始下标」的约定 |
| B2 | `files.py` → `_find_actual_file()` 兜底分支 | `taskstore.stem_belongs`(stem 归属规则唯一来源) |
| B3 | `taskstore.is_downloaded()` + `static/js/pages/downloads.js` 的已下载集合 | 新增 `extras.dl_key` / `extras.page_from_url`(键规则唯一来源) |
| B4 | `auto_refresh.refresh_once()` 的锁获取方式 + 路由响应 + 按钮反馈 | 「同一时刻只有一轮检查」的不变量 |
| B5 | `downloader.cancel_download()` / `_finalize_cancelled()` | `taskstore.TERMINAL`(终态集合唯一来源) |
| B6 | `taskstore.compute_stats()` 的 `day_start` | 与同函数 `month_start` 相同的自然周期口径 |

B1/B2 造成不可逆数据损失,优先级最高;B3 让「合集 / 多 P 下载」主场景不可用;B4 触碰 `docs/COMPLIANCE.md` 的对外承诺;B5/B6 是既有单一来源纪律与统计口径的收口。

现有 183 个 pytest 用例全过,说明这 6 条都在测试盲区里。设计中每条缺陷都给出可直接落成 pytest 断言的验证点,并明确标注**新增**与**需修改的既有测试**——其中有两条既有测试会被修复反转或变得不稳定,必须同步改,详见「Testing Strategy」。

---

## Glossary

### 通用术语

- **Bug_Condition (C)**:触发缺陷的输入条件。每条缺陷在「Bug Details」里给出形式化定义 `isBugCondition_Bn(X)`。
- **Property (P)**:缺陷输入下期望的正确行为,见「Correctness Properties」。
- **Preservation(保持)**:非缺陷输入(`¬C(X)`)上修复前后行为必须逐位相同。
- **F / F'**:修复前 / 修复后的行为。
- **单一来源(single source)**:同一条规则在全项目只有一份实现,其它位置一律引用它。本项目已有先例:`taskstore.ACTIVE` / `TERMINAL`、`extras.build_filename`、`extras.sanitize_filename`、`quality.js` 的 `QUALITY_OPTIONS`。

### 本次涉及的代码实体

| 实体 | 位置 | 说明 |
|------|------|------|
| `state.items` | `static/js/lib.js` | 当前列表页的完整数据数组;所有勾选下标都以它为索引基准 |
| `state.checked` | `static/js/lib.js` | 勾选集合,存的是 `state.items` 的**原始下标** |
| `renderDefaultList()` | `static/js/lib.js` | 标准视频卡列表渲染;6 个页面共用(downloads / history / feed(bili 源) / fav(夹内) / seasons(集内) / later) |
| `renderLibList(pairs, …)` | `static/js/lib.js` | 消费 `pairs: [{it, idx}]`,把 `idx` 写进 `data-libidx` 与 `toggleLibCheck(idx, …)` |
| `stem_belongs(stem, expected)` | `taskstore.py` | 磁盘文件 stem 是否属于期望名(精确相等,或期望名后紧跟分隔符 `_STEM_SUFFIX_STARTS`)。本次由 `_stem_belongs` 提升为公开名 |
| `_find_actual_file(task_id, expected_stem)` | `files.py` | 找 yt-dlp 实际落盘的主文件:先用 pp 钩子捕获的权威路径,没捕获到再扫盘 |
| `align_task_file(task_id)` | `files.py` | 下载成功后把磁盘文件名对齐任务权威 title;尽力而为,任何失败只写展示字段 |
| `is_downloaded(url)` | `taskstore.py`(经 `downloader` 再导出) | 「已下载」判定,列表角标与批量跳过共用 |
| `dl_key(bvid, page)` | `extras.py`(新增) | 已下载判定的唯一键规则 |
| `page_from_url(url)` | `extras.py`(新增) | 从视频 URL 解析分 P 序号;无 `?p=` 视为 1 |
| `refresh_once()` | `auto_refresh.py` | 跑一轮自动检查;串行 + UP 间隔 ≥5 秒 |
| `TERMINAL` | `taskstore.py` | 终态集合 `("done", "failed", "cancelled", "interrupted")` |
| `compute_stats()` | `taskstore.py` | 统计面板数据:总数 / 总字节 / 本月 / 今日 |

---

## 全局约束(适用于全部 6 项修复)

以下约束对每一条修复都成立,实现与评审时逐条核对。

### 语言

文档与代码注释使用中文。

### 单一来源纪律

不得为同一规则产生第二份实现。本次涉及的 5 条规则及其唯一来源:

| 规则 | 唯一来源 | 引用方 |
|------|----------|--------|
| 任务状态集合 | `taskstore.ACTIVE` / `taskstore.TERMINAL` | `downloader`、`app.py`、前端 `TASK_ACTIVE` / SSE 收流集合 |
| stem 归属判定 | `taskstore.stem_belongs` | `taskstore._task_files`、`files._find_actual_file` |
| 文件名组装 | `extras.build_filename`(内部用 `extras.sanitize_filename`) | `downloader._base_opts`(outtmpl)、`files.align_task_file`、`taskstore._task_files` |
| 清晰度选项 | 后端 `up_list.QUALITY_CHOICES` / 前端 `quality.js` 的 `QUALITY_OPTIONS` | 各处下拉;已有 `tests/test_up_list.py` 的一致性断言 |
| 已下载判定的键规则 | `extras.dl_key` + `extras.page_from_url`(后端)/ `util.js` 的 `dlKey` + `pageFromUrl`(前端) | `taskstore.is_downloaded`、`downloads.js` 的已下载集合与 `isDownloaded()` |

前后端天然是两份运行时,无法共享代码;纪律的落地方式是**把分隔符常量提到两侧各自的唯一来源里,再用测试把两个常量绑定比较**(沿用 `TASK_ACTIVE` ↔ `taskstore.ACTIVE` 的既有做法)。

### 锁纪律

- `_tasks_lock` 只罩任务表本身;写盘 / 删文件 / 网络等慢 IO 一律放锁外。
- `_persist()` 内部会抢 `_tasks_lock`,**禁止持 `_tasks_lock` 调用它**。
- 本次改动对锁的影响:
  - B3 `is_downloaded`:循环体内只做纯字符串运算(`bvid_from_url` / `dl_key`),与现状同级,不引入 IO,不调 `_persist()`。
  - B5 `cancel_download`:状态判定与置位在锁内,`_persist()` 在锁外(现状已如此,不变)。
  - B6 `compute_stats`:`list_tasks()` 锁内取快照,累加与 `rglob` 磁盘统计在锁外(现状已如此,不变)。
  - B4 `auto_refresh._lock` 与 `_tasks_lock` 无关。锁序为 `_lock` → `_tasks_lock`(`refresh_once` 内调 `downloader.is_downloaded`),单向;`taskstore` 不认识 `auto_refresh`,不存在反向路径,无死锁风险。
  - B1 / B2 不涉及锁(前端 / 纯磁盘扫描,`align_task_file` 已在锁外调 `_find_actual_file`)。

### 测试隔离

`tests/conftest.py` 的 autouse fixture 已把 `taskstore.TASKS_FILE`、`pagecache.CACHE_DIR` 重定向到 `tmp_path`,并给每个用例一张干净的任务表。新增测试:

- 不得触碰真实 `data/`;
- 需要下载目录的用例必须 `monkeypatch.setattr(<mod>.config, "DOWNLOAD_DIR", tmp_path)`(沿用 `tests/test_filename_alignment.py` 的既有写法);
- 直接往 `downloader._tasks` 塞任务的用例沿用 `_mk_task(...)` + `finally: pop` 的既有写法。

### 前端行为的锁定方式

前端没有 JS 测试运行时,行为靠 `tests/test_app.py` 的 `_tpl()`(`index.html` + `main.css` + `static/js/**/*.js` 拼接串)做子串断言来锁,必要时用 regex 从 JS 源码里提取常量与后端常量比较。

### 变更记录

6 条修复合并为一条 CHANGELOG 条目,写在 `CHANGELOG.md` 的 `## [未发布]` 小节下(`tests/test_docs.py` 要求 `[未发布]` 占位在所有已发布版本之前,该结构不变)。历史小节里对 `_stem_belongs` 等旧标识符的引用**不改动**——那是当时的事实记录,重命名会篡改历史;改名一事在新条目里说明。

---

## Bug Details

### B1 搜索过滤后勾选下标错位

#### Bug Condition

`renderDefaultList()` 构造 `pairs` 时用的是**遍历数组内的位置**,而不是条目在 `state.items` 里的原始下标:

```js
const source = q ? filtered : state.items.slice(0, state.shown);
const pairs = [];
source.forEach((it, idx) => pairs.push({ it, idx }));
```

无搜索词时 `source` 是从 0 起的前缀切片,位置恰好等于原始下标(巧合成立);有搜索词时 `source` 是 `filtered`,`idx` 是「第几个命中」,与 `state.items` 的下标无任何关系。而下游全部按「原始下标索引 `state.items`」解读这个值。

**形式化规格:**

```
FUNCTION isBugCondition_B1(input)
  INPUT: input = (items, query, visibleItem)   // 一次列表渲染中的某个可见条目
  OUTPUT: boolean

  RETURN input.query ≠ ""
     AND filter(input.items, input.query) ≠ input.items
END FUNCTION
```

#### Examples

设 `state.items = [A, B, C, D]`,搜索词命中 `[C, D]`:

- 勾选可见的 C(它是第 0 个命中)→ 发出 `data-libidx="0"` → `delSelected()` 取 `state.items[0]` = **A** → 删除 A 的记录并 `unlink` A 的磁盘文件,C 完好无损。用户看到"我删的那条还在,另一条不见了",且文件不可恢复。
- 在追更合集 / 收藏夹 / 稍后再看等页勾选可见的 D(第 1 个命中)→ `libDownload()` 取 `state.items[1]` = **B** → 提交下载 B。
- 先勾选 A(`state.checked = {0}`),再输入搜索词命中 `[C, D]` → C 拿到 `origIdx = 0` → `state.checked.has(0)` 为真 → **C 被高亮成已勾选**,而真实选择是 A。
- 无搜索词时勾选任意条目 → `source` 是前缀切片,`idx` == 原始下标 → 正确(这正是缺陷长期未被发现的原因:主路径是对的)。

---

### B2 落盘文件兜底定位用 10 字前缀匹配

#### Bug Condition

`files._find_actual_file()` 的扫盘兜底分支:

```python
# 兜底:yt-dlp 对字面段做了意外变换时按前缀找(截短些避免长标题误配)
prefix = [f for f in media if f.stem.startswith(expected_stem[:10])]
```

`expected_stem[:10]` 把归属判定退化成「共享前 10 个字符即认作同一任务」,且期望名短于 10 字时切片等于整名,退化为无边界的整名前缀匹配。命中后 `align_task_file()` 会把该文件**连同同 stem 的副文件一起改名**成本任务的期望名。

**形式化规格:**

```
FUNCTION isBugCondition_B2(input)
  INPUT: input = (expectedStem, diskStem)
  OUTPUT: boolean

  RETURN capturedFinalPath IS NONE            // pp 钩子未捕获到权威路径
     AND diskStem ≠ input.expectedStem        // 非精确匹配
     AND diskStem.startswith(input.expectedStem[:10])
END FUNCTION
```

#### Examples

- 期望名 `第01期开学第一课`(≥10 字),盘上另有一个无关视频 `第01期开学第一课(下)特别版.mp4`。前 10 字相同 → 被认作本任务的落盘文件 → 改名成 `第01期开学第一课.mp4`,连它的 `.jpg` / `.srt` / `.xml` 副文件一起改。**另一个视频的文件名永久丢失**,两个视频的记录都指向同一个文件。
- 期望名 `测试`(<10 字),盘上有 `测试2.mp4`(另一个视频)。`expected_stem[:10]` == `测试` → `测试2` 前缀命中 → 误认误改。
- 期望名 `标题`,盘上 `标题 [下载].mp4`(yt-dlp 追加了后缀)→ 应当命中(余部以空格开头,属合法后缀形态),这是**必须保持**的行为,见 3.9。
- pp 钩子已捕获权威路径且文件存在 → 根本不扫盘,与本缺陷无关,见 3.7。

---

### B3 「已下载」判定只看 BV 号,多 P 视频被误跳过

#### Bug Condition

后端 `taskstore.is_downloaded()` 与前端 `downloads.js` 的 `_doneBvids` 都只用 BV 号做判定,分 P 维度被丢弃——尽管两侧都拿得到:任务侧有 `t["page"]`,查询侧 URL 形如 `https://www.bilibili.com/video/{BV}?p={N}`。

```python
# taskstore.is_downloaded
if t.get("status") == "done" and bvid_from_url(t.get("url") or "") == bv:
    return True
```

```js
// downloads.js
_doneBvids = new Set(tasks.filter(t => t.status === 'done')
  .map(t => bvFromUrl(t.url)).filter(Boolean));
```

**形式化规格:**

```
FUNCTION isBugCondition_B3(input)
  INPUT: input = (url, tasks)
  OUTPUT: boolean

  // 同 BV 有已完成任务,但分 P 不同;且本分 P 自己没有完成任务
  RETURN (∃ t ∈ input.tasks : t.status = "done"
                          AND bvid(t.url) = bvid(input.url)
                          AND pageOf(t) ≠ pageOf(input.url))
     AND (¬∃ t ∈ input.tasks : t.status = "done"
                           AND bvid(t.url) = bvid(input.url)
                           AND pageOf(t) = pageOf(input.url))
END FUNCTION
```

其中 `pageOf(t)` 取任务字段 `t["page"]`(缺失兜底 1),`pageOf(url)` 从 URL 的 `?p=` 解析(无则 1)。**两侧不对称是必须的**,原因见「Fix Implementation」。

#### Examples

- 多 P 视频 `BV1abc`(共 5 P),已下载 P1。在新建下载抽屉粘贴该 URL → 解析出 5 个 `?p=N` 条目 → 全部挂「已下载」角标 → 勾选 P2–P5 点批量下载 → 提示「所选项均已下载过」,一条都不入队。
- 同上,已下载的是 P3。浏览 P1 → 也被标成已下载。
- 单 P 视频 URL(不带 `?p=`)且其 BV 已有完成任务 → 应判已下载,**必须保持**,见 3.12。
- 判定的 (BV, 分 P) 与某完成任务完全相同 → 应判已下载,**必须保持**,见 3.13。
- URL 里提取不到 BV(短链 / av 号)→ 应判未下载,**必须保持**,见 3.14。
- 任务状态不是 `done` → 不计入,**必须保持**,见 3.15。

---

### B4 手动「立即检查新视频」会排队叠加多轮

#### Bug Condition

`auto_refresh.refresh_once()` 用阻塞的 `with _lock:`。锁保证了「不并发」,但没保证「不排队」——后来者阻塞在锁上,前一轮释放后完整再跑一轮。同时路由无条件返回 `{"ok": True}`,前端无从区分。

```python
def refresh_once() -> int:
    with _lock:            # 阻塞获取:后来者排队而不是跳过
        ...
```

```python
# app.py /api/up-list/check
threading.Thread(target=_run, daemon=True, name="bd-manual-check").start()
return jsonify({"ok": True})     # 无论是否真的启动了新一轮,响应完全相同
```

**形式化规格:**

```
FUNCTION isBugCondition_B4(input)
  INPUT: input = 一次检查触发(手动点击或后台轮询到点)
  OUTPUT: boolean

  RETURN 已有一轮 refresh 正在运行
END FUNCTION
```

#### Examples

- 连点「立即检查新视频」5 次 → 5 个后台线程依次抢锁 → 串行跑 5 轮完整检查。每轮对每个 UP 调一次 `arc/search`,UP 之间 sleep 5 秒;30 个 UP 的清单 = 5 × 30 = 150 次接口调用,持续约 12 分钟。这正是 `docs/COMPLIANCE.md` 承诺要避免的账号级风控场景。
- 后台 30 分钟轮询到点时手动检查正在跑 → 轮询那一轮也排队叠加。
- 检查进行中再点一次 → 响应与「已成功启动新一轮」完全一致 → 前端只能固定禁用按钮 30 秒装样子(而真实一轮可能要几十秒到几分钟,窗口既不准也不对)。
- 没有检查在运行时触发 → 应在后台起一轮并立即返回,**必须保持**,见 3.16。

---

### B5 终态集合硬编码副本漏 interrupted

#### Bug Condition

`downloader.py` 里有两份硬编码的终态元组,都漏了 `interrupted`:

```python
def cancel_download(task_id: str) -> bool:
    ...
    if not t or t["status"] in ("done", "failed", "cancelled"):   # 漏 interrupted
        return False
    t["cancel"] = True
    t["status"] = "cancelling"
```

```python
def _finalize_cancelled(task_id: str) -> None:
    ...
    if t and t["status"] not in ("done", "failed", "cancelled"):  # 漏 interrupted
        t["status"] = "cancelled"
```

v2.1 已把状态集合收敛到 `taskstore.ACTIVE` / `TERMINAL` 唯一来源,这两处漏改。

**形式化规格:**

```
FUNCTION isBugCondition_B5(input)
  INPUT: input = task
  OUTPUT: boolean

  // 终态集合的硬编码副本漏掉的那个状态
  RETURN input.status ∈ TERMINAL
     AND input.status ∉ ("done", "failed", "cancelled")
END FUNCTION
```

即 `input.status = "interrupted"`。`interrupted` 由 `_load_persisted()` 在启动时写入:上次进程被杀时处于活跃态的任务统一标记为中断。

#### Examples

- 服务重启后,一条 `interrupted` 任务出现在下载记录页。任务卡因 `interrupted ∉ TASK_ACTIVE` 不显示「取消」按钮,但接口可达(SSE 重连、脚本、旧页面缓存)。一旦调用 `/api/cancel/<id>`:状态被改成 `cancelling`(活跃态)并返回成功,而没有任何下载线程会去收尾 → **永久僵尸态**。
- 上述僵尸任务的下游后果:同 URL + 清晰度 + 分 P 的新下载请求被 `find_active_by_url` 判为「正在下载中」返回 409;「清除全部记录」把它当活跃任务不清除。用户既下不了这个视频,也删不掉这条记录。
- 对 `queued` / `downloading` / `merging` / `retrying` 调取消 → 应置 `cancelling` 并由下载线程收尾,**必须保持**,见 3.21。
- 对 `done` / `failed` / `cancelled` 调取消 → 应返回失败且状态不变,**必须保持**,见 3.22。

---

### B6 统计「今日」按滚动 24 小时

#### Bug Condition

`taskstore.compute_stats()` 里两个周期起点用了两套口径:

```python
month_start = int(time.mktime(datetime.date(today.year, today.month, 1).timetuple()))  # 自然月
day_start = now - 86400                                                                # 滚动 24 小时
```

**形式化规格:**

```
FUNCTION isBugCondition_B6(input)
  INPUT: input = finished_at (Unix 秒)
  OUTPUT: boolean

  RETURN input < startOfToday() AND input ≥ now() − 86400
END FUNCTION
```

`startOfToday()` 指本地时区的自然日 00:00。

#### Examples

- 昨晚 23:00 完成的任务,今早 08:00 打开统计:`23:00 ≥ 08:00 − 24h` 成立 → 被算进「今日」。用户看到的「今日 3」里有 2 条是昨天的。
- 同一份统计里「本月」严格按自然月(上月末完成的不计入),两个口径自相矛盾。
- 今天完成的任务 → 应计入「今日」,**必须保持**,见 3.26。
- 上月末完成的任务 → 不计入「本月」,**必须保持**,见 3.25。

---

## Expected Behavior

### Preservation Requirements

修复的每一处都必须让非缺陷输入上的行为逐位不变。下面按缺陷列出「必须保持不变的行为」与「不受影响的输入范围」。

#### B1 勾选与批量操作

**Unchanged Behaviors:**
- 无搜索词时勾选下载记录并「删除记录 + 文件」→ 精确删除所勾选条目的记录与文件(3.1)。
- 无搜索词时在内容页勾选并批量下载 → 提交所勾选的视频(3.2)。
- 「全选」只作用于当前可见(未被搜索词过滤掉)的条目(3.3)。
- 点击封面图切换勾选 → 正确切换该条目状态并同步底部操作条计数(3.4)。
- 快数据源后台拉到新数据且用户已有勾选 → 放弃静默替换,退回「查看最新」提示条(3.5)。
- 下载记录页在有勾选期间收到 SSE 进度或轮询结果 → 推迟整体重拉,勾选清零后补上(3.6)。

**Scope:**
搜索词为空的一切渲染路径完全不受影响——此时 `pairs` 的下标本来就等于原始下标,修复后取值逐位相同。`advanceDefaultChunk()`(无限滚推进)只在搜索词为空时被调用(`maybeLoadMore` / `maybeFillScreen` 都对非空 query 提前返回),其构造的 `pairs` 同样不变。

#### B2 落盘文件定位

**Unchanged Behaviors:**
- pp 钩子已捕获最终落盘路径且该文件存在 → 优先使用该权威路径,不扫盘(3.7)。
- 磁盘文件名与期望名精确相等 → 命中该文件(3.8)。
- 磁盘文件名是期望名加分隔符后缀(分 P 的 ` - P3`、字幕的 `.zh-Hans`、改名冲突的 ` (1)`)→ 认定属于本任务(3.9)。
- 同 stem 的封面 / 字幕 / 弹幕副文件在改名与删除时被覆盖为整族,不留孤儿(3.10)。
- 期望名对应的目标文件已存在且与实际文件不是同一文件 → 拒绝覆盖,按同大小 / 不同大小分别写 `rename_skipped` / `rename_conflict`(3.11)。

**Scope:**
`_find_actual_file()` 的前两级(`_final_paths` 权威路径、`stem == expected_stem` 精确匹配)一字不改,只替换第三级兜底分支的谓词。`_rename_with_sidecars`、`align_task_file` 的冲突处理分支、`taskstore._task_files` / `_file_family` 全部不动。

#### B3 已下载判定

**Unchanged Behaviors:**
- 不带 `?p=` 的单 P 视频 URL 且其 BV 已有完成任务 → 判为已下载(3.12)。
- 判定的 (BV, 分 P) 与某完成任务完全相同 → 判为已下载并在批量提交时跳过(3.13)。
- URL 中提取不到 BV(短链 / av 号)→ 判为未下载(3.14)。
- 任务只有 `done` 之外的状态 → 不计入「已下载」(3.15)。

**Scope:**
所有「BV 级 URL(无 `?p=`)+ 任务 page 为 1」的组合,新旧判定结果完全相同——这覆盖了绝大多数单 P 视频场景。后端重复下载拦截 `find_active_by_url(url, label, page)` 本来就按 (URL, 清晰度, 分 P) 判定,不在本次改动范围。

#### B4 自动检查

**Unchanged Behaviors:**
- 没有检查正在运行时触发手动检查 → 后台线程起一轮并立即返回,不阻塞前端(3.16)。
- 一轮检查内串行处理多个 UP,UP 之间保持不少于 5 秒的间隔(3.17)。
- 某个 UP 本轮拉取返回空(疑似限流)→ 不推进该 UP 的已见标记,留给下一轮重试(3.18)。
- 后台轮询线程按配置的间隔(不低于既有下限)周期性触发检查(3.19)。
- 一轮检查中某个 UP 处理抛异常 → 记录日志并继续处理后续 UP,不中断整轮(3.20)。

**Scope:**
`refresh_once()` 的**函数体**一字不改(UP 遍历、间隔 sleep、限流不推进、异常兜底、返回入队条数),只替换锁的获取方式。`fetch_new` / `estimate_ps` / `start_loop` 的间隔计算全部不动。返回值类型仍是 `int`,两个既有调用方无需改动。

#### B5 任务状态机

**Unchanged Behaviors:**
- 对 `queued` / `downloading` / `merging` / `retrying` 调取消 → 置为 `cancelling` 并由下载线程收尾为 `cancelled`(3.21)。
- 对 `done` / `failed` / `cancelled` 调取消 → 返回失败且不改变任务状态(3.22)。
- 下载线程收尾被取消的任务 → 只在任务尚未处于终态时才落为 `cancelled`,已终结的状态不被覆盖(3.23)。
- 前端渲染任务卡与角标使用与后端活跃态集合一致的状态列表(3.24)。

**Scope:**
`("done", "failed", "cancelled")` 是 `TERMINAL` 的真子集,唯一差集是 `interrupted`。因此对这三个状态以及全部活跃态,新旧判定结果完全相同——`interrupted` 是唯一行为发生变化的输入。

#### B6 统计口径

**Unchanged Behaviors:**
- 「本月」按自然月计算,上月末完成的任务不计入本月(3.25)。
- 今天完成的任务计入「今日」(3.26)。
- 总下载数 / 总字节数只统计 `done` 任务,并在历史记录缺 `downloaded_bytes` 时用下载目录磁盘实测值(递归)兜底(3.27)。

**Scope:**
只改 `day_start` 一行(外加删除随之失去用途的 `now` 变量)。`month_start`、`total_count`、`total_bytes`、`disk_bytes` 的计算与返回结构全部不变。`finished_at ≥ startOfToday()` 的任务(即真正在今天完成的)新旧结果相同。

---

## Hypothesized Root Cause

### B1

根因唯一且已定位到具体表达式:`renderDefaultList()` 用 `Array.prototype.forEach` 的第二个参数当下标,而该参数是**遍历数组内的位置**,不是条目在 `state.items` 里的位置。

```js
const source = q ? filtered : state.items.slice(0, state.shown);
source.forEach((it, idx) => pairs.push({ it, idx }));
```

- 无搜索词分支:`source` 是 `state.items.slice(0, n)`,从 0 起的前缀切片 → 位置 == 原始下标。**巧合正确**。
- 有搜索词分支:`source` 是 `filtered`,位置 == 「第几个命中」。**语义错位**。

错位为什么会造成删错文件,取决于下游的解读约定。核对全部消费方,它们**一致地**把这个值当原始下标用:

| 消费方 | 代码 | 解读 |
|--------|------|------|
| `renderLibList` | `data-libidx="${origIdx}"`、`onchange="toggleLibCheck(${origIdx}, …)"` | 原始下标 |
| `renderLibList` 高亮 | `state.checked.has(origIdx)` | 原始下标 |
| `lib.toggleLibCheck` | `state.checked.add(origIdx)` | 原始下标 |
| `lib.libToggleAll` | `state.checked.add(parseInt(cb.dataset.libidx))` | 原始下标 |
| `downloads.delSelected` | `state.items[parseInt(cb.dataset.libidx)].task_id` | 原始下标 |
| `downloads.libDownload` | `state.items[parseInt(cb.dataset.libidx)]` | 原始下标 |

**结论:约定是一致的,只有生产者一处违约。** 因此修复面只有一处——`renderDefaultList()` 构造 pairs 的地方;`renderLibList()` 的签名已经是 `pairs: [{it, idx}]` 形态,`delSelected()` / `libDownload()` 的语义本来就正确,不需要改。

`advanceDefaultChunk()` 也构造 pairs,但用的是 `state.items.slice(0, state.shown).map((it, idx) => …)`——同样是从 0 起的前缀切片,已经正确。它也只在搜索词为空时被调用。

影响面 6 个页面(downloads / history / feed(bili 源) / fav(夹内) / seasons(集内) / later)全部复用 `renderDefaultList`,一处修好即全部生效。

### B2

根因是**同一条规则的第二份实现漏改**。v2.1 已经发现「前缀匹配会误配邻居视频」并在 `taskstore._task_files` 侧收紧为 `_stem_belongs`(完整期望名 + 分隔符边界),但 `files._find_actual_file` 的兜底分支保留了旧的前缀写法,而且更宽松(只取前 10 字)。

`expected_stem[:10]` 这个切片的动机(注释写着「截短些避免长标题误配」)本身是自相矛盾的:截短只会**放宽**匹配、增加误配。它真正想解决的是「yt-dlp 对字面段做了意外变换」这类异常落盘名,但用截断前缀去容错,代价是把归属判定的边界完全放弃。

短名场景(1.5)是同一根因的退化形态:`expected_stem` 短于 10 字时切片等于整名,`startswith` 没有边界检查 → 「测试」命中「测试2」。

### B3

根因是**判定粒度少了一个维度**。任务表里 `page` 字段一直存在(`start_download` / `start_batch` 都写),URL 里 `?p=` 一直存在(`parse_part` 生成、`resolve_playlist` 产出),但两端的判定都只用 BV。

值得单独指出的是**两侧取 page 的方式必须不对称**,这是实现时最容易搞错的地方:

- **任务侧**必须读 `t["page"]`,不能从 `t["url"]` 解析。因为 `start_download(url, label, page=3)` 存进任务表的 `url` 是用户粘贴的原始 URL(通常**不带** `?p=`),而 `page` 是 3。若从任务 URL 解析会得到 1,判定全错。
- **查询侧**只能从 URL 解析。因为 `is_downloaded(url)` / `isDownloaded(url)` 的入参只有 URL。

第二个根因是**前后端各写一份键规则**。哪怕两边都补上 page,只要键的拼法不一致(例如后端 `f"{bv}:{page}"`、前端 `bv + '#p' + page`),集合就对不上,角标与批量跳过会各说各话——而这种漂移不会被任何现有测试发现。

### B4

根因是**用「互斥」的工具去实现「去重」的语义**。`threading.Lock` 的阻塞获取保证的是「不同时执行」,而需求要的是「进行中就不再来一轮」。两者的差别只在后来者的处置:阻塞 = 排队后补跑,非阻塞 = 直接丢弃。

第二个根因是**接口没有把这个状态告诉调用方**。路由无条件返回 `{"ok": True}`,前端只能猜——于是写了个固定 30 秒的假禁用窗口,既不准(真实一轮取决于 UP 数量,可能几十秒也可能几分钟)也不对(禁用期结束后再点仍然会叠加)。假窗口是缺陷的**症状**,不是它自己的缺陷:后端如实回报后,前端不再需要猜。

### B5

根因与 v2.1 修过的那批完全同源:终态集合有硬编码副本。当时收敛了 `_ACTIVE` / `find_active_by_url` / `_load_persisted` 三处活跃态副本(都漏 `retrying`),但 `downloader.py` 里两处**终态**副本没有被纳入排查范围,`interrupted` 因此漏掉。

漏掉的具体后果链:`cancel_download` 把 `interrupted`(终态)判成"还能取消",于是置 `cancel = True` 并把状态改成 `cancelling`(活跃态)。而 `cancelling → cancelled` 的转移只发生在 `_run_download` / `_finalize_cancelled` 里,即**必须有一个正在运行的下载线程**。`interrupted` 任务的线程早在上次进程结束时就没了,没有任何人会去收尾 → 永久 `cancelling`。1.12 的两个后果(409 拦截、清不掉记录)都是这个错误活跃态的下游效应,不是独立缺陷。

### B6

根因是**同一份统计里两套周期口径**,且滚动窗口那套是更早的实现遗留。`month_start` 走 `datetime.date(...) → timetuple() → time.mktime()`(本地自然月),`day_start` 走 `now - 86400`(滚动 24 小时)。`time.mktime` 按本地时区解释 `timetuple`,正是自然周期起点所需的语义;把同一套写法用在「日」上即可,不需要引入新的时间处理方式。

---

## Correctness Properties

Property 1 至 Property 6 为各缺陷的 Fix Checking(缺陷输入下的期望行为),Property 7 至 Property 12 为对应的 Preservation Checking(非缺陷输入下行为不变)。

Property 1: Bug Condition - 勾选下标必须能定位回它所代表的条目

_For any_ 一次列表渲染中的可见条目,当搜索词非空且过滤确实改变了列表(`isBugCondition_B1` 为真)时,该条目复选框发出的下标 `emitted` SHALL 满足 `state.items[emitted]` 就是该条目本身;由此「删除记录 + 文件」只删该可见条目的记录与磁盘文件、批量下载只提交所勾选的可见条目、界面高亮的「已勾选」集合与真实选择集完全一致。

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Bug Condition - 兜底匹配与 stem 归属规则等价

_For any_ 期望名与磁盘 stem 的组合,当 pp 钩子未捕获权威路径、磁盘 stem 与期望名不相等但共享前 10 个字符(`isBugCondition_B2` 为真)时,扫盘兜底的命中结果 SHALL 与 `stem_belongs(diskStem, expectedStem)` 逻辑等价——即仅当磁盘名与期望名精确相等、或期望名后紧跟分隔符后缀(` ` / `.` / `-` / `_` / `(` / `[`)时才认作本任务的落盘文件;不满足该归属规则的文件一律不认、不改名。

**Validates: Requirements 2.4, 2.5**

Property 3: Bug Condition - 已下载判定的粒度是 (bvid, page)

_For any_ URL 与任务表的组合,当同 BV 存在已完成任务但其分 P 与被判定 URL 的分 P 不同、且被判定的那个分 P 自己没有完成任务(`isBugCondition_B3` 为真)时,`isDownloaded'(url)` SHALL 返回 false;由此该分 P 显示为未下载、批量提交时被入队而不被跳过。

**Validates: Requirements 2.6, 2.7, 2.8**

Property 4: Bug Condition - 后来者跳过而非排队

_For any_ 检查触发(手动点击或后台轮询到点),当已有一轮 refresh 正在运行(`isBugCondition_B4` 为真)时,本次触发 SHALL 不发起任何 B 站请求、不叠加第二轮,且手动检查接口 SHALL 在响应中如实标明「已有检查在进行中、本次未启动新一轮」,前端据此给出对应提示而不再依赖固定 30 秒的假禁用窗口。

**Validates: Requirements 2.9, 2.10**

Property 5: Bug Condition - 终态任务不可被拉回活跃态

_For any_ 任务,当其状态属于 `TERMINAL` 但不在旧硬编码副本 `("done", "failed", "cancelled")` 内(`isBugCondition_B5` 为真,即状态为 `interrupted`)时,`cancelDownload'(task)` SHALL 返回 false 且任务状态保持为 `interrupted`;且「取消」「重复下载拦截」「清除全部记录」三处对终态的判断 SHALL 完全一致地把它当作已结束的任务——取消被拒、不拦截同 URL 的新下载、记录可被清除。

**Validates: Requirements 2.11, 2.12**

Property 6: Bug Condition - 「今日」按本地自然日

_For any_ `finished_at` 时间戳,当它早于本地自然日 00:00 但晚于「当前时刻往前 86400 秒」(`isBugCondition_B6` 为真)时,该任务 SHALL 不计入 `day_count`;「今日」的起点 SHALL 是本地自然日 00:00,与「本月 = 自然月」同口径。

**Validates: Requirements 2.13**

Property 7: Preservation - 无搜索词时的勾选与批量操作

_For any_ 搜索词为空的列表渲染(`isBugCondition_B1` 为假),修复后代码 SHALL 产出与修复前逐位相同的 `pairs`、`data-libidx` 取值与高亮集合;由此无搜索词时的精确删除、批量提交、「全选」只作用于可见条目、封面点选切换、有勾选时快源退回「查看最新」、有勾选时推迟整体重拉,全部与修复前行为一致。

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Property 8: Preservation - 权威路径优先与合法后缀命中

_For any_ 不满足 `isBugCondition_B2` 的输入(pp 钩子已捕获权威路径、或磁盘名与期望名精确相等、或两者不共享前 10 字符),修复后的落盘文件定位 SHALL 与修复前产生相同结果;权威路径优先不扫盘、精确同名命中、期望名加分隔符后缀(` - P3` / `.zh-Hans` / ` (1)`)命中、副文件整族覆盖、目标已存在时按同大小 / 不同大小写 `rename_skipped` / `rename_conflict`,全部不变。

**Validates: Requirements 3.7, 3.8, 3.9, 3.10, 3.11**

Property 9: Preservation - 单 P 与同分 P 的已下载判定

_For any_ 不满足 `isBugCondition_B3` 的输入,`isDownloaded'(url)` SHALL 与 `isDownloaded(url)` 返回相同结果;不带 `?p=` 的单 P URL 且其 BV 有完成任务仍判已下载、(BV, 分 P) 与完成任务完全相同仍判已下载并在批量提交时跳过、提取不到 BV 仍判未下载、非 `done` 状态仍不计入已下载。

**Validates: Requirements 3.12, 3.13, 3.14, 3.15**

Property 10: Preservation - 单轮检查的执行语义

_For any_ 不满足 `isBugCondition_B4` 的触发(没有检查正在运行),`refreshOnce'` SHALL 与 `refreshOnce` 产生相同的副作用与返回值;后台线程起一轮并立即返回不阻塞前端、UP 之间间隔不少于 5 秒、限流返回空时不推进已见标记、后台轮询按配置间隔周期触发、单个 UP 异常只记日志不中断整轮,全部不变。

**Validates: Requirements 3.16, 3.17, 3.18, 3.19, 3.20**

Property 11: Preservation - 活跃态与其余终态的取消语义

_For any_ 不满足 `isBugCondition_B5` 的任务(状态属于 `ACTIVE`,或属于 `done` / `failed` / `cancelled`),`cancelDownload'` 与 `finalizeCancelled'` SHALL 与修复前产生相同结果;活跃态任务仍置 `cancelling` 并由下载线程收尾为 `cancelled`、`done` / `failed` / `cancelled` 仍返回失败且状态不变、收尾时仍只在任务尚未处于终态时才落为 `cancelled`、前端状态列表仍与后端活跃态集合一致。

**Validates: Requirements 3.21, 3.22, 3.23, 3.24**

Property 12: Preservation - 本月口径与总量统计

_For any_ 不满足 `isBugCondition_B6` 的 `finished_at`(今天完成、或早于「当前时刻往前 86400 秒」),`computeStats'` SHALL 与 `computeStats` 返回相同的 `month_count` / `total_count` / `total_bytes` / `disk_bytes`,且今天完成的任务仍计入「今日」;「本月」仍按自然月计算(上月末完成的不计入)、总量仍只统计 `done` 任务并在缺 `downloaded_bytes` 时用下载目录递归磁盘实测值兜底。

**Validates: Requirements 3.25, 3.26, 3.27**

---

## Fix Implementation

### B1 携带原始下标

**File**: `static/js/lib.js`

**Function**: `renderDefaultList()`(新增模块内私有 `matchesQuery()` 与导出 `visiblePairs()`)

**Specific Changes**:

1. **抽出 pairs 构造为唯一来源**。新增 `visiblePairs(q)`,遍历 `state.items` 本身,`idx` 天然就是原始下标;搜索态按谓词筛选,非搜索态按 `idx < state.shown` 截断:

   ```js
   /* 搜索匹配谓词(标题或 UP 名命中);q 已 toLowerCase + trim */
   function matchesQuery(it, q) {
     const t = (it.title || '').toLowerCase();
     const u = (it.uploader || '-').toLowerCase();
     return t.includes(q) || u.includes(q);
   }

   /* 当前应渲染的 (条目, 原始下标) 对。
      idx 必须是条目在 state.items 里的原始下标 —— 勾选框的 data-libidx、
      toggleLibCheck、state.checked、delSelected/libDownload 全按原始下标索引
      state.items。曾对过滤后的数组 forEach 取位置当下标,搜索态下删除/下载
      会打到另一条记录(含 unlink 磁盘文件,不可恢复)。 */
   export function visiblePairs(q) {
     const pairs = [];
     state.items.forEach((it, idx) => {
       if (q ? matchesQuery(it, q) : idx < state.shown) pairs.push({ it, idx });
     });
     return pairs;
   }
   ```

2. **`renderDefaultList()` 改用 `visiblePairs()`**,并把 meta 的「匹配 N」改成取 `pairs.length`(与过滤结果同一个数,不再单独维护 `filtered`):

   ```js
   export function renderDefaultList(d = libDom()) {
     if (!d || !d.list) return;
     const ctrl = page();
     const q = getLibSearchQuery();
     if (!state.shown) state.shown = curChunk();
     const pairs = visiblePairs(q);
     renderLibList(pairs, d.list);
     if (d.meta) {
       d.meta.textContent = q
         ? `共 ${state.items.length} 条 · 匹配 ${pairs.length}`
         : (ctrl.showCount === false ? '' : `共 ${state.items.length} 条`);
     }
     maybeFillScreen();
   }
   ```

   注意 `if (!state.shown) state.shown = curChunk();` 必须移到 `visiblePairs()` 之前(原代码在 `filtered` 之后),否则非搜索态首次渲染时 `state.shown` 为 0,`idx < 0` 恒假,列表会空。

3. **`advanceDefaultChunk()` 里的第二处 pairs 构造改为复用 `visiblePairs()`**,消灭重复实现(它原本用 `state.items.slice(0, state.shown).map(...)`,结果正确但是同一规则的第二份写法):

   ```js
   state.shown += curChunk();
   renderLibList(visiblePairs(getLibSearchQuery()), libDom().list);
   ```

   不改成调 `renderDefaultList()`,是为了避免 `renderDefaultList → maybeFillScreen → advanceDefaultChunk` 的间接递归(虽有 `_filling` 保护,但依赖调用路径,脆弱)。

**不需要改的地方(明确记录,避免评审时误扩范围):**

- `renderLibList(pairs, container)`:签名已是 `[{it, idx}]` 形态,内部用 `origIdx` 写 `data-libidx` / `toggleLibCheck` / 高亮判定,语义正确。
- `downloads.delSelected()` / `downloads.libDownload()`:从 DOM 读 `:checked` 复选框再 `state.items[parseInt(cb.dataset.libidx)]`,语义本来就是「按原始下标索引 `state.items`」,原始下标修对后自然正确。
- `lib.toggleLibCheck()` / `lib.libToggleAll()` / `lib.updateLibCount()`:全部已按原始下标 / DOM 计数工作。
- 6 个受影响页面(downloads / history / feed(bili 源) / fav(夹内) / seasons(集内) / later)全部复用 `renderDefaultList`,无需逐页改动。

**需求 2.3 为什么在修好原始下标后自然成立:**

`state.checked` 存的一直是原始下标(`toggleLibCheck(origIdx)` 直接 `add(origIdx)`),`renderLibList` 的高亮判定也一直是 `state.checked.has(origIdx)`。缺陷唯一的来源是 `origIdx` 本身在搜索态下取错了值——A 的下标 0 被 C 拿到,于是 `has(0)` 命中在 C 身上。原始下标修对后,一个条目被高亮**当且仅当**它自己的原始下标在 `checked` 里,选择状态不可能转移到别的条目。**因此 2.3 不需要额外改动。**

配套语义(修复前后一致,记录以免被误认为回归):

- 先勾选后搜索时,未命中搜索词的条目仍留在 `state.checked` 里,但不在 DOM 中。`delSelected()` / `libDownload()` / `updateLibCount()` 都从 DOM 读 `:checked`,所以它们只作用于**可见且勾选**的条目——这正是 2.1 / 2.2 要求的「只删除该可见条目自身」。
- `cachedLoad()` 的快源无感替换守卫用的是 `state.checked.size === 0`(包含被搜索隐藏的勾选),因此 3.5 的保护范围不变,仍然保守。

---

### B2 兜底分支复用 stem 归属规则

**Files**: `taskstore.py`(提升 `_stem_belongs` 为公开名)、`files.py`(兜底分支改为引用它)

**Specific Changes**:

1. **`taskstore.py`:`_stem_belongs` → `stem_belongs`**,保留一行向后兼容别名。这与同文件已有的 `_ACTIVE = ACTIVE` 是同一处理方式,历史注释与 CHANGELOG 里对旧名的引用不会失真:

   ```python
   def stem_belongs(stem: str, expected: str) -> bool:
       """磁盘文件的 stem 是否属于期望名 expected(精确相等,或带分隔符后缀)。

       全项目唯一来源:taskstore._task_files(定位/删除)与 files._find_actual_file
       (下载后改名对齐)共用。曾各写一份,files 侧那份用 expected[:10] 前缀匹配,
       会把邻居视频的文件连同副文件一起改名成本任务的期望名(不可逆)。
       """
       ...

   _stem_belongs = stem_belongs  # 旧名保留,避免下游 import 断裂
   ```

   同时把 `_task_files` 的内部调用与其 docstring 里的「匹配规则见 `_stem_belongs`」一并改为新名。

2. **`files.py`:import 并替换兜底谓词**。顶部已有 `from taskstore import _tasks, _tasks_lock, _persist, _final_paths`,依赖方向已经是 `files.py → taskstore`,直接加名字即可,**不在 files.py 里复制第三份匹配规则**:

   ```python
   from taskstore import (_tasks, _tasks_lock, _persist, _final_paths,
                          stem_belongs)
   ```

   ```python
   def _find_actual_file(task_id: str, expected_stem: str):
       """找 yt-dlp 实际落盘的主文件。

       优先 postprocessor 钩子捕获的最终路径(权威);没捕获到(旧流程/异常)
       再扫下载目录:stem 精确匹配 → stem 归属规则(stem_belongs)。返回 Path 或 None。
       """
       ...
           exact = [f for f in media if f.stem == expected_stem]
           if exact:
               return max(exact, key=lambda f: f.stat().st_size)
           # 兜底:yt-dlp 对字面段做了意外变换时,按 stem 归属规则找
           # (与 taskstore._task_files 同一份规则;曾用 expected_stem[:10] 前缀,
           #  共享前 10 字的邻居视频会被误认并改名 —— 短名时更是整名无边界前缀)
           belongs = [f for f in media if stem_belongs(f.stem, expected_stem)]
           if belongs:
               return max(belongs, key=lambda f: f.stat().st_size)
       return None
   ```

   保留 `exact` 这一级(而不是合并进 `belongs`):`stem_belongs` 的结果集包含精确同名者,若合并后用 `max(size)` 挑选,可能选到一个体积更大的带后缀文件而不是精确同名文件。分两级保证「精确名优先」,3.8 的既有行为逐位不变。

**改用 `stem_belongs` 后会退化的合法场景,及为什么可接受:**

「yt-dlp 对字面段做了意外变换」可细分为三类落盘形态:

| 形态 | 例子(期望名 `标题`) | 旧规则(前 10 字前缀) | 新规则(`stem_belongs`) |
|------|---------------------|----------------------|------------------------|
| (a) 追加后缀 | `标题 [下载].mp4`、`标题.f137.mp4`、`标题 - P3.mp4`、`标题 (1).mp4` | 命中 | **命中**(余部以 ` ` / `.` / `-` / `(` 开头) |
| (b) 名字被截断 | 长标题落盘成期望名的前缀 | 命中 | **不命中** |
| (c) 第 11 字之后的字符被替换 | `标题abcdefghXX` → `标题abcdefghYY` | 命中 | **不命中** |

即 (b)(c) 确有退化。退化后 `_find_actual_file` 返回 `None`,`align_task_file()` 走:

```python
if actual is None:
    t["rename_error"] = "下载完成但没找到落盘文件"
```

**该分支的行为可接受,理由四条:**

1. **不影响任务完成状态。** `align_task_file` 的全部失败路径都只写展示字段;`_run_download` 在 `_align_task_file(task_id)` 返回之后才置 `status = "done"` 并统计体积,所以任务照样是 `done`。
2. **失败是可见的、可恢复的。** `rename_error` 会被前端 `taskWarnHtml()` 渲染成条目内提示条(「自动改名失败:…」),文件仍在磁盘上,用户可自行改名或用「在 Finder 中显示」定位下载目录。
3. **被换掉的旧行为是不可恢复的。** 旧规则的失败模式是把**另一个视频**的文件连同其封面 / 字幕 / 弹幕一起改名成本任务的期望名,原文件名永久丢失,两条记录指向同一个文件。用「少数异常场景退化为可见可恢复的 `rename_error`」换掉「误改邻居文件的不可逆数据损坏」,取舍方向明确。
4. **(b)(c) 本身已是异常路径,且被上游大幅收窄。** 命名权在 Python 侧(设计 A.2):outtmpl 用 `build_filename` 组装好的**字面**文件名 + `.%(ext)s`,不再让 yt-dlp 按字节截断 `%(title).200B`;正常路径由 pp 钩子写入 `_final_paths` 直接给出权威路径,连扫盘都不走(3.7)。

**结论:接受该退化,不为 (b)(c) 保留任何前缀兜底**——保留就等于把第二份更宽松的规则留在原地,缺陷会以同样的形态复现。

---

### B3 已下载判定改为 (bvid, page) 粒度

**Files**: `extras.py`(新增键规则)、`taskstore.py`(后端判定)、`static/js/util.js`(前端键规则)、`static/js/pages/downloads.js`(前端判定)

#### 1. `extras.py` 新增键规则(依赖链底部)

放在 `extras.py` 而不是 `taskstore.py`,与 `bvid_from_url` / `build_filename` 同层:`taskstore` 已经 `import extras`,`auto_refresh` 经 `downloader` 间接复用同一份,前端语义也以此为基准。纯字符串逻辑必须待在依赖链底部。

```python
# 已下载判定的键分隔符。前端 static/js/util.js 的 DL_KEY_SEP 必须与此一致,
# tests 有绑定断言 —— 两侧键规则漂移会让角标与批量跳过各说各话,且不会报错。
DL_KEY_SEP = "#p"

_PAGE_RE = re.compile(r"[?&]p=(\d+)")


def page_from_url(url: str) -> int:
    """从视频 URL 取分 P 序号;无 ?p= / 非法值视为 1。

    URL 形态:https://www.bilibili.com/video/{BV}?p={N}
    """
    m = _PAGE_RE.search(url or "")
    return int(m.group(1)) if m else 1


def dl_key(bvid: str, page: int) -> str:
    """「已下载」判定的唯一键:(bvid, 分P) → "BVxxxxxxxxxx#p1"。

    判定粒度必须是 (bvid, page):只看 bvid 会让合集下完 P1 后其余分 P
    全被当已下载而跳过,自动下载也漏下新分 P。
    """
    return f"{bvid}{DL_KEY_SEP}{int(page) or 1}"
```

#### 2. `taskstore.is_downloaded()`

```python
def is_downloaded(url: str) -> bool:
    """该 (BV 号, 分 P) 是否已有 done 任务——列表「已下载」标记与批量跳过用。

    查询侧的 page 只能从 URL 的 ?p= 解析(入参只有 URL);任务侧的 page 必须读
    t["page"],不能解析 t["url"] —— start_download(url, label, page=3) 存进任务表的
    url 是用户粘贴的原始链接(通常不带 ?p=),page 才是权威值。两侧取法不对称是刻意的。
    """
    bv = bvid_from_url(url)
    if not bv:
        return False
    key = extras.dl_key(bv, extras.page_from_url(url))
    with _tasks_lock:
        for t in _tasks.values():
            if t.get("status") != "done":
                continue
            tbv = bvid_from_url(t.get("url") or "")
            if tbv and extras.dl_key(tbv, int(t.get("page") or 1)) == key:
                return True
    return False
```

锁内只有纯字符串运算,与现状同级(现状也在锁内调 `bvid_from_url`),不引入 IO,不调 `_persist()`。

#### 3. `static/js/util.js` 新增键规则(与 `bvFromUrl` 同层)

```js
/* 已下载判定的键分隔符。必须与后端 extras.DL_KEY_SEP 一致(tests 有绑定断言)。 */
export const DL_KEY_SEP = '#p';

/* 从视频 URL 取分 P 序号;无 ?p= / 非法值视为 1。 */
export function pageFromUrl(url) {
  const m = /[?&]p=(\d+)/.exec(url || '');
  return m ? parseInt(m[1], 10) : 1;
}

/* 「已下载」判定的唯一键:(bv, 分P) → "BVxxxxxxxxxx#p1" */
export function dlKey(bv, page) {
  return bv + DL_KEY_SEP + (parseInt(page, 10) || 1);
}
```

#### 4. `static/js/pages/downloads.js`

```js
import { escapeHtml, escapeAttr, formatBytes, bvFromUrl, dlKey, pageFromUrl } from '../util.js';

/* 已下载判定(列表角标 + 批量跳过):done 任务的 (BV, 分P) 键集合。
   只按 BV 建集合会让合集下完 P1 后其余分 P 全被当已下载。 */
let _doneKeys = new Set();

/* 集合构建的唯一来源(loadDownloads 与 pollTasks 共用)。
   任务侧的 page 读 t.page —— /api/tasks 的任务对象已含该字段,
   不能从 t.url 解析(任务 url 常不带 ?p=)。 */
function doneKeySet(tasks) {
  return new Set(tasks.filter(t => t.status === 'done')
    .map(t => { const bv = bvFromUrl(t.url); return bv ? dlKey(bv, t.page || 1) : null; })
    .filter(Boolean));
}

function isDownloaded(url) {
  const bv = bvFromUrl(url);
  return !!bv && _doneKeys.has(dlKey(bv, pageFromUrl(url)));
}
```

`loadDownloads()` 与 `pollTasks()` 里原本各有一份 `new Set(tasks.filter(...).map(bvFromUrl)...)`,两处都改为 `_doneKeys = doneKeySet(tasks);`,消灭重复。

#### 5. 调用点清单(设计必须覆盖全部)

| 调用点 | 入口表达式 | 修复后的行为 |
|--------|-----------|-------------|
| `auto_refresh.refresh_once()` | `downloader.is_downloaded(v["url"])` | 走后端新实现。`v["url"]` 来自 `bapi.fetch_up_videos` → `_video_item`,形如 `.../video/{bvid}`(**不带 `?p=`**)→ page 解析为 1 |
| 列表页「已下载」角标 | `lib.renderLibList` → `_itemExtras.isDownloaded(it.url)`(经 `setItemExtras` 注入),以及导出的 `lib.isUrlDownloaded(url)` | 走前端新实现 |
| `downloads.libDownload()` | 模块内直接调 `isDownloaded(it.url)` | 走前端新实现。列表页数据源的 URL 不带 `?p=` → page 1,与该函数提交的 `page: 1` 自洽 |
| `newdl.batchDownload()` | `lib.isUrlDownloaded(it.url)` | 走前端新实现。`parsing.resolve_playlist` 对多 P 视频产出 `?p=N` 形态的 URL,**这里是 1.7 的真实现场**;该函数本来就用 `it.url.includes('?p=')` 解析 page 提交,与新判定一致 |
| 后端重复下载拦截 | `find_active_by_url(url, label, page)` | 本来就按 (URL, 清晰度, 分 P) 判定,**不在本次改动范围** |

#### 6. 键格式与前后端一致性的锁定方式

**统一键格式:`{bvid}#p{page}`**,分隔符常量化为两侧各自的唯一来源(`extras.DL_KEY_SEP` / `util.js` 的 `DL_KEY_SEP`),page 缺失一律归一为 1。

一致性用两条测试锁住,沿用项目既有做法:

1. **常量绑定断言**(对应 `TASK_ACTIVE ↔ taskstore.ACTIVE` 的写法):用 regex 从 `static/js/util.js` 提取 `DL_KEY_SEP = '…'` 的字面值,断言等于 `extras.DL_KEY_SEP`。任一侧改分隔符不同步就跑挂。
2. **前后端状态集合一致性断言**:给定一组固定任务(含多 P),用后端 `extras.dl_key` 算出 done 键集合;同时对前端拼接串做子串断言,确认 `doneKeySet` 用 `dlKey(bv, t.page || 1)` 建集合、`isDownloaded` 用 `dlKey(bv, pageFromUrl(url))` 查集合,且旧名 `_doneBvids` 已不再出现。这样"少了 page 维度"或"两侧取 page 的方式写反"都会被抓到。

#### 7. 需求 2.8 的可达性(如实记录)

`auto_refresh` 的输入 URL 恒不带 `?p=`(见调用点清单),因此解析出的 page 恒为 1。2.8「该 BV 号已有其他分 P 的完成任务 → 新分 P 应入队」在两种形态下结果不同:

- **已完成任务是该 BV 的非 1 分 P**(用户曾用分 P 选择器下过 P3):新键 `BV#p1` ≠ 已有键 `BV#p3` → 判未下载 → 入队。**2.8 成立。**
- **已完成任务是该 BV 的 P1,而「新分 P」是同 BV 下新增的 P2**:`arc/search` 只报该 BV、不带分 P 信息 → 解析为 page 1 → 仍判已下载 → 跳过。**2.8 不成立。**

后者的残余缺口根因不在判定键,而在数据源:要覆盖它必须对每个新 BV 额外调 `x/player/pagelist` 枚举分 P,给自动检查增加「1 次接口调用 / 视频」,直接冲撞 B4 正在收紧的风控边界与 `docs/COMPLIANCE.md` 的对外承诺。**这属于范围扩大,不在本次实现**,列入「待决策项」。B3 的最小修复面交付 2.6 / 2.7 完整、2.8 部分成立。

---

### B4 检查触发改为跳过而非排队

**Files**: `auto_refresh.py`、`app.py`(路由)、`static/js/pages/feed.js`(按钮反馈)

#### 1. `auto_refresh.py`:非阻塞获取 + 状态谓词

```python
def is_running() -> bool:
    """是否已有一轮检查在跑。路由据此如实回报「本次未启动新一轮」(需求 2.10)。"""
    return _lock.locked()


def refresh_once() -> int:
    """跑一轮检查:清单里每个 UP 的新投稿(未下载过的)直接入队下载。

    返回本轮入队任务数(未启动新一轮时为 0)。串行 + 间隔,防账号级风控。
    锁是非阻塞获取:已有一轮在跑时**直接跳过**而不是排队 —— 阻塞获取会让连点 N 次
    变成串行跑 N 轮完整检查(每 UP 一次 arc/search、UP 间隔 5 秒),违背保守间隔设计。
    """
    if not _lock.acquire(blocking=False):
        log.info("自动下载:已有一轮检查在运行,本次跳过(不排队、不发请求)")
        return 0
    try:
        ups = up_list.list_ups()
        ...  # 函数体一字不改
        return enqueued
    finally:
        _lock.release()
```

**返回值契约不变**(仍是 `int` = 本轮入队条数),因此两个既有调用方**都不需要改**:

| 调用方 | 代码 | 跳过时的表现 |
|--------|------|-------------|
| `app.py` `/api/up-list/check` 后台线程 | `n = auto_refresh.refresh_once(); log.info("手动检查完成,入队 %d 条", n)` | 打「入队 0 条」,名实相符 |
| `auto_refresh.start_loop()` 轮询循环 | `n = refresh_once(); if n: log.info(...)` | `n == 0` → 不打日志,语义正确 |

「是否在跑」通过**新增谓词** `is_running()` 暴露,而不是把 skipped 编码进返回值(例如 `-1`)——后者会让 `if n:` 把 `-1` 当真值,必须同步改两个调用方,改动面更大。

#### 2. `app.py`:路由如实回报

```python
@app.route("/api/up-list/check", methods=["POST"])
def up_list_check():
    """手动触发一轮检查(与后台自动下载同一逻辑):清单 UP 的新投稿直接入队下载。
    串行 + UP 间隔,耗时数十秒,放后台线程跑,前端靠队列轮询看结果。

    已有一轮在跑时如实回报 started=False(需求 2.10):不排队、不发 B 站请求,
    前端据此提示,不再用固定 30 秒的假禁用窗口装样子。
    """
    if auto_refresh.is_running():
        return jsonify({"ok": True, "started": False,
                        "message": "已有检查在进行中,本次未启动新一轮"})

    def _run():
        try:
            n = auto_refresh.refresh_once()
            log.info("手动检查完成,入队 %d 条", n)
        except Exception:
            log.exception("手动检查异常")
    threading.Thread(target=_run, daemon=True, name="bd-manual-check").start()
    return jsonify({"ok": True, "started": True, "message": "已在后台开始检查"})
```

- `ok` 保持 `True`:「已有检查在进行中」不是错误,是如实告知。既有测试里的 `assert r.get_json()["ok"]` 不受影响。
- **竞态说明(可接受)**:`is_running()` 与线程启动之间有一个极窄窗口,两个并发请求可能都读到 `False` 并各起一个线程。但 `refresh_once` 的非阻塞获取保证第二个线程立即返回 0——**绝不会有两轮并发,也绝不会排队**,这正是风控真正需要的不变量。代价仅是响应可能乐观地报 `started: True` 而实际被跳过。
  若要求响应与实际严格一致,可改为「在请求线程里 try-acquire、由 worker 在 `finally` 释放」(`threading.Lock` 允许跨线程 release)。本设计**不采用**:锁的持有跨线程会让所有权变得难以推理,而它换来的只是一个极窄窗口下的响应文案准确性。

#### 3. `static/js/pages/feed.js`:按新语义给反馈

```js
export async function upCheckNow(btn) {
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>检查中...'; }
  try {
    const r = await postEmpty('/api/up-list/check');
    // started=false:后端已有一轮在跑,本次未启动 —— 如实告知,不再靠固定 30 秒假禁用
    alert(r && r.started === false
      ? '已有检查在进行中,本次未启动新一轮。稍后队列会自动带回新任务。'
      : '已在后台开始检查,新视频会自动进入下载队列。');
  } catch (e) {
    alert(e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  }
}
```

- **固定 30 秒禁用窗口整体删除**(需求 2.10)。按钮只在请求在途期间禁用;再次点击不再叠加,由服务端跳过并如实回报。不变量由后端保证,前端不需要猜一轮检查要跑多久。
- 3.16 保持:路由仍立即返回,不阻塞前端。

**前端桥的兼容性(必须保持,否则既有测试跑挂):**

- 函数名 `upCheckNow` 不变、`export` 不变、参数仍是 `(btn)`。
- `templates/index.html` 的 `onclick="upCheckNow(this)"` 不变。
- `static/js/app.js` 的 window 桥条目 `upCheckNow: feed.upCheckNow` 不变。

→ `test_frontend_window_bridge_targets_are_exported` 与 `test_frontend_inline_handlers_are_bridged` 自然继续通过,**无需改桥**。

---

### B5 终态判断复用 taskstore.TERMINAL

**File**: `downloader.py`

**Functions**: `cancel_download()`、`_finalize_cancelled()`

**Specific Changes**:

1. **顶部 import 加 `TERMINAL`**(已有一批同源名字,按同样方式引入):

   ```python
   from taskstore import (_tasks, _tasks_lock, _persist, _final_paths,
                          _load_persisted, _task_files, TERMINAL,
                          get_task, list_tasks, find_active_by_url, is_downloaded,
                          find_task_files, delete_tasks, clear_tasks,
                          clean_invalid_records)
   ```

2. **`cancel_download()`**:

   ```python
   def cancel_download(task_id: str) -> bool:
       """标记任务取消。yt-dlp 线程会在下个分片边界停止(无法强杀,靠标志位)。

       终态判断走 taskstore.TERMINAL 唯一来源。曾硬编码 ("done","failed","cancelled")
       漏了 interrupted:对中断任务调取消会把它改成 cancelling(活跃态)却没有任何线程
       去收尾 → 永久僵尸态,连带卡住重复下载拦截与「清除全部记录」。
       """
       with _tasks_lock:
           t = _tasks.get(task_id)
           if not t or t["status"] in TERMINAL:
               return False
           t["cancel"] = True
           t["status"] = "cancelling"
       _persist()
       return True
   ```

3. **`_finalize_cancelled()`**:

   ```python
   def _finalize_cancelled(task_id: str) -> None:
       """把未终结的任务落为已取消(取消发生在下载回调之外时的兜底)。"""
       with _tasks_lock:
           t = _tasks.get(task_id)
           if t and t["status"] not in TERMINAL:
               t["status"] = "cancelled"
       _persist()
   ```

`_persist()` 仍在锁外调用(现状已如此),锁纪律不变。

**需求 3.23 一致性确认(用户明确要求核对):**

`_finalize_cancelled` 的语义是「未终结才落为 cancelled」。改用 `TERMINAL` 后 `interrupted` 也被视为已终结、不会被覆盖成 `cancelled`——与 3.23「只在任务尚未处于终态时才落为 `cancelled`,已终结的任务状态不被覆盖」**字面一致**。旧的三元组写法反而违反 3.23:它会把一个已终结的 `interrupted` 覆盖成 `cancelled`。

补充实际可达性:`interrupted` 只由 `_load_persisted()` 在启动时写入,此时不存在对应的下载线程;且修复后 `cancel_download` 不再给它置 `cancel` 标志,所以运行中 `_finalize_cancelled` 到不了 `interrupted` 分支。这处改动在行为上等价,价值是**消灭第二份硬编码副本**——留着它,下次新增终态时又会漏一处。

**需求 2.12 的三处一致性核对(修复后):**

| 判断处 | 实现 | 对 `interrupted` |
|--------|------|-----------------|
| 取消 | `cancel_download` → `t["status"] in TERMINAL` | 拒绝、状态保持 `interrupted` ✓(2.11) |
| 重复下载拦截 | `find_active_by_url` → `t["status"] in ACTIVE` | 不拦截,同 URL 可重新下载 ✓(本来就对) |
| 清除全部记录 | `clear_tasks` → `t["status"] not in _ACTIVE` | 可被清除 ✓(本来就对) |

三处判断都已引用唯一来源。1.12 描述的两个后果(409 拦截、清不掉记录)都是「被错误置成 `cancelling`」的下游效应,修好取消这一处即同时消失——**不需要改另外两处**。

---

### B6 「今日」改为本地自然日 00:00

**File**: `taskstore.py`

**Function**: `compute_stats()`

**Specific Changes**:

```python
def compute_stats() -> dict:
    """统计面板数据:总下载数、总字节数、本月/今日 数。本月=自然月,今日=自然日。

    历史 done 任务没有 downloaded_bytes 字段时,总字节用下载目录磁盘实测值兜底。
    """
    import datetime
    today = datetime.date.today()
    month_start = int(time.mktime(datetime.date(today.year, today.month, 1).timetuple()))
    # 本地自然日 00:00,与 month_start 同一套 datetime/time.mktime 写法。
    # 曾用 now - 86400(滚动 24h):昨晚 23:00 完成的任务今早仍算「今日」,
    # 与同一份统计里「本月 = 自然月」自相矛盾。
    day_start = int(time.mktime(today.timetuple()))
    ...
```

- `date.timetuple()` 的时 / 分 / 秒为 0,`time.mktime` 按**本地时区**解释 → 正是本地自然日 00:00,与 `month_start` 完全同源。
- `now = int(time.time())` 在修复后不再被任何表达式使用(它此前只服务于 `day_start = now - 86400`),**一并删除**,不留死变量。
- 其余部分(累加循环、`disk_bytes` 递归统计、返回结构)一字不改。

---

## Testing Strategy

### Validation Approach

两阶段:先在**未修复**的代码上跑探索性测试,逼出能证明缺陷存在的反例并确认 / 推翻根因假设;再验证修复对缺陷输入生效(Fix Checking)、对非缺陷输入行为不变(Preservation Checking)。

前端没有 JS 运行时,B1 / B3(前端侧)/ B4(前端侧)的锁定方式是对 `_tpl()` 拼接串做子串断言,并用 regex 提取 JS 常量与后端常量绑定比较——沿用 `TASK_ACTIVE ↔ taskstore.ACTIVE` 与 `quality.js ↔ up_list.QUALITY_CHOICES` 的既有做法。

全部新增测试遵守「全局约束 → 测试隔离」:不触碰真实 `data/`,需要下载目录的用例 monkeypatch `config.DOWNLOAD_DIR` 到 `tmp_path`。

---

### Exploratory Bug Condition Checking

目标:在**未修复**代码上先看到失败,确认根因假设。

**B1** — 静态复核 + 手工复现。在下载记录页造 4 条记录,搜索命中后两条,勾选第一个命中项,读 DOM 的 `data-libidx`:未修复时是 `0`,而该条目在 `state.items` 里是第 2 个 → 直接印证「发出的是命中序号」。反例:`delSelected` 会取 `state.items[0]`。

**B2** — 在 `tmp_path` 里只放一个邻居文件 `第01期特别版.mp4`,任务期望名 `第01期`,清空 `_final_paths`,调 `files._find_actual_file("t", "第01期")`。未修复时返回邻居文件的 `Path`(反例);跑 `align_task_file` 会看到邻居文件被改名成 `第01期.mp4`。短名版本:期望名 `测试`,盘上 `测试2.mp4`。

**B3** — 造 done 任务 (BV1, page=1),调 `taskstore.is_downloaded(".../BV1?p=2")`。未修复时返回 `True`(反例),证明 page 维度未参与判定。

**B4** — 主线程 `auto_refresh._lock.acquire()` 后,在另一线程调 `refresh_once()`,并给 `bapi.fetch_up_videos` 打计数桩。未修复时该线程阻塞在锁上,主线程释放后计数 > 0(反例:排队后补跑,并真的发了请求)。

**B5** — 造 `interrupted` 任务,调 `downloader.cancel_download(tid)`。未修复时返回 `True` 且状态变成 `cancelling`(反例);随后 `find_active_by_url` 会拦截同 URL 新下载、`clear_tasks` 清不掉它。

**B6** — 样本 `finished_at = 本地自然日 00:00 − 60`(昨天 23:59),调 `compute_stats()`。未修复时(且当前时刻距昨天 23:59 不足 24h,即凌晨之外的大多数时刻)`day_count == 1`(反例)。

**Expected Counterexamples 汇总:**

- B1:`data-libidx` 发出命中序号而非原始下标 → 删除 / 下载打到 `state.items` 里同下标位置的另一条。
- B2:共享前 10 字(或短名整名前缀)的邻居文件被认作本任务落盘文件并被改名。
- B3:同 BV 不同分 P 被判为已下载。
- B4:后来者阻塞排队并在前一轮结束后完整再跑一遍,期间真实发起 B 站请求。
- B5:`interrupted` → `cancelling`,无人收尾。
- B6:昨日 00:00 之后完成的任务被计入「今日」。

若某条反例未能复现,则根因假设被推翻,需回到「Hypothesized Root Cause」重新分析,不得直接改代码。

---

### Fix Checking

对每条缺陷,验证「缺陷输入 → 期望行为」:

```
FOR ALL input WHERE isBugCondition_Bn(input) DO
  result := fixedFunction(input)
  ASSERT expectedBehavior_Pn(result)
END FOR
```

具体到本次:

- **B1**:`∀` 搜索态可见条目,`state.items[emitted] IS visibleItem`。
- **B2**:`∀` 满足 C(B2) 的 (期望名, 磁盘 stem),`matched = diskStem ⟺ stem_belongs(diskStem, expectedStem)`。
- **B3**:`∀` 满足 C(B3) 的 (url, tasks),`is_downloaded'(url) = False`。
- **B4**:`∀` 已有一轮在跑时的触发,零 B 站请求 + 响应 `started = False`。
- **B5**:`∀` `interrupted` 任务,`cancel_download'(t) = False` 且状态不变。
- **B6**:`∀` 满足 C(B6) 的 `finished_at`,不计入 `day_count`。

---

### Preservation Checking

```
FOR ALL input WHERE NOT isBugCondition_Bn(input) DO
  ASSERT F(input) = F'(input)
END FOR
```

**为什么本次以定向用例为主、不引入 PBT 框架**:6 条缺陷的非缺陷输入域都能被有限的等价类完全覆盖(B5 的差集只有一个状态;B2 的输入域按「精确 / 合法后缀 / 前缀 / 无关」四类划分即穷尽;B3 按「有无 `?p=`」× 「任务 page 是否相同」划分;B6 按「今天 / 昨天 / 更早」划分),定向用例即可给出完整覆盖,而项目现有测试栈没有 PBT 依赖。**不为此新增依赖**。B1 的组合空间较大(items × query × 勾选集),但它的正确性可由一个结构性不变量锁死(见下),比随机生成更强。

各条的 Preservation 测试点:

- **B1**:搜索词为空时 `visiblePairs('')` 的输出与 `state.items.slice(0, state.shown).map((it, idx) => ({it, idx}))` 逐位相同(结构性不变量,可用子串断言 + 手工核对锁定)。3.5 的守卫 `state.checked.size === 0` 表达式必须仍然存在。
- **B2**:精确同名命中、`标题 [下载].mp4` 等合法后缀命中、pp 钩子权威路径优先不扫盘、冲突时 `rename_skipped` / `rename_conflict` 分支——既有测试已覆盖,必须继续全绿。
- **B3**:不带 `?p=` 的 URL + 任务 `page=1` → `True`;(BV, page) 完全相同 → `True`;短链 / av 号 → `False`;非 `done` 状态 → `False`。
- **B4**:无人在跑时起一轮并立即返回;UP 间隔 sleep 序列不变;限流不推进 `last_seen`;单 UP 异常不中断整轮;返回值仍为入队条数——既有 `tests/test_auto_refresh.py` 已全覆盖,必须继续全绿(它们验证的是 `refresh_once` 的函数体,而本次只改锁获取方式)。
- **B5**:活跃态取消仍置 `cancelling`;`done` / `failed` / `cancelled` 取消仍返回 `False` 且状态不变。
- **B6**:`month_count` 仍按自然月;今天完成的仍计入「今日」;`total_count` / `total_bytes` / `disk_bytes` 不变。

---

### Unit Tests

按模块列出新增用例。

#### `tests/test_extras.py`(B3 键规则)

- `test_page_from_url`:`?p=3` → 3;无 `?p=` → 1;`&p=2` → 2;`?p=abc` → 1;`""` / `None` → 1。
- `test_dl_key_format`:`dl_key("BV1xx411c7mD", 3) == "BV1xx411c7mD#p3"`;`dl_key("BV…", 0)` 与 `dl_key("BV…", None)` 归一为 `#p1`;`DL_KEY_SEP == "#p"`。

#### `tests/test_downloader.py`(B3 后端判定、B5 状态机)

- `test_is_downloaded_is_per_page`(Fix Checking B3):done 任务 (BV1, page=1) → `is_downloaded(".../BV1")` 为 `True`、`is_downloaded(".../BV1?p=2")` 为 `False`;另造 done (BV1, page=3) → `?p=3` 为 `True`。
- `test_is_downloaded_uses_task_page_field_not_task_url`(B3 关键不对称):任务 `url` 不带 `?p=` 但 `page=3`(即 `start_download(url, label, page=3)` 的真实形态)→ `?p=3` 判 `True`、不带 `?p=` 判 `False`。锁住「任务侧读 `t["page"]`,不解析任务 URL」。
- `test_cancel_rejects_terminal_states`(Fix Checking B5 + Preservation 3.22):`interrupted` 取消返回 `False` 且状态仍为 `interrupted`;`done` / `failed` / `cancelled` 同样返回 `False` 且状态不变。
- `test_cancel_preserves_active_states`(Preservation 3.21):`queued` / `downloading` / `merging` / `retrying` 取消返回 `True`、状态变 `cancelling`、`cancel` 标志置位。
- `test_terminal_checks_use_single_source`(单一来源纪律):`downloader.py` 源码里不再出现字面元组 `("done", "failed", "cancelled")`,且 `cancel_download` / `_finalize_cancelled` 的源码都出现 `TERMINAL`(用 `inspect.getsource`,与该文件既有的静态分析风格一致)。
- `test_interrupted_task_not_blocking_and_clearable`(需求 2.12):`interrupted` 任务下 `find_active_by_url(url, label, page)` 返回 `None`;`clear_tasks(delete_file=False)` 能把它清掉。

#### `tests/test_filename_alignment.py`(B2)

- `test_find_actual_file_rejects_prefix_neighbour`(Fix Checking B2 / 需求 2.4):期望名 `第01期`,`tmp_path` 里只有 `第01期特别版.mp4` → `_find_actual_file` 返回 `None`,邻居文件仍在且文件名未变。
- `test_find_actual_file_rejects_short_name_prefix`(需求 2.5):期望名 `测试`,盘上 `测试2.mp4` → 不命中。
- `test_align_records_rename_error_when_no_belonging_file`(集成 + 退化行为确认):上述情形跑完整 `_run_download`,断言任务 `status == "done"`、`rename_error` 非空、邻居文件名逐字未变。这条同时锁住「退化为 `rename_error` 不影响完成状态」这个设计决策。
- `test_find_actual_file_shares_stem_rule`(单一来源纪律):`files.py` 源码不含 `[:10]` 切片,且从 `taskstore` import 了 `stem_belongs`。
- `test_stem_belongs_public_alias`:`taskstore.stem_belongs is taskstore._stem_belongs`(旧名别名保留,下游 import 不断裂)。

#### `tests/test_auto_refresh.py`(B4 后端)

- `test_refresh_once_skips_when_already_running`(Fix Checking B4 / 需求 2.9):主线程持 `auto_refresh._lock` 后调 `refresh_once()` → 立即返回 `0`,且 `up_list.list_ups` 与 `bapi.fetch_up_videos` 的调用计数均为 0(证明「未发起任何 B 站请求」)。
- `test_is_running_reflects_lock`:未持锁时 `is_running()` 为 `False`;持锁时为 `True`;释放后回到 `False`。
- `test_refresh_once_releases_lock_on_exception`:让 `up_list.list_ups` 抛异常,断言异常向上传播后 `is_running()` 为 `False`(`finally: _lock.release()` 生效),否则一次异常会永久卡死后续所有检查。

#### `tests/test_app.py`(路由 + 前端结构)

- `test_up_list_check_reports_started_true`(需求 3.16):无检查在跑时 `started is True`、`ok is True`,且后台线程确实调了 `refresh_once`。
- `test_up_list_check_reports_already_running`(Fix Checking B4 / 需求 2.10):monkeypatch `auto_refresh.is_running` 返回 `True`,并给 `refresh_once` 打计数桩 → 断言 `ok is True`、`started is False`、`message` 含「已有检查在进行中」,且 `refresh_once` 调用计数为 0(路由压根没起线程)。
- `test_stats_day_is_calendar_day`(Fix Checking B6 / 需求 2.13):样本 = `{自然日 00:00 + 60}`(今天)与 `{自然日 00:00 − 60}`(昨天 23:59)→ `day_count == 1`。**不断言 `month_count`**,理由见下方稳定性说明。
- `test_frontend_default_list_uses_original_index`(Fix Checking B1):`lib.js` 里存在 `visiblePairs`,其实现对 `state.items.forEach((it, idx)` 建对;且不再出现对过滤后数组取位置的形态(断言 `source.forEach((it, idx)` 与 `filtered.forEach` 都不在 `_tpl()` 里);`renderDefaultList` 与 `advanceDefaultChunk` 都调用 `visiblePairs(`。
- `test_frontend_download_key_shared_rule`(B3 前后端一致性):regex 从 `static/js/util.js` 提取 `DL_KEY_SEP = '…'`,断言等于 `extras.DL_KEY_SEP`;`downloads.js` 出现 `dlKey(bv, t.page || 1)` 与 `dlKey(bv, pageFromUrl(url))`;旧名 `_doneBvids` 不再出现在 `_tpl()` 里。
- `test_frontend_up_check_has_no_fixed_window`(需求 2.10 前端侧):`feed.js` 里出现 `r.started`,且 `upCheckNow` 的实现不再含 `30000`。

#### 稳定性说明:`test_stats_day_is_calendar_day` 为什么不断言 month_count

「昨天 23:59」这条样本在**每月 1 日的 00:00–00:59** 运行时属于**上月**,会让 `month_count` 的期望值随运行时刻变化。把 day 与 month 的断言拆到两条独立测试里,各自的样本集就都恒定:

- 新增的 day 测试:样本 `{day_start + 60, day_start − 60}` → `day_count == 1` 恒成立(不看 month)。
- 既有的 month 测试:样本 `{day_start + 60, month_start − 43200}` → `month_count == 1` 且 `day_count == 1` 恒成立(`day_start + 60` 必在本月)。

---

### Property-Based Tests

本次**不引入 PBT 框架**(项目现有测试栈无此依赖),理由见「Preservation Checking」。以下三条以「参数化用例遍历完整等价类」的形式落地,效果等价于对有限输入域的穷举:

- **B2 归属规则的等价类遍历**:`@pytest.mark.parametrize` 覆盖 (期望名, 磁盘 stem) 的四类组合 —— 精确相等 / 期望名 + 各个合法分隔符后缀(` ` `.` `-` `_` `(` `[` 逐个)/ 期望名后紧跟非分隔符字符 / 完全无关 —— 断言 `_find_actual_file` 的命中结果与 `stem_belongs` 逐一等价。这是 Property 2 的直接实例化。
- **B3 判定粒度的等价类遍历**:参数化 (任务 page, URL 是否带 `?p=`, URL 的 page) 的组合,断言 `is_downloaded` 结果等于 `dl_key` 相等性。同时覆盖 Property 3 与 Property 9。
- **B5 状态集合的全状态遍历**:对 `ACTIVE ∪ TERMINAL` 里的每个状态各造一个任务,断言 `cancel_download` 的返回值恰好等于 `status not in TERMINAL`,且终态任务的 `status` 未被修改。这是 Property 5 + Property 11 的穷举形式,新增状态时会自动被覆盖。

---

### Integration Tests

- **B1 端到端(手工验证,记入验收清单)**:在 6 个受影响页面各做一次「搜索 → 勾选 → 执行」。下载记录页用「删除记录 + 文件」验证只有目标记录与文件消失;其余 5 页用批量下载验证入队的是所勾选的视频。再做一次「先勾选 → 后搜索」,确认高亮集合与真实选择一致。
- **B2 端到端**:`test_align_records_rename_error_when_no_belonging_file` 已覆盖(跑完整 `_run_download` → `align_task_file`),额外确认邻居文件的**副文件**(`.jpg` / `.srt` / `.xml`)也未被改名。
- **B3 端到端**:多 P 视频经 `/api/batch/parse` → `resolve_playlist` 产出 `?p=N` 条目 → 已下载 P1 的情况下提交 P2–P5,断言 `/api/batch/download` 的 `count == 4`(而非提示「所选项均已下载过」)。
- **B4 端到端**:连续两次 POST `/api/up-list/check`,断言第二次 `started is False`;并验证 `arc/search` 桩的调用次数等于「一轮」的量而不是两轮。
- **B5 端到端**:造 `interrupted` 任务 → POST `/api/cancel/<id>` 返回失败 → 状态仍 `interrupted` → POST `/api/download` 同 URL + 清晰度 + 分 P 不返回 409 → POST `/api/tasks/clear` 能清掉该记录。三处一致性一次跑通(需求 2.12)。
- **B6 端到端**:`/api/stats` 的 `day_count` 与 `month_count` 在同一份任务数据下口径一致(昨日样本两者行为一致地按自然周期归属)。

---

### 既有测试的影响清单

这一节是本次修复的**必读项**:有两条既有测试会被修复反转或变得不稳定。

#### 必须修改

**1. `tests/test_downloader.py::test_is_downloaded`** — 会被 B3 修复**反转**。

现有断言:

```python
_mk_task("dd1", status="done", url="https://www.bilibili.com/video/BV1xx411c7mD")   # page=1
assert downloader.is_downloaded("https://www.bilibili.com/video/BV1xx411c7mD?p=3") is True
```

任务是 (BV1, page=1),查询是 (BV1, page=3)。修复后判定为 `False`,该断言必挂。**这条断言本身就是把「只看 BV」的缺陷当成了规格。**

改法:把该行改为断言 `?p=3` 为 `False`(这正是 B3 的 Fix Checking),并补一行不带 `?p=` 的断言为 `True` 以覆盖 3.12:

```python
# 判定粒度是 (BV, 分P):同 BV 的其他分 P 不算已下载
assert downloader.is_downloaded("https://www.bilibili.com/video/BV1xx411c7mD?p=3") is False
assert downloader.is_downloaded("https://www.bilibili.com/video/BV1xx411c7mD") is True    # 3.12
```

并在 docstring 里注明「旧断言曾期望 `?p=3` 为 True,那是缺陷 B3 的表现」。

**2. `tests/test_app.py::test_stats_month_is_calendar_month`** — 会因 B6 修复变得**跨零点不稳定**。

现有的「今天」样本是 `finished_at = int(time.time()) - 3600`。改成自然日口径后,若测试在本地 **00:00–01:00** 之间运行,该时间戳落在昨天(`< day_start`)→ `day_count` 变 0 → `assert r["day_count"] == 1` 失败。

改法:样本时间以自然日起点为基准,而不是「当前时刻往前推」:

```python
today = datetime.date.today()
day_start = int(time.mktime(today.timetuple()))
month_start = int(time.mktime(today.replace(day=1).timetuple()))
monkeypatch.setattr("taskstore.list_tasks", lambda: [
    # 今天。刻意不用 now - 3600:自然日口径下,凌晨 0-1 点跑测试时它会落到昨天
    {"status": "done", "downloaded_bytes": 100, "finished_at": day_start + 60},
    {"status": "done", "downloaded_bytes": 100, "finished_at": month_start - 43200},  # 上月末
])
```

`day_start + 60` 在 00:00–00:01 之间运行时是「未来 1 分钟」,但 `compute_stats` 只做 `finished >= day_start` 比较、不校验未来,结果稳定。month 断言不动。

**残余竞态(记录在测试注释里,不做代码改造)**:测试算 `today` 与 `compute_stats` 算 `today` 之间若正好跨过零点(微秒级窗口),两边 `day_start` 不同,理论上可 flake。彻底消除需要给 `compute_stats` 注入统一时钟——那会为测试在 `taskstore` 里开一道接缝,**本设计不做**。若后续要求确定性,备选方案是在测试里 monkeypatch `taskstore.datetime.date`,把 `today()` 固定住。

#### 建议改写(不改也不会挂)

**3. `tests/test_app.py::test_up_list_check_never_concurrent`**

它 monkeypatch 了 `refresh_once → 0`,所以修复前后都通过——docstring 写着「refresh_once 内部有锁,手动+后台不会叠着打 B 站接口(这里只验路由不炸)」,而它**压根没验到锁**。这正是 B4 躲过 183 个用例的原因之一。

建议改写为不 patch `refresh_once`、用真实锁验证需求 2.9 / 2.10:持 `auto_refresh._lock` 时 POST `/api/up-list/check` → 断言 `started is False`,且 `bapi.fetch_up_videos` 零调用。改写后它与新增的 `test_up_list_check_reports_already_running` 分工为「路由 + 真实锁」与「路由 + 谓词桩」两个层次。

#### 确认继续通过(无需修改)

| 测试 | 为什么不受影响 |
|------|---------------|
| `tests/test_filename_alignment.py::test_run_download_align_glob_fallback` | 落盘名 `标题 [下载].mp4`,期望名 `标题`,余部以空格开头 → `stem_belongs` 命中。**建议**把 docstring 里的「按 stem 前缀」改为「按 stem 归属规则(精确 / 分隔符后缀)」,措辞与实现对齐 |
| `tests/test_app.py::test_up_list_check_runs_refresh_in_background` | patch 了 `refresh_once`,未 patch `is_running`;真实 `_lock` 未被持有 → `is_running()` 为 `False` → 起线程 → 桩被调用 → `ok` 为 `True`。逐条断言均成立 |
| `tests/test_app.py::test_frontend_window_bridge_targets_are_exported` | `upCheckNow` 的名字、`export`、桥条目全部不变 |
| `tests/test_app.py::test_frontend_inline_handlers_are_bridged` | `index.html` 的 `onclick="upCheckNow(this)"` 不变 |
| `tests/test_downloader.py::test_task_state_sets_single_source` | 前端 `TASK_ACTIVE` 与 SSE 终态集合都不改;后端 `ACTIVE` / `TERMINAL` 的取值不变 |
| `tests/test_auto_refresh.py` 全部现有用例 | `refresh_once` 的函数体一字不改,只改锁获取方式;无人持锁时非阻塞获取必然成功,执行路径与返回值完全相同 |
| `tests/test_auto_refresh.py::test_compliance_doc_matches_rate_limits` | `UP_INTERVAL` 与 `config.AUTO_REFRESH_MIN` 均不变 |
| `tests/test_docs.py` 全部用例 | CHANGELOG 新条目写在既有 `## [未发布]` 占位下,版本结构不变 |

---

## 待决策项(需用户确认后才实现)

1. **需求 2.8 的残余缺口**:同一 BV 下新增分 P 时,`arc/search` 不返回分 P 信息,自动检查无法察觉。覆盖它需要对每个新 BV 额外调 `x/player/pagelist` 枚举分 P,给自动检查增加「1 次接口调用 / 视频」,与 B4 正在收紧的风控边界及 `docs/COMPLIANCE.md` 的对外承诺冲突。**本次不实现**,是否扩范围请确认。
2. **`/api/up-list/check` 响应的严格一致性**:本设计接受「`is_running()` 检查与线程启动之间的极窄窗口内,响应可能乐观地报 `started: True` 而实际被跳过」。风控要的不变量(绝不并发、绝不排队)在任何情况下都成立。若要求响应与实际严格一致,需改为「请求线程 try-acquire、worker 在 `finally` 释放」的跨线程持锁方案,请确认是否需要。

---

## 文档与变更记录

- **`CHANGELOG.md`**:在 `## [未发布]` 下新增一条 `### 🐛 修复(Fixed)`,6 条缺陷各一段,写清用户可感知的后果与根因(沿用 v2.1.1 小节的写法密度)。需额外记录三件事:B2 的退化取舍(少数异常场景由静默误改邻居文件变为可见的 `rename_error`)、B3 的 2.8 部分成立范围、`_stem_belongs` → `stem_belongs` 的改名。
- **`docs/REQUIREMENTS.md`**:底部的缺陷复盘表增 6 行(缺陷 / 根因 / 修复方式),与既有 v2.1 各行同格式。`/api/up-list/check` 的接口说明补上 `started` 字段语义。
- **`docs/COMPLIANCE.md`**:B4 收紧了手动检查的实际行为(不再排队叠加),在自动任务限频小节补一句「进行中再次触发直接跳过,不排队」。`UP_INTERVAL` 与 `AUTO_REFRESH_MIN` 的数字不变,`test_compliance_doc_matches_rate_limits` 不受影响。
