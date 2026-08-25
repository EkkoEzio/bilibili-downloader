# CHANGELOG

所有版本的变更记录。版本遵循[语义化版本](https://semver.org/)。

## [未发布]

## [v2.1.1] - 2026-08-24

一次全项目复查后的清理:修掉 v2.1 重构带进来的一个功能断链,
把三处「同一份知识多副本」收敛成单一来源,并让两份长期漂移的文档重新对齐代码。

> 统计:测试 161 → 183 全过。

### 🐛 修复(Fixed)

- **「批量下载」按钮点了没反应**:v2.1 前端模块化时 `newdl.js` 的
  `batchDownload` 漏写 `export`,`app.js` 的 window 桥上取到的是 `undefined`,
  内联 `onclick` 直接 TypeError。影响新建下载抽屉里解析收藏夹 / 合集 /
  UP 主页 / 多行链接之后的批量下载(单视频下载与各列表页的「下载选中」不受影响)。
  现有前端测试是对拼接文本做子串断言,而这个名字在定义处和桥上都在,
  缺的只是一个关键字——因此 161 个测试全绿也没发现。
- **重试窗口内的状态判断失效**:后端三处手写的活跃状态元组
  (`_ACTIVE` / `find_active_by_url` / `_load_persisted`)都漏了 `retrying`,
  而前端 `TASK_ACTIVE` 有。后果:重试期间重复下载拦截失效(同一视频可能起两个
  任务写同一个文件名)、「清除全部记录」会把正在跑的任务记录删掉、
  重启后 `retrying` 任务永远停在活跃态成僵尸。
- **「删除记录 + 文件」可能删掉邻居视频的文件**:`_task_files` 用**未清洗**的
  `title[:30]` 做文件名前缀匹配,任何共享前 30 字的两个视频互相误配。
  改用完整期望名 + 分隔符边界匹配(`_stem_belongs`)。
- **标题含 `/ : ? *` 等字符的任务永远定位不到文件**:同一处根因——拿原始 title
  去匹配磁盘上已被换成 `_` 的名字。Reveal 找不到文件,还会被「清除失效记录」
  误判为失效。匹配前先过 `sanitize_filename`。
- **删除后残留孤儿附件、体积统计少算**:有 `actual_filename` 的新任务只返回主文件,
  封面 / 字幕 / 弹幕成了孤儿。新增 `_file_family`,把同 stem 的副文件纳入同一族。
- **非 macOS 上「在文件管理器中定位」接口 500**:硬编码 `open -R`,而
  `subprocess.run(check=False)` 只忽略返回码、挡不住命令不存在的
  `FileNotFoundError`。新增 `files.reveal_path` / `open_dir` 按平台分发
  (macOS `open -R` / Windows `explorer /select,` / 其它退化为打开所在目录),
  命令缺失时静默失败。
- **取消 / 中断的任务 SSE 不收流**:服务端只判 `done`/`failed`,取消后仍每秒推一次,
  靠前端主动 close 才停。收流条件改用 `taskstore.TERMINAL`。
- **开了「按 UP 建子目录」后磁盘用量显示 0**:`disk_bytes` 只 `iterdir()` 扫顶层,
  归档模式下文件全在子目录里。改 `rglob("*")` 递归统计。
- **跑测试会改写用户真实的下载记录**(本次清理中实测发现):`conftest.py` 之前只
  隔离了页面缓存,没隔离任务史。而 `taskstore` 是模块级全局状态、导入时就从真实
  `data/tasks.json` 恢复存量任务,任何触发 `_persist()` 的测试都会把「真实记录 +
  测试残留」一起写回真实文件——轻则把正在下载的 `merging` 改写成 `interrupted`,
  重则测试删掉某条真实记录后一 persist 就永久丢下载史(`data/` 在 `.gitignore` 里,
  没有找回途径)。现在 `TASKS_FILE` 重定向到临时目录,且每个用例拿到干净任务表。
  这与 2026-08-22 修的页面缓存污染是同一类问题,当时只补了缓存那一半。

### 🔄 变更(Changed)

- **任务状态机收敛为单一来源**:`taskstore.ACTIVE` / `TERMINAL` 两个常量,
  后端各处与 `app.py` 的 SSE 统一引用;新增测试断言前后端两份集合一致、
  互不重叠、且所有有文案的状态都已归类。
- **`build_filename` 从 `files.py` 下移到 `extras.py`**:`taskstore` 定位文件也要用它,
  而 `files` 依赖 `taskstore`——纯命名逻辑必须待在依赖链底部才不成环。
  `files.py` 继续再导出,`downloader.build_filename` 等调用方不变。
- **路由层不再直接调 `subprocess`**:系统交互统一收进 `files.py`。

### 🧪 测试(Test)

- **window 桥双向一致性**(新增两条,专治上面那个断链):
  桥上每个 `mod.member` 必须真的被该模块 `export`;内联 `on*` 处理器引用的
  全局函数必须都挂在桥上。用临时回退验证过能精确报出缺失项。
- **`COMPLIANCE.md` 的限频承诺与代码常量绑定**:文档里的「UP 之间 ≥N 秒」
  「轮询下限 N 分钟」直接与 `auto_refresh.UP_INTERVAL` / `config.AUTO_REFRESH_MIN`
  对齐比较,任一边改动不同步就跑挂。
- 新增 `_task_files` 匹配收紧的用例:不匹配更长标题的邻居、删文件不波及邻居、
  非法字符标题可定位、多 P 后缀精确命中、副文件纳入同族。
- 新增跨平台唤起用例(平台分发 / 命令缺失不抛 / 路由层无 `subprocess`)、
  磁盘用量递归统计用例。
- 新增运行时数据隔离的自检用例:任务史/缓存落盘位置在临时目录、任务表每例干净、
  `_persist` 后真实 `data/tasks.json` 一个字节不变(去掉隔离 fixture 即三条全红)。
- 3 个旧测试的 patch seam 从 `app.subprocess.run` 改为
  `app.downloader.reveal_path` / `open_dir`。

### 📚 文档(Docs)

- **`docs/REQUIREMENTS.md` 从 v2.0 状态更新到 v2.1**:此前还写着「8 个 Python 模块 /
  模板 2679 行 / 114 测试」,而 README 把它标为「代码实际状态」。现同步了模块清单与
  行数、前端三分离结构、测试分布;补上漏记的 `/api/up-list/reset-seen` 与
  `/api/task/<id>/resolve-conflict` 并修正各分节计数;R17/R18 重写为当前实现;
  新增「模块依赖方向」图;bug 修复表补 13 行 v2.1 记录;头部附上核对数字的命令。
- **`docs/COMPLIANCE.md` 修正两处失真**:UP 间隔写的是 ≥3 秒(代码早已是 5 秒,
  对外承诺比实现松);以及仍在描述 v2.0 已废弃的「主动勾选自动追更」流程。
- `CHANGELOG.md` 的 `[未发布]` 占位小节此前错放在 v2.1.0 与 v2.0.0 之间,已移回顶部;
  v2.1.0 的小节标题曾在整理过程中被误写成 `[v2.0.0] - 2026-08-18`(于是文件里出现
  两个 v2.0.0、v2.1.0 凭空消失,正文还挂在错标题下),已修回。
- 新增 `tests/test_docs.py`:把文档的可机械校验部分固定住——CHANGELOG 版本小节
  不重复且由新到旧、`[未发布]` 占位必须在所有已发布版本之前、每个版本都有底部
  链接引用、README 顶部版本横幅与 CHANGELOG 最新版本一致。上面那个重复标题的
  错误,现在会被第一条直接拦下。

## [v2.1.0] - 2026-08-24

本版两条主线:**下载文件名对齐官方标题 + 可靠性六连修**(2.0 后的增量),
以及一次贯穿前后端的**模块化重构**——行为不变、结构就位,为后续拓展铺路。

> 统计:161 个单元测试全过(2.0 为 114)。
> 前端 `app.js` 1801 行单文件拆为 16 个 ES modules;
> 后端 `downloader.py` 874 行拆出 taskstore/parsing/files,
> `bilibili_auth.py` 546 行拆出 bilibili_api。

### ✨ 新功能(Features)

- **`BD_PORT` 环境变量可换端口**(默认仍 5050):多实例 / 冒烟测试
  不再需要改代码。

### 🔄 变更(Changed)

#### 模块化重构:前端页控制器(行为不变)

- `app.js` 拆为 16 个 ES modules(`<script type="module">`,无构建工具):
  `lib.js` 页控制器框架(共享状态 + 注册表 + 标准列表渲染/无限滚/勾选
  操作条/缓存优先加载),六个页面各自在 `pages/*.js` 注册控制器——
  新增页面从「在 5 个函数里加特判」变成「实现一个控制器对象」;
- `app.js` 仅剩入口 + window 桥(内联 `onclick` 兼容层,模块间一律 import);
- 下载记录页的任务进度卡经 `lib.setItemExtras` 注入,列表框架对任务页无感知。

#### 模块化重构:后端按职责拆分(行为不变)

- `downloader.py` → 下载执行引擎 + `taskstore.py`(任务表/tasks.json
  持久化/CRUD/统计)+ `parsing.py`(yt-dlp 解析)+ `files.py`(文件名
  组装/对齐/冲突);删除只写不读的 `_threads` 死状态;
- `bilibili_auth.py` → 登录与会话 + `bilibili_api.py`(B 站读接口客户端);
  「添加 UP」的三步业务编排从路由层下沉为 `bilibili_api.register_up`;
- `config.py` 可配项改为 `_FIELDS` 字段表驱动:加新字段从改 5 处变为加 1 行
  (加载/校验/夹紧/回滚/序列化全由表驱动,行为与旧实现逐项等价);
- `app.py`:`@api_errors` 装饰器统一 16 个路由的异常出口(500 JSON + 日志),
  统计计算下沉 `taskstore.compute_stats`;
- 清晰度可选项三副本(后端清单/前端 quality.js/操作条 HTML)新增一致性测试,
  漏改直接炸测试。

### 🐛 修复(Fixed)

- **下载文件名对齐 B 站官方标题**:落盘命名权收归 Python 侧(outtmpl 用
  任务权威标题的字面文件名),下载完成后强制把磁盘文件对齐任务标题
  (封面/字幕等副文件随主文件一起改);同名冲突不覆盖,在下载记录页
  提供「加 (1) 改名 / 打开现有文件 / 忽略」三种处理。
- **扫码二维码图与轮询 key 不匹配**:二维码图接口改为使用前端传入的
  配对 `url` 绘制(此前自行 generate 会产生新 key,扫了也一直「未扫码」),
  并加 passport 域名白名单防钓鱼码。
- **任务史持久化裸奔**:tasks.json 改 tmp+rename 原子写 + 独立写锁,
  文件损坏时备份 `.corrupt-*` 供手工找回,绝不静默清零。
- **登出竞态残留旧身份**:bilibili_auth 会话/数据双域加锁,登出发生在
  Edge cookie 提取期间时,提取结果不再写回缓存(旧身份最长残留 5 分钟)。
- **重启后首开「追更合集」不再必弹一次「查看最新」**:快数据源(追更合集 /
  我的收藏 / 稍后再看,单接口几百毫秒)后台拉新返回后直接无感替换渲染,
  不再弹提示条——提示条只为慢源(关注动态 / 关注列表)保留。
- **前端 URL 注入面收窄**:多 P 清晰度加载不再把用户可控 URL 拼进内联 JS
  (改 data-url + 事件监听);收藏夹/合集/UP 卡片的动态参数改走 `data-*`。
- **测试不再污染真实页面缓存**:路由测试经 Flask test_client 会把 mock 数据
  写进 `data/cache/`,导致跑完测试后页面首次打开必弹「有新内容」提示;
  新增 `tests/conftest.py` 全局把缓存写入隔离到临时目录。

## [v2.0.0] - 2026-08-18

本次大版本以「本地网页应用整体重构 + 用户体验补丁」为主线,
围绕四个核心痛点展开:**取消繁琐的追更确认流程**、**首屏秒开不干等**、
**下载队列与下载记录合并去重**、**设置 UI 重新设计 + 改动即生效**。
另外对历史 bug、测试覆盖、文档结构做了清理。

> 统计:14 个源文件 / 文档变动,新增 1 个文件(pagecache.py),
> 114 个单元测试全过,后端 5 个模块、前端 1 个模板。
> 前端模板 `templates/index.html` 从 2314 行涨到 2679 行(增加抽屉/卡片/角标等)。

### ✨ 新功能(Features)

#### UP 清单:加入即自动下载(替代 v1 的「追更确认制」)

- 废弃「追更开关 + 待确认横幅 + 本地发现历史」整套确认制流程
  (`up_list.auto` 字段、`/api/pending*` 系列接口、`new-banner` 提示均删除);
- 加入清单的 UP 一律自动下载新投稿——按每个 UP 的默认清晰度
  (新增 `quality` 字段,UI 下拉选择:最高画质自动 / 1080P / 720P / 480P / 360P / 仅音频);
- 添加 UP 时以**该 UP 当前最新投稿为基线** (`mark_seen`),只下加入之后的新投稿,
  杜绝历史全量补档(回归:存量旧 UP `漫士沉思录` 在升级时一次性补了 30 条,
  后续不会再发生);
- 后台自动下载 (`auto_refresh.refresh_once`) 改串行 + 单并发直接入队
  (`downloader.start_batch` 走每个 UP 的 quality label);
- 手动触发 `/api/up-list/check`(后台线程),抽屉「立即检查新视频」按钮。

#### 缓存优先 + 后台拉新(数据源秒开不干等)

- 新增 `pagecache.py`:数据源端点成功返回时把结果写入 `data/cache/<key>.json`
  (白名单: `follow_updates` / `followings` / `fav_folders` / `fav_seasons` / `watchlater`);
- 前端统一加载器 `cachedLoad()`:进入页面**先读 `/api/cache/<key>` 立即渲染**,
  后台请求真接口;拉到与展示的不同时,顶部提示「**获取到新内容 · 查看最新**」,
  用户主动点才替换(不打断浏览);
- 无缓存的冷启动走加载态(并发刷新已让关注动态首批数据约 40 秒,缓存接入后秒开)。

#### 下载队列并入下载记录页

- 删除独立的「下载队列」抽屉(`dpanel-queue`)、`renderTask`/`loadTasks`/`updateQueueBadge`
  等一组函数;
- **下载记录页 = 全部任务**:进行中排最前(`taskCardHtml` 任务卡:进度条 / 速度 / 取消,
  `updateTaskCard` SSE 就地更新 DOM),后续接完成 / 失败 / 取消记录(失败显示原因);
- 侧边栏「下载记录」角标 = 进行中任务数(`updateDownloadsBadge`);30 秒兜底轮询
  `pollTasks()` 与 SSE 互为保险;
- 底部操作条按页面切换动作:下载记录页显示「删除记录 / 删除记录 + 文件」
  (`delSelected`),其他页面仍为批量下载;页头新增「清除记录」按钮
  (弹窗含「只清失败 / 失效」选项 `delInvalidChoice`);
- 单视频 / 批量下载启动后自动跳到下载记录页(原 `openDrawer('queue')` 行为)。

#### 全局设置改版:抽屉化 + 改动即生效

- 「设置」作为独立抽屉入口(原账号抽屉底部) → 并入账号抽屉下半部分,
  按「下载 / 自动下载 / 保存内容 / 外观」四组排列;
- 移除「保存设置」按钮,所有控件(下拉 / 开关 / 输入框)`onchange` 直接 POST `/api/config`,
  标题旁闪「**已自动保存**」轻提示;
- 主题切换(浅色 / 深色 / 跟随系统)与开关项同样即时落库。

#### 收藏夹页接入「我追的合集」+ 网格视图

- 接入 B 站收藏页左侧栏的「合集区」(`/x/v3/fav/folder/collected/list`,
  必须带 `platform=web` 否则永远返回空列表——从官方 JS bundle 反查出来的踩坑点),
  `/x/space/fav/season/list` 拉合集内容;
- 收藏夹与合集首屏支持**列表 / 网格卡片两种视图**(`fav-card`),
  卡片含封面 / 视频数角标 / 标题 / 类型标签;
- 后续拆分为两个一级页:
  - **追更合集**(独立一级页,自己的搜索 / 刷新 / 视图切换 / 返回);
  - **我的收藏**(只显示收藏夹列表);
- 修复:空收藏夹进入不再显示误导文案「可能 B 站限流或账号无数据」,
  改为「这里还没有收藏任何视频」,避免窄窗口下的尴尬折行。

#### 导航重构

- 侧边栏**菜单顺序**:下载记录 / 观看历史 / 关注动态 / 追更合集 / 我的收藏 / 稍后再看;
- **左下角**:账号(内含登录 + 全部设置),不再单独有「设置」入口;
- **关注动态**页头分段:关注动态 / 关注列表 / 自动下载(原「UP 更新」三视图改名);
- **自动下载视图同时承担 UP 管理**:粘贴链接添加 / 立即检查,UP 卡片内含
  清晰度下拉 + 移除(原抽屉并入)。

#### 通用工具栏统一

- 六个页面页头统一:**搜索 / 刷新 / 切换列表/网格视图**;
- 收藏夹首屏补搜索(按收藏夹名过滤),合集首屏按合集名搜索(本来就有);
- **刷新语义**:立即展示现有数据 + 后台取新,永不再阻塞(下载记录页加了刷新按钮);
- 网格首屏不可滚动修复:首屏多渲染一行 + `maybeFillScreen()` 自动补块直到可滚动
  (历史上因为恰好铺满一屏时滚动条不出现,无限滚会卡死)。

### 🐛 Bug 修复(Fixes)

- **网格首屏无法滚动**:观看历史 / 关注动态 / 关注列表 / 收藏夹 / 稍后再看全修,
  之前只有列表视图能滚动(列表固定 30 条)。
- **下载记录封面右下角播放按钮**新增(`/api/task/<id>/reveal` → `open -R`),
  支持按 UP 归档的子目录里定位文件,找不到时退回打开下载目录;
  队列完成态按钮同款替换。
- **任务记录新增 `uploader_mid`**(yt-dlp 的 `uploader_id` + 批量项透传),
  下载记录页点 UP 名跳转 UP 主页(其他页面早就支持,统一交互)。
- **UP 名称 + 头像**:从用户卡片接口(`/x/web-interface/card`)取,
  存量无名称记录拉到视频后自动回填。
- **收藏夹接口** 切到 B 站原生 `/x/v3/fav/resource/list`(原 yt-dlp 路径会被风控),
  失效视频标注 `attr!=0`。
- **添加 UP 时不再回填旧数据**:`fetch_up_card` 失败不再 fallback 到 arc/search
  抛 AssertionError(避免拉取 30 个 UP)。
- **关注列表 ↔ 关注动态切换回归修复**:`cachedLoad` 重构为缓存渲染
  与后台请求分离,旧实现下首次后台拉取 40+ 秒期间切回会被 `in-flight` 锁
  直接 return(只能靠刷新解决),现在缓存独立渲染,在途保护只罩后台请求。
- **删除完成态不再删文件**:清除所有 / 选中删除时 `delete_file` 默认 false,
  单独选项控制(原逻辑误把默认弄反,会导致点「只删除记录」时文件也被删)。
- **下载记录 `download_dir` 改动即生效**:改完后台进程立即读到新目录
  (`config.DOWNLOAD_DIR` 运行时再读,不再快照)。

### ⚡ 重构(Refactors)

- 删除独立的「下载队列」抽屉及相关函数(`renderTask` / `loadTasks` /
  `_queueChecked` / `updateQueueBadge` / `refreshLibBadges` / `task-item` 系列 CSS);
- 删除 UP 清单抽屉,管理功能并入自动下载页面视图;
- 删除「追更确认制」整套 UI 与 API;
- 下载记录页的 `taskCardHtml` 复用 `video-item` 卡片,简化任务/视频两种形态的渲染;
- `pagecache` 用白名单防止任意文件读写;
- 启动器 `启动.command` 启动前 `lsof` + `kill` 自动清理旧实例(解决「改了不生效」
  这个老问题),5 秒未退出再 `kill -9` 强制结束。

### 🧪 测试(Test)

- 单元测试 **69 → 114**(增加 45 个):
  - `test_app.py`:up-list 基线、缓存层、缓存写盘、路由、删除无效记录、下载记录定位、
    抽屉移除、统一工具栏、缓存优先加载器、关注列表切回、`/api/followings` 接口等;
  - `test_auth.py`:收藏夹原生接口、`platform=web` 必带项、合集内容解析等;
  - `test_auto_refresh.py`:加入清单全部入队(原 auto 字段测试改写)、限流不推进、
    自适应页大小、名称回填、清理未下载等;
  - `test_downloader.py`:按 UP 归档的子目录定位、`clean_invalid_records` 等;
  - `test_up_list.py`:每个 UP 清晰度设置、非法标签、auto 字段废弃等;
  - `test_config.py` / `test_extras.py` / `test_quality.py` 保持并对齐新行为。
- 测试结构沿用仓库惯例:对源码做静态分析 + 用 pytest 的 `monkeypatch` 桩函数,
  避免起 yt-dlp / Flask 进程。

### 📚 文档(Docs)

- 新增 `CHANGELOG.md`(本文件):每次发布的变更日志;
- `docs/REQUIREMENTS.md`:重写为 v2.0 状态,删除调度布局,补全新功能 / API;
- `docs/BACKLOG.md`:按 2026-08-18(a)~(f) 六个批次记录本次迭代;
- `README.md`:重写产品定位与功能列表,与 v2.0 实现对齐;
- 「项目结构」更新:`pagecache.py` / 当前行数 / 测试数。

### ⚠️ 兼容性说明

- `data/up_list.json` 中**老数据的 `auto` 字段被自动忽略**(读取时丢弃),
  加入清单的 UP 仍然按新逻辑自动下载新投稿;
- 升级后**老 UP 的「已见标记」保留**:可能触发一次性历史补档
  (本次发布的真实数据:漫士沉思录 30 条)。后续新加入的 UP 会以「当前最新投稿
  为基线」,只下加入之后的新投稿,不会复现;
- `/api/up-updates`、`/api/up-history`、`/api/pending` 系列接口删除,
  第三方脚本需改用 `/api/up-list` + `/api/up-list/check`;
- `/api/open-folder` 删除,改为 `/api/task/<id>/reveal` 按任务定位。

---

## [v1.x] 历史(2026-08-08 ~ 2026-08-15)

详见 `docs/BACKLOG.md` 与 git 历史。本版本之前的项目使用「订阅追更 + 待确认」
的旧交互范式。

[v2.1.1]: https://github.com/EkkoEzio/bilibili-downloader/releases/tag/v2.1.1
[v2.1.0]: https://github.com/EkkoEzio/bilibili-downloader/releases/tag/v2.1.0
[v2.0.0]: https://github.com/EkkoEzio/bilibili-downloader/releases/tag/v2.0.0