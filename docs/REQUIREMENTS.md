# bilibili download — 当前需求与实现约束

> 状态：**v2.3.0（2026-09-03）**。
>
> 本文描述当前产品行为与安全边界，不记录易漂移的源码行数、路由总数或测试数量；发布历史见 `CHANGELOG.md`，历史产品批次见 `docs/BACKLOG.md`。

---

## 1. 产品定位

### R1 本地网页应用

- 形态：Python Flask 后端 + 原生 ES modules 单页前端。
- 默认地址：`http://localhost:5050`；可通过 `BD_PORT` 选择 1–65535 的其他端口。
- 数据：配置、认证凭据、任务史、UP 清单与页面缓存持久化在本机 `data/`。
- 网络：登录、内容读取、解析和下载会访问 B 站及媒体地址；项目不提供自己的远端账号或遥测服务。
- 边界：不实现账号密码/滑块自动化、DRM 或付费墙绕过，也不提供上传或二次分发入口。

### R2 启动、ready 与单实例

- 工作区级 OS 文件锁必须在读取或迁移运行态数据之前取得；同一工作区一次只允许一个实例，异常退出后由内核释放锁。
- `启动.command` 必须遵循 `BD_PORT`。检测到本项目已运行或目标端口被未知进程占用时，只报告并退出，不得 TERM、`kill -9` 或以其他方式终止未知进程。
- 启动器只在独立 readiness helper 确认本应用 ready 后打开浏览器；helper 必须使用 `time.monotonic()` 建立严格 60 秒墙钟 deadline，网络 timeout、重试 sleep 和打开等待都不得超过剩余时间。helper 只做 HTTP 探测与 `open`，不得保存服务进程标识或终止任何进程。
- 主 launcher 必须以 `exec "$PYTHON" app.py` 原位进入服务，不创建 pidfile、不使用 `$!`、不通过数值 PID 探测/清理进程；终端信号和退出码直接由 Python 承接。
- 60 秒内未 ready 时 helper 只给出明确提示并自行退出，不得声称或尝试自动停止仍在启动中的服务。
- 下载与自动追更由后端进程执行。浏览器 tab 不是运行前提，关闭后可重新打开；服务进程必须继续运行。

---

## 2. 认证与账号隔离

### R3 登录方式

支持三种显式来源：

1. B 站官方二维码扫码；
2. Edge 浏览器 Cookie；
3. 用户导入的 `cookies.txt`。

二维码和登录 Cookie 都可能过期。二维码是否有效以 B 站轮询状态为准，本项目不承诺固定时长或“长期有效”。只有后端轮询到成功并持久化凭据后，扫码页面才能安全关闭。

### R4 认证上下文

认证上下文至少包含：

- `source`：当前凭据来源；
- `fingerprint`：由已识别账号 ID 派生的非敏感指纹；尚未识别账号时使用来源级指纹；
- `incarnation`：随机 32 位十六进制生命期标识；状态缺失、损坏、不可恢复或从 v1 迁移时开启新生命期；
- `generation`：仅在同一 incarnation 内单调推进的代际，不是跨生命期全局 high-water；
- `authenticated`：是否已经识别为账号级上下文。

要求：

- API 请求取得属于同一完整上下文的 `(context, cookiejar)` 快照，不得拼接旧 Cookie 与新 context。
- 慢请求提交结果前必须 CAS 校验包含 incarnation 与 generation 的完整认证上下文；变化时返回 HTTP 409 `auth_context_changed`。
- Cookiejar 的短时内存复用必须绑定完整认证上下文，不能只按“当前来源”复用。
- 首次从 Cookie 或导航接口识别账号后，应升级为账号 fingerprint，并在当前 incarnation 内推进 generation。
- 来源或身份切换沿用当前 incarnation 并推进 generation；切换或退出登录要串行化凭据文件、认证上下文和内存缓存提交，失败不得宣称成功。
- 合法 v1 状态迁移到 v2 时保留 source、fingerprint、generation 与 authenticated，分配新 incarnation 并写回；不得据此认领旧缓存。
- 状态缺失、损坏或凭据不可恢复时必须开启新 incarnation，generation 从新生命期重新开始；不得从旧 generation 推断全局 high-water。
- 登录状态接口只公开非敏感元数据，包括 `auth_incarnation` / `auth_generation`，不返回 `SESSDATA`、`bili_jct` 或 Cookie 内容。

### R5 内存与磁盘缓存

- 关注列表、关注动态、WBI 等内存缓存按完整认证上下文隔离；旧生命期或旧代请求不得覆盖当前缓存。
- 生产页面缓存路径为：

  `data/cache/accounts/<fingerprint>/i-<incarnation>/g-<generation>/<key>.json`

- 生产读取只接受 exact fingerprint + incarnation + generation，不回退到旧顶层缓存、其他生命期或其他代际。
- 磁盘缓存先写候选文件，再在认证 CAS 临界区原子发布；认证变化时删除候选并返回冲突；metadata 必须记录同一完整上下文。
- 同一 incarnation 内切回同一账号仍产生新 generation；新 incarnation 永不复用旧生命期缓存。旧缓存由 30 天清理策略回收。
- 观看历史保持服务端分页；其他列表页可采用“当前完整认证上下文缓存先显 + 后台拉新”。慢源提示查看最新，快源在没有活动选择时可直接替换。

---

## 3. 内容浏览

### R6 一级页面

侧边栏顺序固定为：

**下载记录 → 观看历史 → 关注动态 → 追更合集 → 我的收藏 → 稍后再看**。

### R7 通用列表行为

- 搜索按标题、UP 或页面业务字段过滤；选择状态使用稳定业务 ID，不得使用过滤后数组下标作为身份。
- 刷新、排序、搜索、虚拟窗口和异步响应不得把选择或删除操作转移到其他条目。
- 列表/网格视图、分页或无限滚动需保留一致的选择语义。
- 来自上游或任务史的动态文本必须转义；外部链接只允许 HTTP(S)，数字和布尔字段必须规范化。
- 关注动态页提供关注投稿、关注列表、自动下载三个视图；关注列表可加入本地 UP 清单。

### R8 页面差异

- 观看历史：显示观看进度，服务端逐页加载更早记录。
- 追更合集、收藏夹、稍后再看：可进入内容并批量下载。
- 下载记录：进行中任务置顶；显示进度、速度、ETA、取消、失败原因、持久化健康、定位与清理动作。

---

## 4. 下载与任务模型

### R9 下载能力

- 单视频：解析标题、封面、UP、时长和可用格式，选择清晰度后下载。
- 多 P：识别分集并使用 `(BV, page)` 粒度去重；任务侧 page 以持久字段为准，查询条目可从 URL 解析。
- 批量：多链接、播放列表、收藏夹、合集等入口最终进入同一批量下载接口。
- DASH：调用 ffmpeg 合并音视频；仅音频不强制视频容器。
- 清晰度：2160P / 1080P / 720P / 480P / 360P / 音频；缺档时选择不超过目标的最高可用档。
- 可选副件：字幕、封面、弹幕；可按 UP 建子目录。
- 所有单任务、批量和自动追更共享一个后端任务表、队列、固定 worker 池和运行时并发 gate。

### R10 稳定身份与产物 manifest

- `task_id` 是任务记录身份；`artifact_set_id` 是该任务媒体与副件集合的稳定身份，两者不得混用。
- 新任务从创建起包含 schema 版本、固定 `artifact_root` 和空 manifest。
- manifest 中每个条目记录角色（`media` / `sidecar`）、规范的下载根相对路径、大小及派生 artifact ID；不信任客户端提供的 artifact ID。
- 主媒体对齐和副件写入后必须在同一任务的 artifact lifecycle 锁内刷新 manifest。
- 新任务的 reveal、统计、失效判断和破坏性删除以 manifest 为文件事实源；`actual_filename` 只作展示和兼容。
- 历史无 manifest 任务保留保守兼容：路径必须在下载根内，主候选不唯一时拒绝破坏性删除。
- manifest 绑定创建时下载根。用户切换下载目录后，不得把旧任务静默重绑定到新目录中的同名文件。
- 若下载命令成功但不能确认唯一、安全、存在的主媒体，任务必须失败，不能无条件标记完成。

### R11 创建的应用层 durable-first

- 活跃任务去重、pending/总量容量检查和整批快照生成在同一持久化临界区完成。
- 新任务完整快照先以临时文件 + 原子替换写入 `tasks.json`；成功后才加入内存任务表，最后注册 runtime 并入队。
- 写盘失败时返回明确错误，下载不得入队，也不得产生“有下载无记录”的幽灵任务。
- 入队阶段若失败，已经持久化的任务转为 `failed` 并保留可观察记录。
- durable-first 指应用层发布顺序，不表示文件系统断电级 `fsync` 保证。
- 后续运行状态持久化失败必须通过 `durable` / `persistence_pending` / `persistence_error` 暴露，不得静默宣称已落盘。

### R12 取消与安全删除

- 取消采用 durable-before-publish：在固定 `_persist_lock → _tasks_lock` 锁序下生成候选完整快照，慢 IO 不持任务表锁；只有原子写成功后，才在原任务字典上发布 `status="cancelling"` 与 `cancel=True`。
- 取消候选写盘失败时必须更新持久化健康并抛出明确错误；API 不得返回虚假成功，worker 也不得观察到取消标志。
- 写盘期间任务可继续推进。若目标在 IO 期间已终结、被移除或替换，必须在释放持久化锁前以当前完整任务表校正磁盘并返回未取消，不得把旧任务复活为 `cancelling`。
- 取消不得整体替换任务表或目标任务字典，既有对象身份和公开 bool/异常契约必须保持。
- 删除活动任务时：
  1. 持久化取消意图；
  2. 在不持有任务表锁的情况下等待关联 runtime 静默；
  3. 超时、缺少可证明静默的 runtime 或任务仍为活动态时，保留记录和文件。
- 对可删除任务，在 artifact lifecycle 锁内读取最新记录与 manifest；先持久化“移除这些记录后”的完整任务表，再移除内存记录，最后删除文件。
- 任务记录写盘失败时不得 unlink 文件。
- unlink 失败时记录已删除，不得伪造回滚；API 返回 `partial`、retained、deleted、skipped、failed 等明细。
- manifest 任务只删除 manifest 当前明确列出的安全路径；legacy 任务只有在主媒体候选唯一时才允许删除文件。
- 每次实际删除前重新验证下载根边界；下载根本身、根外路径和根内 symlink 指向根外均不得删除。

### R13 进度与终态

- 活跃态和终态由 `taskstore` 单一来源定义；取消、重启中断和重试不得散落硬编码状态副本。
- 下载重试使用递增退避；用户取消或账号冷却不得继续制造重试请求。
- 完成终态必须与最终 manifest、`downloaded_bytes` 和 `finished_at` 一并提交。
- 进程重启时历史活动任务转为 `interrupted`，不会自动伪装成仍运行。

---

## 5. 自动下载

### R14 UP 清单

- 添加 UP 主页或 mid；添加时以当前最新投稿建立基线，不补下载加入前的历史。
- 每个 UP 可配置默认清晰度；移除时同时移除其游标与续扫状态。
- 清单数据和游标写入失败必须明确报错，不得先改内存再宣称成功。

### R15 扫描、游标与恢复

- 后台轮询间隔可配置但下限为 10 分钟；UP 之间请求间隔至少 5 秒。
- 手动与定时触发共享同一个非阻塞原子 reservation；忙时直接跳过，不排队补跑，且不得改写上一轮 `last_*` 状态。
- `/api/up-list/check` 必须先成功预约业务锁，再登记并启动 worker、返回 `started=true`；线程启动失败必须释放预约。仅检查线程登记或先调用 `is_running()` 不构成原子启动保证。
- 状态包装器继续调用模块级零参数 `refresh_once()`，保留 monkeypatch / scheduler 契约；直接调用 `refresh_once()` 仍保持非阻塞锁语义。
- reservation 必须持续到成功或错误终态的状态字段写完后才释放，避免相邻轮次的生命周期状态交错。
- 游标由 `ts + same_ts_bvids` 组成，覆盖同一秒多投稿。
- 单轮每 UP 最多扫描 3 页；达到预算且未跨过旧游标时保存 continuation。每轮重取首页；首页锚点变化时把变化前的 deep 前沿冻结为 `head_repair_until_pn`，从第 2 页连续修复低页，同时继续推进 deep。
- deep 前沿遇到短页或跨过旧游标时只持久化 `terminal_pn`；只有 head 已连续闭合冻结接缝且终止证据仍属于稳定首页锚点时才可提交 committed cursor。旧 schema 缺少接缝证明时必须从第 2 页保守重证。
- continuation 累计项目达到容量边界时明确失败，不得裁旧项并推进 committed cursor。
- 自动任务失败或进程中断后，从持久任务史按指数退避恢复；重试不通过高频重扫 UP 接口实现。

---

## 6. API、安全与实时事件

### R16 严格 API 输入与错误

- JSON 写接口必须收到 JSON object；空 body、无效 JSON、数组根节点不得静默当作空对象。
- 字符串、整数、有限数值、布尔和列表均按字段做类型、长度、范围与数量校验。
- 非 2xx API 错误使用稳定 envelope：

  ```json
  {"ok": false, "error": "面向用户的消息", "code": "stable_code"}
  ```

- 字段错误可附 `field`；容量错误附 `capacity/limit/current/requested`；重复任务附现有 `task_id`。
- 未知异常记录到日志，HTTP 响应只返回通用 `internal_error`，不泄露堆栈、上游 URL 或凭据。
- API 响应统一 `no-store`；路由级 400/404/405/413 也转换为稳定 JSON code。
- 个别成功协议内状态（例如缓存 miss 或批量中某项解析失败）可以是 2xx，但必须结构明确，不得与 transport error 混淆。

### R17 本机同源边界

- Host 只允许 `localhost`、`127.0.0.1`、`[::1]` 及合法端口。
- 浏览器发送 Origin、Referer 或 Sec-Fetch-Site 时必须严格同源；三者都缺失时允许本机脚本和测试客户端。
- Cookie 导入体及全局请求体受大小限制；二维码图片只允许 B 站护照 HTTPS 域名。

### R18 聚合 SSE 与兼容入口

- 前端下载页只创建一个 `/api/tasks/events` 聚合 EventSource。
- 聚合流首包为 `snapshot`，之后按 `task_id` 发送带连续 revision 的 `patch`，空闲时发送 heartbeat；客户端发现 revision 断层时必须重建快照。
- EventSource 断开后按退避重连，并可降级为轮询；不得为每个任务建立一条长期 SSE。
- `/api/progress/<task_id>` 暂保留一个兼容周期，仅发送单任务完整快照并在终态结束。
- 聚合流和兼容流共享单进程 SSE 容量；槽满时返回 503 `sse_capacity_exceeded`。

---

## 7. 容量与资源边界

| 资源 | 当前上限 | 需求语义 |
|---|---:|---|
| 单次批量 URL / 单次批量下载项 / 播放列表展开 | 100 | 输入与累计输出都检查；超限要求分批 |
| UP 清单 | 100 | 已有文件超限或新增越界均明确失败 |
| 单 UP continuation 累计项目 | 500 | 读取、合并和写入都检查；不得静默裁剪 |
| queued（pending）任务 | 500 | 整批容量检查，超限不做部分接收 |
| 全部任务历史 | 10,000 | 创建与启动加载都检查；超限保留原文件 |
| `tasks.json` 启动读取 | 64 MiB | 读取时的安全上限；不是每次写入前的序列化大小预检 |
| SSE 连接 | 16 | 聚合与兼容流共享；单进程额度，不是跨进程全局额度 |

补充要求：

- 播放列表解析可多取 1 项只用于判断“实际超过 100”，不得把第 101 项提交给用户或任务队列。
- 任务史和 `tasks.json` 超限不是“文件损坏”；启动应明确失败并保留原文件，供用户备份后人工清理。
- 页面请求体上限、Cookie 导入上限和字段长度限制属于独立 API 防护，不替代上述业务容量。

---

## 8. 请求治理的准确边界

### R19 项目直接 HTTP 调用

- 本项目直接通过 `requests` 发出的 B 站 API 调用统一进入单进程 governor。
- 直接 HTTP 调用最多 2 个同时在途；分类间隔为 sensitive、bulk、media、auth/default 等。
- HTTP 或 JSON 明确 412/429 时进入共享冷却；合法 `Retry-After` 优先，重复事件只延长冷却。
- 冷却期内快速返回本地 HTTP 429 + `Retry-After`，不让 Flask 请求线程睡完整冷却期。
- 登录切换可以清分类 spacing，但不得清账号冷却以绕过风控。

### R20 yt-dlp 边界

- 解析/下载每次 attempt 开始前调用 governor 的 `wait("media")`，遵守当前冷却与启动间隔。
- yt-dlp 内部自行发出的 HTTP 请求 **不占 governor 的两个直接 HTTP 槽**，也不保证每个响应都被 governor 识别 412/429 或计入 governor metrics。
- 因此不得在文档或 UI 中声称“所有网络请求都受两槽 governor”；yt-dlp 下载并发由独立的全局 worker gate 控制。

---

## 9. 前端异步与无障碍

### R21 异步生命周期

- 页面缓存加载、下载任务加载、扫码轮询、新建下载解析和登录状态刷新使用 AbortController 与 generation/sequence 守卫；旧响应、旧 catch、旧 finally 均不得覆盖新页面状态。
- `refreshLogin()` 使用独立 status sequence/controller，且只由当前 status owner 写 `_loggedIn`、登录 DOM、`auth:changed` 或清 controller。退出、浏览器来源切换、Cookie 导入及每次可能保存会话的扫码 poll 在请求前取得共享的单调 mutation owner，并使旧 status owner 失效；每个 mutation 在响应后、调用 `refreshLogin()` 前、刷新返回后以及提示或二维码清理前都必须确认仍为当前 owner，迟到操作静默退出。二维码生成/轮询继续使用独立 controller。
- 认证变化判断必须使用 `auth_incarnation + auth_generation` 组合版本；任一变化都清账号绑定视图和 pending 选择，再按新上下文重新加载。兼容事件字段继续保留 `previousGeneration` / `generation`。
- 所有按钮的 pending 状态在 `finally` 中恢复；错误应经统一网络层解析稳定 `code` 和 `Retry-After`。
- 列表更新按稳定业务 ID merge，不以数组位置作为任务身份。

### R22 无障碍

- 页面包含 skip link、语义 `nav/main`、当前页面 `aria-current` 和 live region。
- 抽屉使用 dialog 语义，打开时隔离背景、圈定焦点、支持 Escape 关闭并恢复触发点焦点。
- 登录 tabs 支持方向键、Home/End；确认操作使用原生 `<dialog>`，不依赖 `prompt()`。
- icon-only 按钮必须有可访问名称；进度条、选择框和状态提示提供对应 ARIA。
- 粗指针环境交互目标至少 44px；焦点可见；支持 safe-area 与 `prefers-reduced-motion`。

---

## 10. 配置、日志与持久化

### R23 配置

- 下载目录、全局并发（1–4）、限速、自动检查间隔、副件、按 UP 归档、通知和主题均支持部分更新。
- 配置先原子写盘，成功后才更新运行时值；失败返回 `config_persist_failed`。
- `BD_PORT` 从环境变量解析，非法或越界值回退默认端口并记录 warning。

### R24 JSON 与日志

- JSON 使用同目录临时文件 + `os.replace` 原子替换；语法损坏文件备份为 `.corrupt-*`。
- 任务史的语义损坏（非列表、非对象项、空或重复 task ID）同样备份，不静默当作空历史。
- `data/app.log` 使用约 5 MiB 大小轮转，保留 3 个备份，并同时输出控制台；不允许日志无限增长。
- 日志不得记录 Cookie、二维码凭据或完整敏感响应。

---

## 11. 当前边界与非目标

- 单实例锁是同一工作区边界；复制仓库可形成不同锁域。
- SSE 16 是单进程容量；当前部署模型不承诺跨进程共享额度。
- durable-first 是应用发布顺序，不是数据库事务、目录 fsync 或掉电必达保证。
- manifest 记录路径、角色与大小，不包含内容 hash；对外部恶意进程在路径校验与 unlink 之间替换文件的通用文件系统竞态不作绝对保证。
- 自动追更从投稿搜索结果工作；同 BV 后续新增分 P 需要额外枚举接口，当前不承诺完整发现。
- 不打包桌面常驻应用；关闭服务进程会中止运行任务，关闭浏览器不会。
- 法律与平台条款边界见 `docs/COMPLIANCE.md`，它不是法律意见。

---

## 12. 主要模块

```text
instance_lock.py    工作区单实例锁
app.py              Flask 路由、严格输入/错误、本机同源、聚合 SSE、日志
bilibili_auth.py    凭据来源、认证快照、fingerprint/incarnation/generation、CAS
bilibili_api.py     账号 API 与完整认证上下文内存缓存
pagecache.py        fingerprint/incarnation/generation 磁盘缓存
request_governor.py 项目直接 HTTP 调用的并发、间隔和冷却
parsing.py          yt-dlp 解析与播放列表边界
downloader.py       worker/gate、运行时句柄、durable-before-publish 取消、完成与删除编排
taskstore.py        稳定身份、manifest、容量、任务持久化和安全文件操作
files.py            落盘定位、改名对齐与冲突处理
up_list.py          UP 清单、复合游标和 continuation
auto_refresh.py     原子预约、自动扫描、同轮互斥和失败退避
storage.py          JSON 原子替换、受限读取与损坏备份
static/js/          原生模块、聚合任务流、异步守卫和无障碍交互
```
