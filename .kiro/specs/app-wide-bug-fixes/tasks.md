# Implementation Plan

按严重度排序执行:B1 → B2 → B3 → B4 → B5 → B6。B1 / B2 造成不可逆数据损失(删错文件、误改邻居视频文件名),必须排在最前。

每条缺陷内部固定四步:**先写能复现缺陷的测试(此时必须失败)→ 改实现 → 跑测试转绿 → 跑全量回归**。任何一条缺陷的探索性测试若没能在未修复代码上失败,说明根因假设被推翻,回到 design.md 的「Hypothesized Root Cause」重新分析,不得直接改代码。

代码形态已在 design.md 的「Fix Implementation」逐条给出,任务里只引用小节、不重复贴代码。

## 范围边界(执行前必读)

### 明确不实现(design.md 已论证,勿扩范围)

1. **需求 2.8 的残余缺口**——同一 BV 下新增分 P 时 `arc/search` 不返回分 P 信息,自动检查无法察觉。覆盖它需给每个新 BV 额外调一次 `x/player/pagelist` 枚举分 P,等于给自动检查增加「1 次接口调用 / 视频」,直接冲撞 B4 正在收紧的风控边界与 `docs/COMPLIANCE.md` 的对外承诺。**本次交付 2.6 / 2.7 完整、2.8 部分成立**(已完成任务是非 1 分 P 时成立,是 P1 时不成立),依据 design.md「B3 → 7. 需求 2.8 的可达性」。
2. **`/api/up-list/check` 响应的严格一致性**——采纳 design.md「B4 → 2. app.py」接受的乐观回报方案:`is_running()` 与线程启动之间存在极窄窗口,响应可能报 `started: True` 而实际被跳过。风控真正需要的不变量(绝不并发、绝不排队)在任何情况下都成立。**不实现**跨线程持锁(请求线程 try-acquire、worker 在 `finally` 释放)方案。
3. **不引入 PBT 框架**——项目现有测试栈无此依赖,不为此新增。Property 2 / 3 / 5 用 `@pytest.mark.parametrize` 遍历**完整等价类**落地,对有限输入域等价于穷举,依据 design.md「Property-Based Tests」。

### 既有测试的处置(design.md「既有测试的影响清单」)

| 测试 | 处置 | 归属任务 |
|------|------|----------|
| `tests/test_downloader.py::test_is_downloaded` | **必须改**:`?p=3` 断言会被 B3 修复反转 | 7 |
| `tests/test_app.py::test_up_list_check_never_concurrent` | **必须改写**:patch 掉 `refresh_once` 因此压根没验到锁 | 10 |
| `tests/test_app.py::test_stats_month_is_calendar_month` | **必须改**:样本改用自然日起点基准,避免凌晨 flake | 17 |
| `tests/test_filename_alignment.py::test_run_download_align_glob_fallback` | 仅 docstring 措辞对齐,断言不动 | 6.4 |

---

## 任务列表

### B1 搜索过滤后勾选下标错位(高危 · 不可逆数据丢失)

- [x] 1. 写 B1 缺陷复现测试(搜索态发出的是命中序号而非原始下标)
  - **Property 1: Bug Condition** - 勾选下标必须能定位回它所代表的条目
  - **CRITICAL**: 本测试必须在未修复代码上 FAIL,失败即证明缺陷存在
  - **DO NOT** 在它失败时去改测试或迁就现有实现
  - 在 `tests/test_app.py` 新增 `test_frontend_default_list_uses_original_index`:断言 `_tpl()` 里存在 `visiblePairs`、其实现对 `state.items.forEach((it, idx)` 建对,且 `source.forEach((it, idx)` 与 `filtered.forEach` 都已不在拼接串里;`renderDefaultList` 与 `advanceDefaultChunk` 都调用 `visiblePairs(`
  - 未修复现场:`static/js/lib.js:128` 为 `source.forEach((it, idx) => pairs.push({ it, idx }));`,断言必挂
  - 手工复现(前端无 JS 运行时,行为侧证据靠这一步):造 4 条记录、搜索命中后两条、勾选第一个命中项,读 DOM 的 `data-libidx` —— 未修复时是 `0`,而该条目在 `state.items` 里是第 2 个
  - **EXPECTED OUTCOME**: 测试 FAIL;手工复现看到 `delSelected()` 会取到 `state.items[0]`
  - 记录反例后即可标记完成
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 2. 写 B1 保持测试(无搜索词路径与勾选守卫逐位不变)
  - **Property 7: Preservation** - 无搜索词时的勾选与批量操作
  - **IMPORTANT**: 遵循观察优先方法 —— 先在未修复代码上观察行为,再写断言
  - 观察:未修复代码在 `q === ''` 时 `source` 是 `state.items.slice(0, state.shown)`,`idx` 恰等于原始下标(巧合正确)
  - 在 `tests/test_app.py` 断言结构性不变量:`visiblePairs('')` 的语义等价于 `state.items.slice(0, state.shown).map((it, idx) => ({it, idx}))`;`cachedLoad()` 的快源无感替换守卫表达式 `state.checked.size === 0` 仍存在;`renderLibList` 仍用 `data-libidx` / `toggleLibCheck` / `state.checked.has(origIdx)`
  - 在未修复代码上运行
  - **EXPECTED OUTCOME**: 守卫与消费方相关断言 PASS(它们本就正确,是要锁住的基线)
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. 修复 B1:`renderDefaultList()` 携带原始下标

  - [x] 3.1 在 `static/js/lib.js` 新增 `matchesQuery()` 与导出 `visiblePairs()`
    - 纯新增,先不改任何调用方,便于独立验证文件仍可解析、导出可见
    - `visiblePairs(q)` 遍历 `state.items` 本身,`idx` 天然是原始下标;搜索态按谓词筛选,非搜索态按 `idx < state.shown` 截断
    - 注释写明「曾对过滤后的数组 forEach 取位置当下标,搜索态下删除 / 下载会打到另一条记录(含 unlink 磁盘文件,不可恢复)」
    - 代码形态见 design.md「Fix Implementation → B1 携带原始下标」第 1 条
    - _Bug_Condition: isBugCondition_B1(input) —— query ≠ "" 且 filter(items, query) ≠ items_
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.2 `renderDefaultList()` 改用 `visiblePairs()`,并前移 `state.shown` 初始化
    - `if (!state.shown) state.shown = curChunk();` **必须移到 `visiblePairs()` 调用之前**(原代码在 `filtered` 之后),否则非搜索态首次渲染 `idx < 0` 恒假、列表会空
    - meta 的「匹配 N」改取 `pairs.length`,不再单独维护 `filtered`
    - 代码形态见 design.md「Fix Implementation → B1」第 2 条
    - _Bug_Condition: isBugCondition_B1(input) from design_
    - _Expected_Behavior: state.items[emitted] IS visibleItem(Property 1)_
    - _Preservation: Property 7 —— q === '' 时 pairs 与修复前逐位相同_
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3_

  - [x] 3.3 `advanceDefaultChunk()` 复用 `visiblePairs()`,消灭第二份 pairs 构造
    - 原实现 `state.items.slice(0, state.shown).map(...)` 结果正确,但是同一规则的第二份写法
    - 不改成调 `renderDefaultList()`,避免 `renderDefaultList → maybeFillScreen → advanceDefaultChunk` 的间接递归
    - _Preservation: Property 7 —— 无限滚推进路径行为不变_
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 3.4 验证 B1 缺陷复现测试转绿
    - **Property 1: Bug Condition** - 勾选下标必须能定位回它所代表的条目
    - **IMPORTANT**: 重跑任务 1 的**同一个**测试,不要新写测试
    - **EXPECTED OUTCOME**: 测试 PASS(确认缺陷已修)
    - 确认无需改动 `renderLibList` / `delSelected` / `libDownload` / `toggleLibCheck` / `libToggleAll`(它们的原始下标语义本来就对),也无需逐页改 6 个受影响页面
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.5 验证 B1 保持测试仍绿
    - **Property 7: Preservation** - 无搜索词时的勾选与批量操作
    - **IMPORTANT**: 重跑任务 2 的**同一批**测试,不要新写
    - **EXPECTED OUTCOME**: 全部 PASS(无回归)
    - 跑 `.venv/bin/pytest -q tests/test_app.py` 确认该文件全绿
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

### B2 落盘文件兜底定位用 10 字前缀匹配(高危 · 不可逆数据损坏)

- [x] 4. 写 B2 缺陷复现测试(邻居视频文件被误认并改名)
  - **Property 2: Bug Condition** - 兜底匹配与 stem 归属规则等价
  - **CRITICAL**: 本测试必须在未修复代码上 FAIL
  - 在 `tests/test_filename_alignment.py` 新增,`monkeypatch.setattr(files.config, "DOWNLOAD_DIR", tmp_path)`,清空 `_final_paths`:
    - `test_find_actual_file_rejects_prefix_neighbour`:期望名 `第01期`,`tmp_path` 只放 `第01期特别版.mp4` → 应返回 `None`、邻居文件名逐字未变
    - `test_find_actual_file_rejects_short_name_prefix`:期望名 `测试`,盘上 `测试2.mp4` → 应不命中
    - `test_align_records_rename_error_when_no_belonging_file`:上述情形跑完整 `_run_download` → 断言 `status == "done"`、`rename_error` 非空、邻居文件及其 `.jpg` / `.srt` / `.xml` 副文件名逐字未变(锁住「退化为可见 `rename_error` 不影响完成状态」这一设计取舍)
  - **Property 2 的等价类遍历**(替代 PBT):`@pytest.mark.parametrize` 覆盖 (期望名, 磁盘 stem) 四类 —— 精确相等 / 期望名 + 各合法分隔符后缀(` ` `.` `-` `_` `(` `[` 逐个)/ 期望名后紧跟非分隔符字符 / 完全无关 —— 断言 `_find_actual_file` 命中结果与 `stem_belongs` 逐一等价
  - 未修复现场:`files.py:62` 为 `prefix = [f for f in media if f.stem.startswith(expected_stem[:10])]`
  - **EXPECTED OUTCOME**: 前缀 / 短名两条 FAIL(返回邻居文件的 `Path` 而非 `None`);跑 `align_task_file` 可见邻居文件被改名成期望名
  - _Requirements: 2.4, 2.5_

- [x] 5. 写 B2 保持测试(权威路径优先与合法后缀命中)
  - **Property 8: Preservation** - 权威路径优先与合法后缀命中
  - **IMPORTANT**: 遵循观察优先方法
  - 观察并断言未修复代码的既有正确行为:pp 钩子已捕获权威路径且文件存在 → 不扫盘;stem 与期望名精确相等 → 命中;`标题 [下载].mp4` / ` - P3` / `.zh-Hans` / ` (1)` 形态 → 命中;同 stem 副文件整族覆盖;目标已存在且非同一文件 → 按同大小 / 不同大小写 `rename_skipped` / `rename_conflict`
  - `tests/test_filename_alignment.py` 既有用例已覆盖上述全部,确认其在未修复代码上全绿即为基线
  - **EXPECTED OUTCOME**: 全部 PASS
  - _Requirements: 3.7, 3.8, 3.9, 3.10, 3.11_

- [x] 6. 修复 B2:兜底分支复用 stem 归属规则

  - [x] 6.1 `taskstore.py`:`_stem_belongs` → `stem_belongs`,保留旧名别名并同步内部引用
    - `taskstore.py:115` 的 `def _stem_belongs` 改名为 `def stem_belongs`,docstring 写明「全项目唯一来源:`taskstore._task_files`(定位 / 删除)与 `files._find_actual_file`(下载后改名对齐)共用」
    - 文件内加一行 `_stem_belongs = stem_belongs  # 旧名保留,避免下游 import 断裂`(与同文件已有的 `_ACTIVE = ACTIVE` 同一处理方式)
    - 同步 `taskstore.py:168` 的 `_task_files` 调用点为新名
    - 同步 `taskstore.py:145` 的 `_task_files` docstring:「匹配规则见 `_stem_belongs`」→ 「匹配规则见 `stem_belongs`」
    - CHANGELOG 历史小节里对旧名的引用**不改动**(那是当时的事实记录,改名会篡改历史)
    - 新增 `test_stem_belongs_public_alias`:`taskstore.stem_belongs is taskstore._stem_belongs`
    - _Requirements: 2.4, 2.5, 3.9, 3.10_

  - [x] 6.2 `files.py`:import `stem_belongs` 并替换 `[:10]` 兜底谓词
    - 顶部 `from taskstore import ...` 追加 `stem_belongs`(依赖方向已是 `files.py → taskstore`,**不在 files.py 里复制第三份匹配规则**)
    - `files.py:62` 的前缀分支改为 `belongs = [f for f in media if stem_belongs(f.stem, expected_stem)]`
    - **保留 `exact` 这一级**、不与 `belongs` 合并:合并后 `max(size)` 可能选到体积更大的带后缀文件而非精确同名文件,3.8 会被破坏
    - 注释写明旧写法的失败模式(共享前 10 字的邻居视频被误认并改名,短名时是整名无边界前缀)
    - 代码形态见 design.md「Fix Implementation → B2 兜底分支复用 stem 归属规则」第 2 条
    - 已知并接受的退化:名字被截断 / 第 11 字后被替换这两类异常落盘形态改为返回 `None` → 写 `rename_error`(可见、可恢复、不影响 `done` 状态),换掉旧行为的不可逆邻居文件误改,理由见 design.md「改用 `stem_belongs` 后会退化的合法场景」
    - 新增 `test_find_actual_file_shares_stem_rule`:`files.py` 源码不含 `[:10]` 切片,且从 `taskstore` import 了 `stem_belongs`
    - _Bug_Condition: isBugCondition_B2(input) —— 未捕获权威路径 且 diskStem ≠ expectedStem 且 共享前 10 字_
    - _Expected_Behavior: (matched = diskStem) ⟺ stem_belongs(diskStem, expectedStem)(Property 2)_
    - _Preservation: Property 8 —— 权威路径优先 / 精确同名 / 合法后缀 / 冲突分支不变_
    - _Requirements: 2.4, 2.5_

  - [x] 6.3 验证 B2 缺陷复现测试转绿
    - **Property 2: Bug Condition** - 兜底匹配与 stem 归属规则等价
    - **IMPORTANT**: 重跑任务 4 的**同一批**测试(含 parametrize 等价类遍历),不要新写
    - **EXPECTED OUTCOME**: 全部 PASS
    - _Requirements: 2.4, 2.5_

  - [x] 6.4 验证 B2 保持测试仍绿,并对齐既有 docstring 措辞
    - **Property 8: Preservation** - 权威路径优先与合法后缀命中
    - 重跑任务 5 的同一批测试,**EXPECTED OUTCOME**: 全部 PASS
    - `tests/test_filename_alignment.py::test_run_download_align_glob_fallback` 的**断言不动**(落盘名 `标题 [下载].mp4`、期望名 `标题`,余部以空格开头 → `stem_belongs` 命中);仅把 docstring 里的「按 stem 前缀」改为「按 stem 归属规则(精确 / 分隔符后缀)」,措辞与实现对齐
    - 跑 `.venv/bin/pytest -q tests/test_filename_alignment.py tests/test_downloader.py` 确认全绿
    - _Requirements: 3.7, 3.8, 3.9, 3.10, 3.11_

### B3 「已下载」判定只看 BV 号(中 · 主场景不可用)

- [x] 7. 写 B3 缺陷复现测试(同 BV 不同分 P 被误判已下载)
  - **Property 3: Bug Condition** - 已下载判定的粒度是 (bvid, page)
  - **CRITICAL**: 本批测试必须在未修复代码上 FAIL
  - **修改既有测试** `tests/test_downloader.py::test_is_downloaded`(约 204 行):它的 `assert downloader.is_downloaded(".../BV1xx411c7mD?p=3") is True` 会被本次修复反转 —— **这条断言本身把缺陷当成了规格**。改为断言 `?p=3` 为 `False`(这正是 B3 的 Fix Checking),并补一行不带 `?p=` 的断言为 `True` 覆盖 3.12;docstring 注明「旧断言曾期望 `?p=3` 为 True,那是缺陷 B3 的表现」
  - 在 `tests/test_downloader.py` 新增:
    - `test_is_downloaded_is_per_page`:done (BV1, page=1) → 不带 `?p=` 为 `True`、`?p=2` 为 `False`;另造 done (BV1, page=3) → `?p=3` 为 `True`
    - `test_is_downloaded_uses_task_page_field_not_task_url`:任务 `url` 不带 `?p=` 但 `page=3`(即 `start_download(url, label, page=3)` 的真实形态)→ `?p=3` 判 `True`、不带 `?p=` 判 `False`。锁住「任务侧读 `t["page"]`、不解析任务 URL」这个必须不对称的取法
  - **Property 3 的等价类遍历**(替代 PBT):`@pytest.mark.parametrize` 覆盖 (任务 page, URL 是否带 `?p=`, URL 的 page) 组合,断言 `is_downloaded` 结果等于 `dl_key` 相等性(同时覆盖 Property 9)
  - 在 `tests/test_extras.py` 新增 `test_page_from_url`(`?p=3`→3;无 `?p=`→1;`&p=2`→2;`?p=abc`→1;`""` / `None`→1)与 `test_dl_key_format`(`dl_key("BV1xx411c7mD", 3) == "BV1xx411c7mD#p3"`;`0` / `None` 归一为 `#p1`;`DL_KEY_SEP == "#p"`)
  - **前后端键规则一致性测试**:在 `tests/test_app.py` 新增 `test_frontend_download_key_shared_rule` —— 用 regex 从 `static/js/util.js` 提取 `DL_KEY_SEP = '…'` 的字面值并断言等于后端 `extras.DL_KEY_SEP`(沿用 `TASK_ACTIVE ↔ taskstore.ACTIVE` 的绑定断言做法);同时断言 `downloads.js` 出现 `dlKey(bv, t.page || 1)` 与 `dlKey(bv, pageFromUrl(url))`,且旧名 `_doneBvids` 不再出现在 `_tpl()` 里 —— 这样「少了 page 维度」与「两侧取 page 的方式写反」都会被抓到
  - 未修复现场:`taskstore.is_downloaded` 只比 `bvid_from_url`;`static/js/pages/downloads.js:16` 为 `let _doneBvids = new Set();`;`extras.DL_KEY_SEP` 与 `util.js` 的 `DL_KEY_SEP` 均不存在
  - **EXPECTED OUTCOME**: 全部 FAIL(反例:同 BV 不同分 P 被判为已下载;键规则常量缺失)
  - _Requirements: 2.6, 2.7, 2.8_

- [x] 8. 写 B3 保持测试(单 P 与同分 P 的判定不变)
  - **Property 9: Preservation** - 单 P 与同分 P 的已下载判定
  - **IMPORTANT**: 遵循观察优先方法
  - 观察并断言未修复代码的既有正确行为:不带 `?p=` 的 URL + 任务 `page=1` → `True`;(BV, page) 完全相同 → `True`;短链 / av 号提取不到 BV → `False`;任务状态非 `done` → `False`
  - 在未修复代码上运行
  - **EXPECTED OUTCOME**: 全部 PASS(这些是覆盖绝大多数单 P 场景的基线,修复后必须逐位相同)
  - _Requirements: 3.12, 3.13, 3.14, 3.15_

- [x] 9. 修复 B3:已下载判定改为 (bvid, page) 粒度

  - [x] 9.1 `extras.py` 新增键规则(依赖链底部)
    - 新增 `DL_KEY_SEP = "#p"`、`page_from_url(url) -> int`(无 `?p=` / 非法值视为 1)、`dl_key(bvid, page) -> str`
    - 放 `extras.py` 而非 `taskstore.py`:与 `bvid_from_url` / `build_filename` 同层,`taskstore` 已 `import extras`,`auto_refresh` 经 `downloader` 间接复用同一份;纯字符串逻辑必须待在依赖链底部
    - `DL_KEY_SEP` 注释写明「前端 `static/js/util.js` 的 `DL_KEY_SEP` 必须与此一致,tests 有绑定断言 —— 两侧键规则漂移会让角标与批量跳过各说各话,且不会报错」
    - 代码形态见 design.md「Fix Implementation → B3」第 1 节
    - _Requirements: 2.6, 2.7_

  - [x] 9.2 `taskstore.is_downloaded()` 改用 `dl_key`
    - 查询侧 page 从 URL 的 `?p=` 解析(入参只有 URL);任务侧 page 读 `t["page"]`,**不能解析 `t["url"]`** —— `start_download(url, label, page=3)` 存进任务表的 `url` 是用户粘贴的原始链接(通常不带 `?p=`),`page` 才是权威值。docstring 写明「两侧取法不对称是刻意的」
    - 锁内只保留纯字符串运算(与现状同级),不引入 IO、不调 `_persist()`
    - 代码形态见 design.md「Fix Implementation → B3」第 2 节
    - _Bug_Condition: isBugCondition_B3(input) —— 同 BV 有 done 但分 P 不同,且本分 P 自己无 done_
    - _Expected_Behavior: is_downloaded'(url) = False(Property 3)_
    - _Preservation: Property 9 —— 无 ?p= + 任务 page=1、同 (BV, page)、提取不到 BV、非 done 状态,结果不变_
    - _Requirements: 2.6, 2.7, 2.8, 3.12, 3.13, 3.14, 3.15_

  - [x] 9.3 `static/js/util.js` 新增前端键规则(与 `bvFromUrl` 同层)
    - 导出 `DL_KEY_SEP = '#p'`、`pageFromUrl(url)`、`dlKey(bv, page)`,语义与后端逐字对应
    - 注释写明「必须与后端 `extras.DL_KEY_SEP` 一致(tests 有绑定断言)」
    - 代码形态见 design.md「Fix Implementation → B3」第 3 节
    - _Requirements: 2.6, 2.7_

  - [x] 9.4 `static/js/pages/downloads.js` 改用 (BV, 分 P) 键集合
    - `_doneBvids` → `_doneKeys`;新增 `doneKeySet(tasks)` 作为集合构建的唯一来源;`isDownloaded(url)` 改查 `dlKey(bv, pageFromUrl(url))`
    - `loadDownloads()`(约 29 行)与 `pollTasks()`(约 102 行)里各有一份 `new Set(tasks.filter(...).map(bvFromUrl)...)`,**两处都改为 `_doneKeys = doneKeySet(tasks);`**,消灭重复
    - 任务侧 page 读 `t.page`(`/api/tasks` 的任务对象已含该字段),不能从 `t.url` 解析
    - 代码形态见 design.md「Fix Implementation → B3」第 4 节
    - 核对 design.md「Fix Implementation → B3」第 5 节的调用点清单已全部覆盖:`auto_refresh.refresh_once` / 列表页角标(`lib.isUrlDownloaded`)/ `downloads.libDownload` / `newdl.batchDownload`(1.7 的真实现场);后端重复下载拦截 `find_active_by_url` 本来就按 (URL, 清晰度, 分 P) 判定,**不在本次改动范围**
    - _Requirements: 2.6, 2.7_

  - [x] 9.5 验证 B3 缺陷复现测试转绿
    - **Property 3: Bug Condition** - 已下载判定的粒度是 (bvid, page)
    - **IMPORTANT**: 重跑任务 7 的**同一批**测试(含已修改的 `test_is_downloaded`、等价类遍历、前后端键规则一致性),不要新写
    - **EXPECTED OUTCOME**: 全部 PASS
    - 端到端确认:多 P 视频经 `/api/batch/parse` → `resolve_playlist` 产出 `?p=N` 条目,已下载 P1 的情况下提交 P2–P5,`/api/batch/download` 的 `count == 4`(而非提示「所选项均已下载过」)
    - **不实现** 2.8 的残余缺口(见「范围边界」第 1 条):已完成任务是该 BV 的 P1、新分 P 是同 BV 下新增 P2 时,`arc/search` 不带分 P 信息 → 仍判已下载 → 跳过。本次交付 2.6 / 2.7 完整、2.8 部分成立
    - _Requirements: 2.6, 2.7, 2.8_

  - [x] 9.6 验证 B3 保持测试仍绿
    - **Property 9: Preservation** - 单 P 与同分 P 的已下载判定
    - 重跑任务 8 的同一批测试,**EXPECTED OUTCOME**: 全部 PASS
    - 跑 `.venv/bin/pytest -q tests/test_extras.py tests/test_downloader.py tests/test_app.py` 确认全绿
    - _Requirements: 3.12, 3.13, 3.14, 3.15_

### B4 手动检查会排队叠加多轮(中 · 风控风险)

- [x] 10. 写 B4 缺陷复现测试(后来者阻塞排队并真的发请求)
  - **Property 4: Bug Condition** - 后来者跳过而非排队
  - **CRITICAL**: 本批测试必须在未修复代码上 FAIL
  - **改写既有测试** `tests/test_app.py::test_up_list_check_never_concurrent`(约 107 行):它 monkeypatch 了 `refresh_once → 0`,所以修复前后都通过 —— docstring 写着「只验路由不炸」,**压根没验到锁,这是 B4 躲过 183 个用例的原因之一**。改写为不 patch `refresh_once`、用真实锁验证 2.9 / 2.10:主线程持 `auto_refresh._lock` 时 POST `/api/up-list/check` → 断言 `started is False`,且 `bapi.fetch_up_videos` 零调用
  - 在 `tests/test_auto_refresh.py` 新增:
    - `test_refresh_once_skips_when_already_running`:主线程持 `auto_refresh._lock` 后调 `refresh_once()` → 立即返回 `0`,且 `up_list.list_ups` 与 `bapi.fetch_up_videos` 调用计数均为 0(证明「未发起任何 B 站请求」)
    - `test_is_running_reflects_lock`:未持锁 `False`、持锁 `True`、释放后回 `False`
    - `test_refresh_once_releases_lock_on_exception`:让 `up_list.list_ups` 抛异常,断言异常传播后 `is_running()` 为 `False`(`finally: _lock.release()` 生效)—— 否则一次异常会永久卡死后续所有检查
  - 在 `tests/test_app.py` 新增:
    - `test_up_list_check_reports_already_running`:monkeypatch `auto_refresh.is_running → True`、给 `refresh_once` 打计数桩 → 断言 `ok is True`、`started is False`、`message` 含「已有检查在进行中」,且 `refresh_once` 计数为 0(路由压根没起线程)
    - `test_frontend_up_check_has_no_fixed_window`:`feed.js` 出现 `r.started`,且 `upCheckNow` 实现不再含 `30000`
  - 未修复现场:`auto_refresh.py:78` 为阻塞的 `with _lock:`;路由无条件 `return jsonify({"ok": True})`;`static/js/pages/feed.js:219` 有 `setTimeout(..., 30000)` 假禁用窗口
  - **EXPECTED OUTCOME**: 全部 FAIL(反例:后来者阻塞排队并在前一轮结束后完整再跑一遍,期间真实发起 B 站请求;`is_running` / `started` 不存在)
  - _Requirements: 2.9, 2.10_

- [x] 11. 写 B4 保持测试(单轮检查的执行语义不变)
  - **Property 10: Preservation** - 单轮检查的执行语义
  - **IMPORTANT**: 遵循观察优先方法
  - 观察并确认未修复代码的既有行为:无人在跑时起一轮并立即返回不阻塞前端;UP 之间 sleep 间隔不少于 5 秒;某 UP 拉取返回空(疑似限流)不推进 `last_seen`;后台轮询按配置间隔周期触发;单 UP 异常只记日志不中断整轮;返回值为入队条数
  - `tests/test_auto_refresh.py` 既有用例已全覆盖上述行为(它们验证的是 `refresh_once` 的**函数体**,而本次只改锁获取方式),确认其在未修复代码上全绿即为基线
  - **EXPECTED OUTCOME**: 全部 PASS
  - _Requirements: 3.16, 3.17, 3.18, 3.19, 3.20_

- [x] 12. 修复 B4:检查触发改为跳过而非排队

  - [x] 12.1 `auto_refresh.py`:新增 `is_running()` + 锁改非阻塞获取
    - 新增 `is_running() -> bool` 返回 `_lock.locked()`,供路由如实回报
    - `refresh_once()` 的 `with _lock:` 改为 `if not _lock.acquire(blocking=False): log.info(...); return 0` + `try: ... finally: _lock.release()`
    - **函数体一字不改**(UP 遍历、间隔 sleep、限流不推进、异常兜底、返回入队条数)
    - 返回值契约不变(仍是 `int`),因此两个既有调用方(`app.py` 后台线程、`start_loop()` 轮询)**都不需要改**:跳过时打「入队 0 条」名实相符,`if n:` 不打日志语义正确
    - 「是否在跑」通过新增谓词暴露,**不**把 skipped 编码进返回值(如 `-1` 会被 `if n:` 当真值,必须同步改两个调用方)
    - 锁序 `_lock → _tasks_lock` 单向,`taskstore` 不认识 `auto_refresh`,无死锁风险
    - 代码形态见 design.md「Fix Implementation → B4」第 1 节
    - _Bug_Condition: isBugCondition_B4(input) —— 已有一轮 refresh 正在运行_
    - _Expected_Behavior: 零 B 站请求 + 如实告知(Property 4)_
    - _Preservation: Property 10 —— 无人在跑时执行路径与返回值完全相同_
    - _Requirements: 2.9, 3.16, 3.17, 3.18, 3.19, 3.20_

  - [x] 12.2 `app.py`:`/api/up-list/check` 如实回报 `started`
    - `auto_refresh.is_running()` 为真时直接返回 `{"ok": True, "started": False, "message": "已有检查在进行中,本次未启动新一轮"}`,不起线程
    - 否则起后台线程并返回 `{"ok": True, "started": True, "message": "已在后台开始检查"}`
    - `ok` 保持 `True`(「已有检查在进行中」不是错误,是如实告知),既有 `assert r.get_json()["ok"]` 不受影响
    - docstring 记录接受的乐观回报竞态(见「范围边界」第 2 条):`is_running()` 与线程启动之间的极窄窗口内响应可能报 `started: True` 而实际被跳过;`refresh_once` 的非阻塞获取保证绝不并发、绝不排队
    - 代码形态见 design.md「Fix Implementation → B4」第 2 节
    - _Expected_Behavior: 响应 started = False(Property 4)_
    - _Requirements: 2.10, 3.16_

  - [x] 12.3 `static/js/pages/feed.js`:删除 30 秒假禁用窗口,按 `started` 给反馈
    - 删除 `feed.js:219` 的 `setTimeout(..., 30000)`;按钮只在请求在途期间禁用
    - `r.started === false` 时提示「已有检查在进行中,本次未启动新一轮」,否则提示「已在后台开始检查」
    - **必须保持前端桥兼容**:函数名 `upCheckNow` 不变、`export` 不变、参数仍是 `(btn)`;`templates/index.html` 的 `onclick="upCheckNow(this)"` 不变;`static/js/app.js` 的 window 桥条目 `upCheckNow: feed.upCheckNow` 不变 → `test_frontend_window_bridge_targets_are_exported` 与 `test_frontend_inline_handlers_are_bridged` 自然继续通过,**无需改桥**
    - 代码形态见 design.md「Fix Implementation → B4」第 3 节
    - _Requirements: 2.10_

  - [x] 12.4 验证 B4 缺陷复现测试转绿
    - **Property 4: Bug Condition** - 后来者跳过而非排队
    - **IMPORTANT**: 重跑任务 10 的**同一批**测试(含改写后的 `test_up_list_check_never_concurrent`),不要新写
    - **EXPECTED OUTCOME**: 全部 PASS
    - 端到端确认:连续两次 POST `/api/up-list/check`,第二次 `started is False`,且 `arc/search` 桩的调用次数等于「一轮」的量而不是两轮
    - _Requirements: 2.9, 2.10_

  - [x] 12.5 验证 B4 保持测试仍绿
    - **Property 10: Preservation** - 单轮检查的执行语义
    - 重跑任务 11 的同一批测试,**EXPECTED OUTCOME**: 全部 PASS
    - 确认 `test_up_list_check_runs_refresh_in_background` 与 `test_compliance_doc_matches_rate_limits` 继续通过(前者未 patch `is_running`、真实 `_lock` 未被持有;后者 `UP_INTERVAL` 与 `config.AUTO_REFRESH_MIN` 均不变)
    - 跑 `.venv/bin/pytest -q tests/test_auto_refresh.py tests/test_app.py` 确认全绿
    - _Requirements: 3.16, 3.17, 3.18, 3.19, 3.20_

### B5 终态集合硬编码副本漏 interrupted(低 · 状态机一致性)

- [x] 13. 写 B5 缺陷复现测试(interrupted 被拉回活跃态成僵尸)
  - **Property 5: Bug Condition** - 终态任务不可被拉回活跃态
  - **CRITICAL**: 本批测试必须在未修复代码上 FAIL
  - 在 `tests/test_downloader.py` 新增(沿用 `_mk_task(...)` + `finally: pop` 的既有写法):
    - `test_cancel_rejects_terminal_states`:`interrupted` 取消返回 `False` 且状态仍为 `interrupted`;`done` / `failed` / `cancelled` 同样返回 `False` 且状态不变
    - `test_interrupted_task_not_blocking_and_clearable`:`interrupted` 任务下 `find_active_by_url(url, label, page)` 返回 `None`;`clear_tasks(delete_file=False)` 能把它清掉
    - `test_terminal_checks_use_single_source`:用 `inspect.getsource`(与该文件既有静态分析风格一致)断言 `downloader.py` 源码不再出现字面元组 `("done", "failed", "cancelled")`,且 `cancel_download` / `_finalize_cancelled` 的源码都出现 `TERMINAL`
  - **Property 5 的全状态遍历**(替代 PBT):对 `ACTIVE ∪ TERMINAL` 里每个状态各造一个任务,`@pytest.mark.parametrize` 断言 `cancel_download` 返回值恰好等于 `status not in TERMINAL`,且终态任务 `status` 未被修改(同时覆盖 Property 11,新增状态时自动被覆盖)
  - 未修复现场:`downloader.py:379`(`cancel_download`)与 `downloader.py:163`(`_finalize_cancelled`)各有一份漏 `interrupted` 的硬编码三元组
  - **EXPECTED OUTCOME**: FAIL(反例:`interrupted` 取消返回 `True` 且状态变成 `cancelling`,随后 `find_active_by_url` 拦截同 URL 新下载、`clear_tasks` 清不掉它)
  - _Requirements: 2.11, 2.12_

- [x] 14. 写 B5 保持测试(活跃态与其余终态语义不变)
  - **Property 11: Preservation** - 活跃态与其余终态的取消语义
  - **IMPORTANT**: 遵循观察优先方法
  - 观察并断言未修复代码的既有正确行为:新增 `test_cancel_preserves_active_states` —— `queued` / `downloading` / `merging` / `retrying` 取消返回 `True`、状态变 `cancelling`、`cancel` 标志置位;`done` / `failed` / `cancelled` 取消返回 `False` 且状态不变;前端 `TASK_ACTIVE` 与 SSE 终态集合仍与后端一致
  - 在未修复代码上运行
  - **EXPECTED OUTCOME**: 全部 PASS(`("done","failed","cancelled")` 是 `TERMINAL` 的真子集,唯一差集是 `interrupted`,故这些输入新旧结果必然相同)
  - _Requirements: 3.21, 3.22, 3.23, 3.24_

- [x] 15. 修复 B5:终态判断复用 `taskstore.TERMINAL`

  - [x] 15.1 `downloader.py`:import `TERMINAL` 并替换两处硬编码副本
    - 顶部 `from taskstore import ...` 追加 `TERMINAL`(与已有一批同源名字同样方式引入)
    - `cancel_download()`(约 379 行):`t["status"] in ("done","failed","cancelled")` → `t["status"] in TERMINAL`;docstring 写明旧写法漏 `interrupted` 导致的永久僵尸态及其下游后果
    - `_finalize_cancelled()`(约 163 行):`t["status"] not in ("done","failed","cancelled")` → `not in TERMINAL`
    - `_persist()` 仍在锁外调用(现状已如此),锁纪律不变
    - 3.23 一致性已核对:改用 `TERMINAL` 后 `interrupted` 被视为已终结、不会被覆盖成 `cancelled`,与 3.23 字面一致;**旧三元组写法反而违反 3.23**(它会把已终结的 `interrupted` 覆盖成 `cancelled`)
    - 2.12 三处一致性已核对(取消 → `TERMINAL`、重复下载拦截 → `ACTIVE`、清除全部记录 → `_ACTIVE`),1.12 的两个后果都是「被错误置成 `cancelling`」的下游效应,**不需要改另外两处**
    - 代码形态见 design.md「Fix Implementation → B5 终态判断复用 taskstore.TERMINAL」
    - _Bug_Condition: isBugCondition_B5(input) —— status ∈ TERMINAL 且 ∉ ("done","failed","cancelled"),即 interrupted_
    - _Expected_Behavior: cancel_download'(task) = False 且状态保持 interrupted(Property 5)_
    - _Preservation: Property 11 —— 活跃态仍置 cancelling、其余终态仍返回 False 且状态不变_
    - _Requirements: 2.11, 2.12, 3.21, 3.22, 3.23_

  - [x] 15.2 验证 B5 缺陷复现测试转绿
    - **Property 5: Bug Condition** - 终态任务不可被拉回活跃态
    - **IMPORTANT**: 重跑任务 13 的**同一批**测试(含全状态遍历),不要新写
    - **EXPECTED OUTCOME**: 全部 PASS
    - 端到端确认(2.12 一次跑通):造 `interrupted` 任务 → POST `/api/cancel/<id>` 返回失败 → 状态仍 `interrupted` → POST `/api/download` 同 URL + 清晰度 + 分 P **不**返回 409 → POST `/api/tasks/clear` 能清掉该记录
    - _Requirements: 2.11, 2.12_

  - [x] 15.3 验证 B5 保持测试仍绿
    - **Property 11: Preservation** - 活跃态与其余终态的取消语义
    - 重跑任务 14 的同一批测试,**EXPECTED OUTCOME**: 全部 PASS
    - 确认 `test_task_state_sets_single_source` 继续通过(前端 `TASK_ACTIVE` 与 SSE 终态集合都不改,后端 `ACTIVE` / `TERMINAL` 取值不变)
    - 跑 `.venv/bin/pytest -q tests/test_downloader.py tests/test_app.py` 确认全绿
    - _Requirements: 3.21, 3.22, 3.23, 3.24_

### B6 统计「今日」按滚动 24 小时(低 · 口径不一致)

- [x] 16. 写 B6 缺陷复现测试(昨日任务被计入今日)
  - **Property 6: Bug Condition** - 「今日」按本地自然日
  - **CRITICAL**: 本测试必须在未修复代码上 FAIL
  - 在 `tests/test_app.py` 新增 `test_stats_day_is_calendar_day`:样本 = `{自然日 00:00 + 60}`(今天)与 `{自然日 00:00 − 60}`(昨天 23:59)→ 断言 `day_count == 1`
  - **不断言 `month_count`**:「昨天 23:59」这条样本在每月 1 日 00:00–00:59 运行时属于上月,会让 `month_count` 期望值随运行时刻变化。day 与 month 的断言拆到两条独立测试里,各自样本集才恒定
  - 未修复现场:`taskstore.py:233` 为 `day_start = now - 86400`
  - **EXPECTED OUTCOME**: FAIL(除凌晨 00:00–00:01 外的绝大多数时刻,`day_count == 2`,昨晚样本被算进今日)
  - _Requirements: 2.13_

- [x] 17. 写 B6 保持测试(本月口径与总量统计不变),并加固既有 month 测试
  - **Property 12: Preservation** - 本月口径与总量统计
  - **IMPORTANT**: 遵循观察优先方法
  - **修改既有测试** `tests/test_app.py::test_stats_month_is_calendar_month`(约 285 行):现有「今天」样本是 `finished_at = int(time.time()) - 3600`,B6 改自然日口径后,若测试在本地 **00:00–01:00** 运行,该时间戳落在昨天(`< day_start`)→ `day_count` 变 0 → `assert r["day_count"] == 1` 失败(flake)
  - 改法:样本时间改用**自然日起点为基准**而不是「当前时刻往前推」—— 今天样本用 `day_start + 60`(`day_start = int(time.mktime(datetime.date.today().timetuple()))`),上月末样本用 `month_start - 43200`;month 断言不动。`day_start + 60` 在 00:00–00:01 运行时是「未来 1 分钟」,但 `compute_stats` 只做 `finished >= day_start` 比较、不校验未来,结果稳定
  - 在测试注释里记录残余竞态:测试算 `today` 与 `compute_stats` 算 `today` 之间若正好跨零点(微秒级窗口)理论上仍可 flake;**不为此给 `compute_stats` 注入统一时钟**(那会在 `taskstore` 里为测试开接缝),备选方案是 monkeypatch `taskstore.datetime.date` 固定 `today()`
  - 同时确认 `total_count` / `total_bytes` / `disk_bytes` 的既有断言不变:只统计 `done` 任务,缺 `downloaded_bytes` 时用下载目录递归磁盘实测值兜底
  - 在未修复代码上运行
  - **EXPECTED OUTCOME**: 改造后的 month 测试 PASS(修复前后都稳定)
  - _Requirements: 3.25, 3.26, 3.27_

- [x] 18. 修复 B6:「今日」改为本地自然日 00:00

  - [x] 18.1 `taskstore.compute_stats()`:`day_start` 改自然日,删除死变量 `now`
    - `day_start = now - 86400` → `day_start = int(time.mktime(today.timetuple()))`,与 `month_start` 同一套 `datetime` / `time.mktime` 写法(`date.timetuple()` 时分秒为 0,`time.mktime` 按本地时区解释 → 正是本地自然日 00:00)
    - `now = int(time.time())`(约 230 行)在修复后不再被任何表达式使用(它此前只服务于 `day_start`),**一并删除**,不留死变量
    - 注释写明旧口径的矛盾(昨晚 23:00 完成的任务今早仍算「今日」,与同一份统计里「本月 = 自然月」自相矛盾)
    - 其余部分(累加循环、`disk_bytes` 递归统计、返回结构)一字不改
    - 代码形态见 design.md「Fix Implementation → B6 「今日」改为本地自然日 00:00」
    - _Bug_Condition: isBugCondition_B6(input) —— finished_at < startOfToday() 且 ≥ now() − 86400_
    - _Expected_Behavior: 该任务不计入 day_count(Property 6)_
    - _Preservation: Property 12 —— month_count / total_count / total_bytes / disk_bytes 不变,今天完成的仍计入今日_
    - _Requirements: 2.13, 3.25, 3.26, 3.27_

  - [x] 18.2 验证 B6 缺陷复现测试转绿
    - **Property 6: Bug Condition** - 「今日」按本地自然日
    - **IMPORTANT**: 重跑任务 16 的**同一个**测试,不要新写
    - **EXPECTED OUTCOME**: 测试 PASS
    - 端到端确认:`/api/stats` 的 `day_count` 与 `month_count` 在同一份任务数据下口径一致(昨日样本两者行为一致地按自然周期归属)
    - _Requirements: 2.13_

  - [x] 18.3 验证 B6 保持测试仍绿
    - **Property 12: Preservation** - 本月口径与总量统计
    - 重跑任务 17 的同一批测试(含加固后的 `test_stats_month_is_calendar_month`),**EXPECTED OUTCOME**: 全部 PASS
    - 跑 `.venv/bin/pytest -q tests/test_app.py` 确认全绿
    - _Requirements: 3.25, 3.26, 3.27_

### 文档与验收

- [x] 19. 更新文档与变更记录
  - `CHANGELOG.md`:在既有 `## [未发布]` 小节下新增一条 `### 🐛 修复(Fixed)`,6 条缺陷各一段,写清用户可感知的后果与根因(沿用 v2.1.1 小节的写法密度)。额外必须记录三件事:B2 的退化取舍(少数异常落盘形态由静默误改邻居文件变为可见的 `rename_error`)、B3 的 2.8 部分成立范围、`_stem_belongs` → `stem_belongs` 的改名。历史小节里对旧标识符的引用**不改动**
  - `docs/REQUIREMENTS.md`:底部缺陷复盘表增 6 行(缺陷 / 根因 / 修复方式),与既有 v2.1 各行同格式;`/api/up-list/check` 的接口说明补上 `started` 字段语义
  - `docs/COMPLIANCE.md`:在自动任务限频小节补一句「进行中再次触发直接跳过、不排队」;`UP_INTERVAL` 与 `AUTO_REFRESH_MIN` 的数字不变
  - 跑 `.venv/bin/pytest -q tests/test_docs.py tests/test_auto_refresh.py` 确认 `[未发布]` 占位结构与 `test_compliance_doc_matches_rate_limits` 均不受影响
  - _Requirements: 2.10, 2.13_

- [x] 20. Checkpoint - 全量回归 + B1 人工验收
  - 跑 `.venv/bin/pytest -q`,确认 **0 failed / 0 error**;总数应为 **183 + 新增用例数**(parametrize 展开后按实际计数),记录实际数字
  - 若有任何既有用例转红,先判定它是「被修复正确反转的过期断言」还是「真实回归」:前者只有任务 7 / 10 / 17 列出的三条,**其余一律按回归处理**,不得为迁就实现而放宽断言
  - **B1 人工验收清单**(纯前端改动、无 JS 运行时,必须手工走一遍):
    - 下载记录页:搜索 → 勾选 → 「删除记录 + 文件」,确认只有目标记录与其磁盘文件消失
    - 观看历史页:搜索 → 勾选 → 批量下载,确认入队的是所勾选的视频
    - 追更合集页(集内):同上
    - 我的收藏页(夹内):同上
    - 稍后再看页:同上
    - 关注动态页(bili 源):同上
    - 「先勾选 → 后搜索」:确认高亮的「已勾选」条目与真实选择集一致,选择状态不转移到其他条目
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.9, 2.10, 2.11, 2.12, 2.13, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19, 3.20, 3.21, 3.22, 3.23, 3.24, 3.25, 3.26, 3.27_
