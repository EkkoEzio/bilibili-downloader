# Bugfix Requirements Document

## Introduction

全栈走查后定位到 6 处真实缺陷。现有 183 个 pytest 用例全部通过 —— 说明这批缺陷都落在既有测试的盲区里,必须为每一条补上能锁死行为的断言。

按严重度排序:

| 编号 | 严重度 | 一句话 | 用户可感知的后果 |
|------|--------|--------|------------------|
| B1 | 高危 · 数据丢失 | 搜索过滤后勾选下标错位 | 删除记录+文件时**删错条目、删错磁盘文件**(不可逆);批量下载**下错视频** |
| B2 | 高危 · 数据损坏 | 落盘文件兜底定位用 10 字前缀匹配 | 把**另一个视频的旧文件改名**成本任务的期望名 |
| B3 | 中 · 功能错误 | 「已下载」判定只看 BV 号 | 合集下完 P1 后,**其余分 P 全被当已下载而跳过**,自动下载也漏下 |
| B4 | 中 · 风控风险 | 手动检查会排队叠加多轮 | 连点「立即检查新视频」→ **连续串行打 B 站接口**,违背账号级风控的保守间隔设计 |
| B5 | 低 · 状态机一致性 | 终态集合有硬编码副本,漏 interrupted | 取消 interrupted 任务 → **永久僵尸态**,卡住重复下载拦截与记录清理 |
| B6 | 低 · 口径不一致 | 「今日」是滚动 24 小时 | 昨晚完成的任务今早仍被算进「今日」,与同一函数里「本月=自然月」自相矛盾 |

B1/B2 会造成不可恢复的数据损失,是本次修复的第一优先级。B3 直接让「合集/多 P 下载」这个主场景不可用。B4 触碰的是项目在 `docs/COMPLIANCE.md` 里明确承诺的合规边界。B5/B6 是既有单一来源纪律与统计口径的收口,同样纳入本次范围。

## Bug Analysis

### Current Behavior (Defect)

#### B1 搜索过滤后勾选下标错位

1.1 WHEN 在下载记录页输入搜索词过滤后勾选某条记录并点击「删除记录 + 文件」 THEN the system 删除的是未过滤列表中同下标位置的**另一条**记录及其磁盘文件,被删的文件不可恢复。

1.2 WHEN 在观看历史 / 追更合集 / 我的收藏 / 稍后再看 / 关注动态页输入搜索词过滤后勾选条目并点击批量下载 THEN the system 提交的是未过滤列表中同下标位置的**其他视频**。

1.3 WHEN 先勾选若干条目、再输入搜索词触发重渲染 THEN the system 在界面上把与实际选择不同的条目高亮为「已勾选」,UI 与真实选择集不一致。

#### B2 落盘文件兜底定位用 10 字前缀匹配

1.4 WHEN 下载完成、最终落盘路径未被后处理钩子捕获、且下载目录(含一层子目录)里存在另一个视频的媒体文件其文件名与本任务期望名共享前 10 个字符 THEN the system 把那个无关文件认作本任务的落盘文件,并连同其同名副文件一起改名成本任务的期望名。

1.5 WHEN 任务期望名短于 10 个字符(如「测试」) THEN the system 退化为整名前缀匹配,把「测试2.mp4」这类其他视频的文件认作本任务的落盘文件。

#### B3 「已下载」判定只看 BV 号,多 P 视频被误跳过

1.6 WHEN 同一合集/多 P 视频的某一个分 P 已下载完成,浏览该合集的其余分 P THEN the system 给同 BV 号下**所有**分 P 挂上「已下载」角标,用户无法分辨实际下了哪几 P。

1.7 WHEN 勾选同一合集中尚未下载的其余分 P 并点击批量下载 THEN the system 把它们全部当作已下载过而过滤掉,并提示「所选项均已下载过」,一条都不入队。

1.8 WHEN 自动下载检查到某合集出现新分 P、而该 BV 号已有完成任务 THEN the system 跳过该分 P 不入队,新分 P 永远不会被自动下载。

#### B4 手动「立即检查新视频」会排队叠加多轮

1.9 WHEN 一轮检查正在运行时再次触发手动检查 THEN the system 让新一轮阻塞排队,并在前一轮结束后完整再跑一遍(每个 UP 之间等待 5 秒、串行请求 B 站 arc/search);连点 N 次即连续跑 N 轮,后台 30 分钟轮询也会与手动检查叠加。

1.10 WHEN 已有检查在进行中时调用手动检查接口 THEN the system 返回与「已成功启动新一轮」完全相同的响应,前端无从区分,只能固定禁用按钮 30 秒。

#### B5 终态集合硬编码副本漏 interrupted

1.11 WHEN 对状态为 `interrupted` 的任务调用取消接口 THEN the system 把它改成 `cancelling`(属于活跃态)并返回成功,而没有任何线程会去收尾,任务永久停留在该状态。

1.12 WHEN 存在上述停留在 `cancelling` 的任务 THEN the system 对同 URL + 清晰度 + 分 P 的新下载请求判为「正在下载中」并返回 409;「清除全部记录」也把该记录当作活跃任务而不清除。

#### B6 统计「今日」按滚动 24 小时

1.13 WHEN 统计接口计算「今日」计数 THEN the system 以「当前时刻往前 86400 秒」为起点,把昨日 00:00 之后完成的任务也计入「今日」(如昨晚 23:00 完成的任务在今早 08:00 仍被算进今日),与同一份统计里「本月 = 自然月」的口径自相矛盾。

### Expected Behavior (Correct)

#### B1 搜索过滤后勾选下标错位

2.1 WHEN 在下载记录页输入搜索词过滤后勾选某条记录并点击「删除记录 + 文件」 THEN the system SHALL 只删除该可见条目自身的记录与磁盘文件,不得触碰任何其他记录或文件。

2.2 WHEN 在观看历史 / 追更合集 / 我的收藏 / 稍后再看 / 关注动态页输入搜索词过滤后勾选条目并点击批量下载 THEN the system SHALL 只提交所勾选的那些可见条目对应的视频。

2.3 WHEN 先勾选若干条目、再输入搜索词触发重渲染 THEN the system SHALL 使界面高亮的「已勾选」条目与真实选择集完全一致(命中搜索词的仍勾选,未命中的隐藏但选择状态不转移到其他条目)。

2.4 WHEN 下载完成、最终落盘路径未被后处理钩子捕获、需要扫盘兜底定位 THEN the system SHALL 只接受文件名与期望名精确相等、或期望名后紧跟分隔符后缀(` ` / `.` / `-` / `_` / `(` / `[`)的文件;不满足该归属规则的文件一律不认、不改名。

2.5 WHEN 任务期望名短于 10 个字符、下载目录存在以该期望名开头但紧跟其他文字的文件(如期望名「测试」与文件「测试2.mp4」) THEN the system SHALL 判定该文件不属于本任务。

2.6 WHEN 同一合集/多 P 视频的某一个分 P 已下载完成,浏览该合集的其余分 P THEN the system SHALL 只给已完成的那个分 P 挂「已下载」角标,其余分 P 显示为未下载。

2.7 WHEN 勾选同一合集中尚未下载的其余分 P 并点击批量下载 THEN the system SHALL 把这些分 P 全部入队,只跳过 (BV, 分 P) 都已完成的那些。

2.8 WHEN 自动下载检查到某合集出现新分 P、而该 BV 号已有其他分 P 的完成任务 THEN the system SHALL 把新分 P 入队下载。

2.9 WHEN 一轮检查正在运行时再次触发手动检查 THEN the system SHALL 直接跳过本次触发,不发起任何 B 站请求、不叠加第二轮。

2.10 WHEN 已有检查在进行中时调用手动检查接口 THEN the system SHALL 在响应中如实标明「已有检查在进行中、本次未启动新一轮」,前端 SHALL 据此给出对应提示,不再依赖固定 30 秒的假禁用窗口。

2.11 WHEN 对状态为 `interrupted` 的任务调用取消接口 THEN the system SHALL 拒绝该操作(返回失败)并保持任务状态为 `interrupted`,不产生无人收尾的活跃态。

2.12 WHEN 任务已处于任一终态(`done` / `failed` / `cancelled` / `interrupted`) THEN the system SHALL 使「取消」「重复下载拦截」「清除全部记录」三处对终态的判断完全一致地把它当作已结束的任务:取消被拒、不拦截同 URL 的新下载、记录可被清除。

2.13 WHEN 统计接口计算「今日」计数 THEN the system SHALL 以本地自然日 00:00 为起点,只把今天完成的任务计入「今日」,与「本月 = 自然月」同口径。

### Unchanged Behavior (Regression Prevention)

#### 勾选与批量操作(B1 相关)

3.1 WHEN 没有搜索词时勾选下载记录并点击「删除记录 + 文件」 THEN the system SHALL CONTINUE TO 精确删除所勾选条目的记录与文件。

3.2 WHEN 没有搜索词时在内容页勾选条目并点击批量下载 THEN the system SHALL CONTINUE TO 提交所勾选的视频。

3.3 WHEN 点击「全选」 THEN the system SHALL CONTINUE TO 只作用于当前可见(未被搜索词过滤掉)的条目。

3.4 WHEN 点击封面图切换勾选 THEN the system SHALL CONTINUE TO 正确切换该条目的勾选状态并同步底部操作条计数。

3.5 WHEN 快数据源后台拉到新数据且用户已有勾选 THEN the system SHALL CONTINUE TO 放弃静默替换、退回「查看最新」提示条,不在有勾选时重渲染列表。

3.6 WHEN 下载记录页在用户已有勾选期间收到 SSE 进度或轮询结果 THEN the system SHALL CONTINUE TO 推迟整体重拉,待勾选清零后补上。

#### 落盘文件定位(B2 相关)

3.7 WHEN 后处理钩子已捕获最终落盘路径且该文件存在 THEN the system SHALL CONTINUE TO 优先使用该权威路径,不扫盘。

3.8 WHEN 磁盘文件名与期望名精确相等 THEN the system SHALL CONTINUE TO 命中该文件。

3.9 WHEN 磁盘文件名是期望名加分隔符后缀(分 P 的 ` - P3`、字幕的 `.zh-Hans`、改名冲突的 ` (1)`) THEN the system SHALL CONTINUE TO 认定其属于本任务。

3.10 WHEN 同 stem 存在封面 / 字幕 / 弹幕副文件 THEN the system SHALL CONTINUE TO 在改名与删除时覆盖整族文件,不留孤儿附件。

3.11 WHEN 期望名对应的目标文件已存在且与实际文件不是同一文件 THEN the system SHALL CONTINUE TO 拒绝覆盖,按同大小/不同大小分别写 `rename_skipped` / `rename_conflict` 交由用户处理。

#### 已下载判定(B3 相关)

3.12 WHEN 判定一个不带 `?p=` 的单 P 视频 URL 且其 BV 号已有完成任务 THEN the system SHALL CONTINUE TO 判为已下载。

3.13 WHEN 判定的 (BV 号, 分 P) 与某个完成任务完全相同 THEN the system SHALL CONTINUE TO 判为已下载并在批量提交时跳过。

3.14 WHEN URL 中提取不到 BV 号(短链 / av 号) THEN the system SHALL CONTINUE TO 判为未下载。

3.15 WHEN 任务只有 `done` 之外的状态(排队 / 下载中 / 失败 / 取消 / 中断) THEN the system SHALL CONTINUE TO 不把对应视频计入「已下载」。

#### 自动检查(B4 相关)

3.16 WHEN 没有检查正在运行时触发手动检查 THEN the system SHALL CONTINUE TO 在后台线程启动一轮并立即返回,不阻塞前端。

3.17 WHEN 一轮检查内串行处理多个 UP THEN the system SHALL CONTINUE TO 在 UP 之间保持不少于 5 秒的间隔。

3.18 WHEN 某个 UP 本轮拉取返回空(疑似限流) THEN the system SHALL CONTINUE TO 不推进该 UP 的已见标记,留给下一轮重试。

3.19 WHEN 后台轮询线程运行 THEN the system SHALL CONTINUE TO 按配置的间隔(不低于既有下限)周期性触发检查。

3.20 WHEN 一轮检查中某个 UP 处理抛异常 THEN the system SHALL CONTINUE TO 记录日志并继续处理后续 UP,不中断整轮。

#### 任务状态机(B5 相关)

3.21 WHEN 对 `queued` / `downloading` / `merging` / `retrying` 状态的任务调用取消 THEN the system SHALL CONTINUE TO 置为 `cancelling` 并由下载线程收尾为 `cancelled`。

3.22 WHEN 对 `done` / `failed` / `cancelled` 状态的任务调用取消 THEN the system SHALL CONTINUE TO 返回失败且不改变任务状态。

3.23 WHEN 下载线程收尾一个被取消的任务 THEN the system SHALL CONTINUE TO 只在任务尚未处于终态时才落为 `cancelled`,已终结的任务状态不被覆盖。

3.24 WHEN 前端渲染任务卡与角标 THEN the system SHALL CONTINUE TO 使用与后端活跃态集合一致的状态列表。

#### 统计口径(B6 相关)

3.25 WHEN 统计「本月」计数 THEN the system SHALL CONTINUE TO 按自然月计算,上月末完成的任务不计入本月。

3.26 WHEN 任务在今天完成 THEN the system SHALL CONTINUE TO 计入「今日」。

3.27 WHEN 统计总下载数 / 总字节数 THEN the system SHALL CONTINUE TO 只统计 `done` 任务,并在历史记录缺 `downloaded_bytes` 时用下载目录磁盘实测值(递归)兜底。

### Bug Condition 与 Property(形式化)

约定:`F` 为修复前的行为,`F'` 为修复后的行为。

#### B1

```pascal
FUNCTION isBugCondition_B1(X)
  INPUT: X = (items, query, visibleItem)   // 一次列表渲染中的某个可见条目
  OUTPUT: boolean

  RETURN X.query ≠ "" AND filter(X.items, X.query) ≠ X.items
END FUNCTION

// Property: Fix Checking —— 勾选下标必须能定位回它所代表的条目
FOR ALL X WHERE isBugCondition_B1(X) DO
  emitted ← checkboxIndexOf(X.visibleItem)
  ASSERT X.items[emitted] IS X.visibleItem
END FOR
```

#### B2

```pascal
FUNCTION isBugCondition_B2(X)
  INPUT: X = (expectedStem, diskStem)
  OUTPUT: boolean

  RETURN capturedFinalPath IS NONE
     AND diskStem ≠ X.expectedStem
     AND diskStem.startswith(X.expectedStem[:10])
END FUNCTION

// Property: Fix Checking —— 兜底匹配与 stem 归属规则等价
FOR ALL X WHERE isBugCondition_B2(X) DO
  matched ← locateActualFile'(X)
  ASSERT (matched = X.diskStem) ⟺ stemBelongs(X.diskStem, X.expectedStem)
END FOR
```

#### B3

```pascal
FUNCTION isBugCondition_B3(X)
  INPUT: X = (url, tasks)
  OUTPUT: boolean

  // 同 BV 有已完成任务,但分 P 不同
  RETURN ∃ t ∈ X.tasks : t.status = "done"
                     AND bvid(t.url) = bvid(X.url)
                     AND pageOf(t) ≠ pageOf(X.url)
     AND ¬∃ t ∈ X.tasks : t.status = "done"
                      AND bvid(t.url) = bvid(X.url)
                      AND pageOf(t) = pageOf(X.url)
END FUNCTION

// Property: Fix Checking —— 判定粒度是 (bvid, page)
FOR ALL X WHERE isBugCondition_B3(X) DO
  ASSERT isDownloaded'(X.url) = FALSE
END FOR
```

#### B4

```pascal
FUNCTION isBugCondition_B4(X)
  INPUT: X = 一次检查触发(手动或后台轮询)
  OUTPUT: boolean

  RETURN 已有一轮 refresh 正在运行
END FUNCTION

// Property: Fix Checking —— 后来者跳过而非排队
FOR ALL X WHERE isBugCondition_B4(X) DO
  result ← refreshOnce'(X)
  ASSERT result 未发起任何 B 站请求
     AND result 如实告知「已有检查在进行中」
END FOR
```

#### B5

```pascal
FUNCTION isBugCondition_B5(X)
  INPUT: X = task
  OUTPUT: boolean

  // 终态集合的硬编码副本漏掉的那个状态
  RETURN X.status ∈ TERMINAL AND X.status ∉ ("done", "failed", "cancelled")
END FUNCTION

// Property: Fix Checking —— 终态任务不可被拉回活跃态
FOR ALL X WHERE isBugCondition_B5(X) DO
  result ← cancelDownload'(X)
  ASSERT result = FALSE AND X.status 保持不变
END FOR
```

#### B6

```pascal
FUNCTION isBugCondition_B6(X)
  INPUT: X = finished_at (Unix 秒)
  OUTPUT: boolean

  RETURN X < startOfToday() AND X ≥ now() − 86400
END FUNCTION

// Property: Fix Checking —— 「今日」按自然日
FOR ALL X WHERE isBugCondition_B6(X) DO
  ASSERT X 不计入 day_count
END FOR
```

#### 共同的保持目标

```pascal
// Property: Preservation Checking —— 非缺陷输入行为逐位不变
FOR EACH n IN (B1, B2, B3, B4, B5, B6) DO
  FOR ALL X WHERE NOT isBugCondition_n(X) DO
    ASSERT F(X) = F'(X)
  END FOR
END FOR
```
