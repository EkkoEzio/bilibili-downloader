# bilibili download — 完整需求文档

> 本文档描述项目当前**已实现的全部功能**，基于代码实际状态整理
>（v2.2.0，2026-08-31）。
> 15 个 Python 模块（4178 行）+ 前端三分离（模板 388 行 / CSS 533 行 /
> 17 个 ES modules 共 2323 行），39 个不同的 `@app.route` 路径
>（38 个 API + `/` 单页骨架；`/api/config` 的 GET/POST 使用两个装饰器）。
> 现有测试 304 个全过。
>
> 改这份文档时请顺手核对数字:`wc -l *.py`、`pytest -q`、
> `grep -o '@app.route("[^"]*"' app.py | sort -u | wc -l`。

---

## 一、项目定位

### R0 核心诉求
- **用户**:B 站大会员,想把视频高画质地留存到本地
- **形态**:本地网页应用(Python Flask 后端 + 单页 HTML 前端),浏览器访问 `localhost:5050`
- **启动方式**:双击 `启动.command`(Mac),首次自动建虚拟环境+装依赖,
  **启动前自动 kill 占着 5050 端口的旧实例**(避免「改了不生效」)
- **隐私**:所有数据存本地 `data/`,不上传任何服务器

### R1 技术栈
- 后端:Python 3 + Flask + yt-dlp(下载引擎)+ ffmpeg(音视频合并)+ requests
- 前端:原生 ES modules 单页(无框架、无构建工具)+ EventSource(SSE 进度);
  结构 / 样式 / 脚本三分离,页控制器注册到 `lib.js` 的公共框架
- 本地运行,数据全部存在 `data/` 目录

---

## 二、登录系统

### R2 三种登录方式

| 方式 | 说明 | 状态 |
|---|---|---|
| **扫码登录**(主) | 网页显示二维码(本地 qrcode 库生成),手机 B 站 App 扫码确认,长期有效 | ✅ |
| **使用 Edge** | 读 Edge 浏览器已有的 B 站登录态(`--cookiesfrom-browser edge`) | ✅ |
| **cookies.txt**(兜底) | 浏览器扩展导出后粘贴 | ✅ |

用户明确**拒绝**账号密码+验证码(B 站极验滑块无法自动化)。

### R3 登录态识别
- 任意一种登录成功后,侧边栏「账号」文字立即显示用户名
- `get_login_status()` 复用下载用的同一套 cookie 来源(扫码/Edge/cookies.txt 统一)
- **退出登录**:清 cookiejar 缓存 + follow 缓存,确保退出立即生效

### R4 登录区 UX
- 「账号」抽屉**上半部分**是登录区(三个 tab:扫码 / Edge / cookies.txt)
- 登录成功后整个登录区不收起,直接展示其他 tab 内容(扫码 / Edge / cookies)
- 扫码成功 → 二维码收起 + 显示「✓ 登录成功!」
- 「账号」抽屉**下半部分**是全部设置(改动即生效,见 R18)

---

## 三、内容浏览(「我的B站」区,6 个一级页)

> 用户原话:"不用去复制链接过来解析,而是直接在这看到一些视频,多选下载"

### 5.1 缓存优先 + 后台拉新(v2.0 新机制)
- 所有数据源(关注动态 / 关注列表 / 收藏夹 / 追更合集 / 稍后再看)打开即显示
  `data/cache/<key>.json` 的上次结果(秒开,不显示加载圈);
- 同时后台请求真接口;
- 拉到与展示的不同时,顶部提示「**获取到新内容 · 查看最新**」,用户主动点才替换;
- 缓存键有白名单（`pagecache.py` 的 `KEYS`），防任意文件读写；
- 应用启动时删除超过 30 天未更新的缓存文件，避免缓存目录长期积累；
- 冷启动(无缓存)才显示加载态;
- 观看历史因单页 ~1s 不参与,保持原有分页。

### 5.2 侧边栏顺序(用户指定)
下载记录 → 观看历史 → 关注动态 → 追更合集 → 我的收藏 → 稍后再看;
左下角:**账号**(内含登录 + 全部设置)。

### R5 观看历史(history)
- 自动加载(首次需登录),含观看进度百分比
- 滚动到底自动逐页加载更早的历史(B 站接口分页)
- 多选 → 选清晰度 → 批量下载

### R6 关注动态(feed)
页头分段切三个视图:

| 视图 | 数据源 | 说明 |
|---|---|---|
| **关注动态**(bili) | `/api/follow-updates` | B 站关注 UP 的最近投稿,串行+1.5 秒间隔防账号级 412 |
| **关注列表**(all) | `/api/followings` | 站点关注的全部 UP,可一键加入本地清单 |
| **自动下载**(mine) | `/api/up-list` | 页面内直接管理 UP 清单(粘贴添加 / 清晰度下拉 / 移除 / 立即检查) |

### R7 追更合集(seasons,v2.0 新独立页)
- `/api/fav-seasons` 拉取(`/x/v3/fav/folder/collected/list`,必须带 `platform=web`
  否则永远返回空列表,实测踩到的坑)
- 卡片 / 列表两种视图,按合集名搜索
- 点进合集看内容(`/api/season-content`)

### R8 我的收藏(fav)
- 列表(卡片 / 列表两种视图,按收藏夹名搜索)
- 点进看内容(收藏夹卡片可按 UP 主链接打开主页)
- /api/fav-folders 拉列表; /api/fav-content 拉内容(B 站原生,失败标「已失效视频」)

### R10 稍后再看(later)
- 加载「稍后再看」列表
- 多选 → 批量下载

### R11 通用体验
- 列表搜索过滤:搜索框实时按标题 / UP 主过滤,勾选保留
- **页头工具栏统一**:搜索 / 刷新 / 切换列表网格 / 视图(刷新不阻塞)
- 网格首屏不可滚动修复（首屏多渲染一行 + 自动补块）
- 所有来自上游/任务记录的动态 HTML 均转义；外部链接只允许 HTTP(S)，UP mid、页码、
  媒体计数等只接受规范数字，避免错误消息、缩略图和 URL 注入

---

## 四、下载能力

### R12 基础下载
- 输入 URL → 解析 → 列出清晰度(带预估文件大小)→ 选 → 下载
- **大会员高画质**:2160P(4K)/ 1080P / 720P / 480P / 360P(需带登录态 cookie)
- **DASH 音视频合并**:高画质自动用 ffmpeg 合并成单 mp4
- 文件名:`{视频标题}.mp4`(清洗非法字符),分P视频带 P 序号

### R13 进度条 / 队列合并(v2.0)
- 实时显示:**进度条 + 百分比 + 速度 + 剩余时间**
- SSE(Server-Sent Events)就地更新下载记录页的卡片,DOM 不刷新不打断
- **下载队列已并入下载记录页**:进行中任务排在最前(进度条/速度/取消),失败
  任务显示原因,完成记录在后

### R14 多 P 分集视频
- 自动识别 → 返回分 P 列表(标题 + 时长)
- 选具体 P → 拉该 P 的清晰度 → 下载

### R15 批量下载
- **多链接一次下**:多行输入框,每行一个 URL
- **分 P 选集下**:勾选要下的 P
- **收藏夹 / 合集 / UP 清单**:解析整列后勾选
- 全局并发默认 2、可配 1–4；固定 worker 池与运行时 gate 由单视频、批量和自动追更
  共同使用，不会因同时启动多个批次而突破全局上限

---

## 五、下载管理(并入下载记录页)

### R16 取消 + 重试
- **取消**:进行中点「取消」按钮,立即结束
- **失败自动重试**:最多 3 次,递增退避(2/4/6 秒)
- 用户取消不重试;重试中显示「重试中」橙色 spinner

### R17 删除(按模式切换)
- **下载记录页**勾选任意条目 → 底部操作条出现「删除记录 / 删除记录 + 文件」
  (`delSelected`,按按钮语义直接删,不再弹二次选择弹窗)
- **页头两个常驻按钮**:「清除全部记录」(只删记录,不动文件)/
  「清除失败/失效记录」(失败任务 + 文件已不在本地的完成任务)
- 「删除记录 + 文件」删的是任务的**整族**文件(主文件 + 封面/字幕/弹幕),
  且只删匹配到自己期望名的文件,不会波及标题相近的邻居视频
- 勾选期间列表重排会移位索引 → `_dirty` 标记延迟到清空勾选后再刷新
  (`onChecksEmpty` 回调)

### R18 文件定位
- 完成卡片右下角**播放按钮** → `/api/task/<id>/reveal` → 在系统文件管理器中
  定位该视频文件;按 UP 归档(子目录)也能找到
- 跨平台:macOS `open -R` / Windows `explorer /select,` / Linux 退化为
  打开所在目录(`files.reveal_path`);命令缺失时静默失败,不影响其他功能
- 找不到文件时退回打开下载目录,接口如实返回 `found: false`
- 定位用的是任务的权威落盘路径(`actual_filename`),连同封面/字幕/弹幕
  等副文件算作同一族(删除、体积统计口径一致)

### R19 统计面板
- 顶部:`总下载数 / 总大小 / 本月 / 今日`
- 下载完成时自动刷新
- 任务记录含 `downloaded_bytes` + `finished_at`

---

## 六、UP 清单:加入即自动下载(v2.0 简化)

> v1 是「勾选自动追更 + 待确认横幅 + 用户点全部下载才入队」的繁琐流程。
> v2 简化:加入即自动下载,新投稿直接进下载队列(单并发串行)。

### R20 清单管理
- 「关注动态 → 自动下载」视图直接管理:粘贴主页链接或 mid 添加;
  关注列表视图也可一键「加入清单」
- **添加时以当前最新投稿为基线**(`mark_seen`),只下加入之后的新投稿,
  不会翻旧账批量补档
- 每个 UP 可单独配默认清晰度(最高画质自动 / 1080P / 720P / 480P / 360P / 仅音频)
- 移除:确认后清除清单与已见标记

### R21 自动下载轮询
- 后台按设置间隔（默认 30 分钟，可配 10–360 分钟）串行检查
- 单 UP 拉取间隔 ≥5 秒；所有 `arc/search` 还受统一 `sensitive` 1.5 秒 + 抖动约束
- 自适应页大小：24 小时内刚拉过使用 5 条小页，否则 15 条；单轮每个 UP 最多
  3 页只是请求预算，不是历史范围上限
- 达到页预算但尚未严格跨过旧游标时不推进 committed cursor，而是持久化 scan
  continuation；后续轮次先重取首页检测 offset 漂移，再按 BVID 去重续扫
- 游标使用 `ts + same_ts_bvids`，同一秒发布多条投稿也不会因排序假设而遗漏
- 明确限流时不推进“已见”标记；自动任务失败或进程中断后从持久任务史按
  10 分钟起步、24 小时封顶指数退避重试
- 可手动触发 `/api/up-list/check`；已有一轮运行时直接跳过、不排队、不发上游请求，
  响应通过 `started` 如实说明是否启动新一轮

### R22 数据持久化
- `data/up_list.json`：`ups`（清单）+ `last_seen`（`ts`、`same_ts_bvids`、
  `fetched_at` 与可选 `scan` continuation）
- 追更游标没有固定过期时间，保留到用户移除 UP 或手动重置；任务历史也不按时间
  自动删除，仅由“清除全部记录”或“清除失败/失效记录”显式清理
- JSON 写入采用 tmp + rename；关键状态只有在写盘成功后才提交到内存。任务创建则
  锁内原子查重并按“先持久化、后内存可见、最后入队”的顺序执行
- 兼容老数据：`auto` 字段读取时丢弃，缺少 `quality`、复合游标或续扫字段时使用默认值

---

## 七、设置(账号抽屉下半部分,改动即生效)

> v2.0 把设置从独立抽屉并入账号抽屉,UI 按"下载 / 自动下载 / 保存内容 / 外观"
> 四组重排;每项控件 `onchange` 直接 POST `/api/config`,无需点保存,
> 标题旁闪「**已自动保存**」轻提示。

### R23 下载设置
- **下载目录**:界面可改(自动展开 ~、创建目录、持久化)
- **同时下载数**:1-4 可选(默认 2)
- **单任务限速**:不限 / 1/2/5/10/20 MB/s(yt-dlp ratelimit)

### R24 自动下载
- **检查间隔**:10/30/60/120/360 分钟(下限 10 分钟,合规护栏)

### R25 保存内容
- **弹幕 / 字幕 / 封面**:任务完成后另存 .xml / srt / 封面图
- **按 UP 建子目录**:归档到「下载目录/UP 名/」下
- **完成通知**:macOS 系统通知

### R26 外观
- **主题**:浅色 / 深色 / 跟随系统
- CSS 变量重构(`--bg-page`/`--text-main` 等),`body.dark` 覆盖

---

## 八、技术约束与已知限制

### R27 端口与本机安全边界
- 默认 `5050`（避开 macOS ControlCenter 占用的 5000 端口）
- 所有请求的 `Host` 必须是 `localhost`、`127.0.0.1` 或 `[::1]`；LAN 地址、机器名、
  反向代理 Host 和伪装成 `localhost.evil` 的域名均拒绝
- 浏览器提供 `Origin`、`Referer`、`Sec-Fetch-Site` 时必须严格同源；来源头全部缺失
  仍允许 Flask test client、curl 和本机脚本

### R28 B 站限流（关键合规边界）
- 所有显式 requests 调用和 yt-dlp 启动链路统一接入 `RequestGovernor`；单进程、单账号
  最多 2 个上游请求同时在途
- `sensitive` 至少 1.5 秒 + 0–0.35 秒抖动；`bulk` 0.25 秒 + 0–0.08 秒；
  `media` 0.5 秒 + 0–0.15 秒；`auth/default` 仍受全局在途上限和账号冷却约束
- HTTP 或 JSON 明确返回 412 时默认冷却 10 分钟，429 默认冷却 2 分钟；合法
  `Retry-After` 优先，重复限流事件只延长、不缩短冷却
- 冷却期内快速抛 `UpstreamRateLimitError`，路由统一返回 HTTP 429 + `Retry-After`；
  不再把限流伪装成空列表或普通 500，也不会继续轰炸上游
- `/api/governor/status` 只读暴露冷却剩余、在途数和计数指标，不返回上游 URL 或凭据
- `collected/list` 必须带 `platform=web`，否则返回空列表

### R29 自动任务合规
- 自动检查属 B 站「自动程序」条款场景,间隔刻意保守(≥5 秒/UP),
  UP 间隔 ≥5 秒,轮询间隔下限 10 分钟
- 详见 `docs/COMPLIANCE.md`

### R30 未做项
- **后台托盘/窗口置顶**:经评估不做(需打包成 Tauri/Electron 桌面应用,
  改变项目形态,投入产出比低)

---

## 九、API 接口清单（38 个 API 路径 / 39 个 API route 装饰器）

> `/api/config` 一个路径分别声明 GET/POST，故装饰器数比 API 路径数多 1；
> 另有 `/` 返回单页骨架。下表把动态 `/api/cache/<key>` 的 5 个合法 key 展开列出。

### 登录(7)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/login/status` | 当前登录状态 |
| GET | `/api/login/qrcode` | 生成二维码(url + qrcode_key) |
| GET | `/api/login/qrcode-image` | 本地生成二维码 PNG |
| GET | `/api/login/qrcode/poll?key=` | 轮询扫码状态 |
| POST | `/api/login/use-browser` | 切换到 Edge cookie 模式 |
| POST | `/api/login/cookie-txt` | 导入 cookies.txt |
| POST | `/api/login/logout` | 退出登录 |

### 请求治理（1）
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/governor/status` | 只读查看账号级冷却、在途请求和限流计数；禁止缓存 |

### 解析/下载（7）
| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/parse` | 解析视频(返回标题/清晰度/预估大小/是否多P) |
| POST | `/api/parse-part` | 解析指定分 P 的清晰度 |
| POST | `/api/download` | 启动单视频下载(带 uploader_mid) |
| POST | `/api/cancel/<task_id>` | 取消下载 |
| GET | `/api/progress/<task_id>` | SSE 推送下载进度(任务进终态后收流) |
| POST | `/api/task/<task_id>/reveal` | 在文件管理器中定位该任务的文件 |
| POST | `/api/task/<task_id>/resolve-conflict` | 处理落盘名冲突(加 (1) 改名 / 打开现有 / 忽略) |

### 批量(2)
| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/batch/parse` | 批量解析(多链接/收藏夹/合集) |
| POST | `/api/batch/download` | 批量启动下载 |

### 队列管理(5)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/tasks` | 所有任务列表 |
| POST | `/api/task/delete` | 删除任务(可选删文件) |
| POST | `/api/tasks/clear` | 清除所有已完成记录 |
| POST | `/api/tasks/clean-invalid` | 清除下载失败 + 文件失效的记录 |
| GET | `/api/stats` | 统计面板 |

### 内容浏览(8)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/history?page=&size=` | 观看历史(带观看进度) |
| GET | `/api/follow-updates?force=` | 关注 UP 投稿(写缓存) |
| GET | `/api/followings` | 我关注的全部 UP(写缓存) |
| GET | `/api/fav-folders` | 收藏夹列表(写缓存) |
| GET | `/api/fav-content?media_id=` | 收藏夹内容 |
| GET | `/api/fav-seasons` | 我追的合集(写缓存) |
| GET | `/api/season-content?season_id=` | 合集内容 |
| GET | `/api/watchlater` | 稍后再看(写缓存) |

### 缓存(5,与上一节 5 个写缓存的端点一一对应)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/cache/follow_updates` | 读关注 UP 投稿的上次结果 |
| GET | `/api/cache/followings` | 读关注列表的上次结果 |
| GET | `/api/cache/fav_folders` | 读收藏夹列表的上次结果 |
| GET | `/api/cache/fav_seasons` | 读合集列表的上次结果 |
| GET | `/api/cache/watchlater` | 读稍后再看的上次结果 |

### UP 清单(6)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/up-list` | UP 清单列表(含 fetched_at / quality) |
| POST | `/api/up-list/add` | 添加 UP(主页 URL 或 mid,打基线) |
| POST | `/api/up-list/remove` | 移除 UP |
| POST | `/api/up-list/set-quality` | 设置某 UP 自动下载的清晰度 |
| POST | `/api/up-list/reset-seen` | 重置某 UP 的已见标记(下次检查视其全部投稿为新增) |
| POST | `/api/up-list/check` | 手动触发一轮检查(后台线程)。响应 `started`:`true` = 本次启动了新一轮;`false` = 已有一轮在进行中,本次直接跳过(不排队、不发 B 站请求),`message` 给出对应文案。两种情况 `ok` 均为 `true`——「进行中」不是错误,是如实告知 |

### 配置(2)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/config` | 当前配置 |
| POST | `/api/config` | 更新配置(部分更新,持久化) |

---

## 十、文件结构

```
bilibili-downloader/
├── app.py              (686 行) Flask 路由、本机同源守卫、限流 429 映射
├── auto_refresh.py     (349 行) 自动追更（串行、复合游标、续扫、失败退避）
├── bilibili_api.py     (460 行) B 站读接口客户端（历史/收藏/合集/关注/UP 投稿）
├── bilibili_auth.py    (232 行) 登录与会话（扫码/Edge/cookies.txt + cookiejar 缓存）
├── config.py           (182 行) 运行时配置（_FIELDS 字段表驱动）
├── downloader.py       (535 行) 全局下载 worker/gate、重试、进度、取消、通知
├── extras.py           (268 行) 文件名清洗/组装 + bvid 提取 + 弹幕（protobuf）
├── files.py            (273 行) 落盘名对齐/冲突处理/跨平台文件管理器唤起
├── pagecache.py        (58 行) 页面数据缓存（打开秒显、30 天未更新清理）
├── parsing.py          (159 行) yt-dlp 解析（单视频/多 P/播放列表）
├── quality.py          (126 行) 清晰度标注与 format 归一化（纯函数）
├── request_governor.py (221 行) 上游并发、分类间隔、账号冷却和状态快照
├── storage.py          (54 行) JSON 原子写、损坏备份与 bool 成功语义
├── taskstore.py        (364 行) 任务表、durable-first CRUD、文件定位与统计
├── up_list.py          (211 行) UP 清单、复合游标与续扫状态
├── templates/index.html(388 行) 单页骨架(纯结构)
├── static/
│   ├── css/main.css    (533 行) 全部样式(含主题变量)
│   └── js/             17 个 ES modules，共 2323 行
│       ├── app.js      入口 + window 桥(内联 onclick 兼容层)
│       ├── lib.js      页控制器框架(共享状态/无限滚/操作条/缓存优先)
│       ├── pages/*.js  六页各一控制器
│       └── *.js        util/icons/net/theme/quality/login/settings/drawer/newdl
├── tests/              304 个单元测试(conftest.py 把运行时数据隔离到临时目录)
│   ├── test_app.py                 (67) 路由 / 缓存层 / 前端结构 / window 桥
│   ├── test_auth.py                (22) 登录、B 站接口、风控参数
│   ├── test_auto_refresh.py        (29) 自动下载轮询 / 合规限频与文档一致
│   ├── test_config.py              (5)  字段表驱动配置
│   ├── test_docs.py                (4)  CHANGELOG 结构 / README 版本横幅一致
│   ├── test_downloader.py          (82) 重试/取消/状态集合/统计
│   ├── test_extras.py              (10) 弹幕解析 / 文件名清洗
│   ├── test_filename_alignment.py  (55) 落盘名对齐 / 冲突 / 文件定位
│   ├── test_quality.py             (12) 清晰度归一化与降档
│   ├── test_storage.py             (10) 原子写 / 损坏备份 / 运行时数据隔离自检
│   └── test_up_list.py             (8)  清单数据层 / 清晰度单一来源
├── data/               运行时(gitignore)
│   ├── config.json     用户设置
│   ├── tasks.json      下载任务历史
│   ├── up_list.json    UP 清单 + last_seen
│   ├── cache/          各数据源上次结果(秒显用)
│   ├── sess.json       扫码登录缓存
│   └── cookies.txt     cookies.txt 导入
├── 启动.command         Mac 双击启动(首次装依赖,启动前自动 kill 旧实例)
├── CHANGELOG.md        ← 版本变更日志
├── docs/
│   ├── REQUIREMENTS.md  ← 本文档
│   ├── BACKLOG.md       ← 产品待办 + 历史批次记录
│   ├── COMPLIANCE.md    ← 法律风险边界与合规护栏
│   └── superpowers/     设计文档 + 实现计划
└── requirements.txt
```

### 模块依赖方向(单向,不成环)

```
storage / extras / quality / request_governor   纯工具与上游治理层
        ↑
config / bilibili_auth / taskstore              配置、身份、任务持久化
        ↑
files / up_list / pagecache                     文件、UP 清单、页面缓存
        ↑
parsing / bilibili_api                          解析与 B 站读接口
        ↑
downloader / auto_refresh                       全局下载调度与自动追更
        ↑
app                                             路由、本机安全边界与异常映射
```

`build_filename` 住在 `extras.py` 而不是 `files.py`:`taskstore` 定位文件也要用它,
而 `files` 依赖 `taskstore`——纯命名逻辑必须待在依赖链底部,否则成环。

---

## 十一、数据隐私

- 所有数据(登录态/任务/UP清单/设置/缓存)全部存在本地 `data/` 目录
- 扫码二维码走 B 站官方接口
- Edge 模式只读本机浏览器 cookie
- **不上传任何服务器**,不收集任何数据
- 本工具仅供个人留存已授权内容使用

---

## 十二、关键 bug 修复记录

| 版本 | 问题 | 根因 | 修复 |
|---|---|---|---|
| v1.x | 扫码登录后状态不更新 | SESSDATA 从 Set-Cookie 提取不可靠 | 改从 `data.url` query 参数提取 |
| v1.x | 登录态永远显示未登录 | `get_login_status` 只认扫码 session | 复用下载用的同一套 cookie 来源 |
| v1.x | 进度条不显示 | hook 没把 status 从 queued 改成 downloading | hook downloading 分支设 status |
| v1.x | 关注更新触发 B 站风控 | 6 并发 × 30 UP = 30 个 arc/search 必然触发 412 | 改串行 + 1.5 秒间隔 |
| v1.x | 取消下载未立即生效 | progress hook 没抛 DownloadCancelled | hook 检查 cancel 抛异常 |
| v1.x | 收藏夹接口静默失败 | 模块未 import yt_dlp 抛 NameError 被裸 except 吞 | 顶层 import + 只捕获 DownloadError |
| v1.x | 关注列表切回关注动态卡死 | 整函数级 in-flight 锁挡切回 | cachedLoad 拆分缓存渲染与后台请求 |
| v2.0 | 网格首屏无法滚动 | 视口算行数恰铺满一屏,滚动条不出现 | 首屏多渲染一行 + maybeFillScreen() |
| v2.0 | `collected/list` 永远空 | 没带 `platform=web` 参数 | 接口必带 platform=web |
| v2.0 | 改了代码不生效 | 旧进程占着 5050 端口 | 启动器启动前自动 kill 旧实例 |
| v2.1 | 扫码确认了仍显示「未扫码」 | 二维码图接口自行 generate,产生了与轮询 key 不同的新 key | 图接口改用前端传入的配对 url 画码 + passport 域名白名单 |
| v2.1 | 任务史可能被清零 | `_persist` 非原子写,崩溃留半截 JSON 且解析失败静默当空 | tmp+rename 原子写 + 独立写锁;损坏备份 `.corrupt-<ts>` |
| v2.1 | 登出后旧身份最长残留 5 分钟 | 登出发生在 Edge cookie 提取期间,提取结果又写回缓存 | 会话域/数据域各自加锁且不嵌套,严格排序 |
| v2.1 | 下载记录标题与磁盘文件名对不上 | yt-dlp `%(title).200B` 按字节截断,CJK 下边界错位 | 命名权收归 Python(build_filename)+ 落盘后强制对齐 |
| v2.1 | 「批量下载」按钮点了没反应 | 模块化重构后 `batchDownload` 漏写 `export`,window 桥上是 undefined | 补 export;加桥↔export 双向一致性测试 |
| v2.1 | 重试窗口内重复下载拦截失效、「清除全部记录」会删掉在跑任务 | 后端三份手写的活跃状态元组都漏了 `retrying` | 收敛为 `taskstore.ACTIVE/TERMINAL` 唯一来源 + 前后端一致性测试 |
| v2.1 | 「删除记录 + 文件」可能删掉邻居视频的文件 | `_task_files` 用**未清洗**的 `title[:30]` 做前缀匹配 | 改用完整期望名 + 分隔符边界(`_stem_belongs`) |
| v2.1 | 标题含 `/ : ?` 的任务永远定位不到文件 | 同上:拿原始 title 去匹配已被换成 `_` 的磁盘名 | 匹配前先过 `sanitize_filename` |
| v2.1 | 删除后残留孤儿 .xml/.srt/.jpg,体积统计少算 | 有 `actual_filename` 时只返回主文件 | `_file_family` 把副文件纳入同一族 |
| v2.1 | 非 macOS 上「定位文件」接口 500 | 硬编码 `open -R`;`check=False` 挡不住命令不存在 | `files.reveal_path/open_dir` 按平台分发并吞 OSError |
| v2.1 | 取消/中断的任务 SSE 不收流 | 服务端只判 `done/failed`,靠前端主动 close 才停 | 收流条件改用 `taskstore.TERMINAL` |
| v2.1 | 开了「按 UP 建子目录」后磁盘用量显示 0 | `disk_bytes` 只 `iterdir()` 扫顶层 | 改 `rglob("*")` 递归统计 |
| v2.2 | 搜索过滤后勾选,删除 / 下载打到另一条记录(含 `unlink` 磁盘文件,不可恢复) | `renderDefaultList` 对过滤后的数组 `forEach`,发出的是「第几个命中」而非 `state.items` 的原始下标 | 下标唯一来源收口为 `visiblePairs(q)`,遍历 `state.items` 本身 |
| v2.2 | 下载后改名可能把另一个视频的文件(连带其副文件)改成本任务的期望名 | `_find_actual_file` 兜底用 `expected_stem[:10]` 前缀匹配,短名时退化为无边界整名前缀 | 兜底分支改引用 `taskstore.stem_belongs`(精确 / 分隔符后缀),不留第三份匹配规则 |
| v2.2 | 合集下完一个分 P 后,其余分 P 全被当已下载而跳过 | 「已下载」判定只看 BV 号,前后端两份实现都丢了分 P 维度 | 粒度改为 (BV 号, 分 P);键规则收口为 `extras.dl_key` / `page_from_url` + 前端 `util.js` 同名对应物,分隔符常量测试绑定 |
| v2.2 | 连点「立即检查新视频」会连续串行打 B 站接口(触碰账号级风控) | `refresh_once` 用阻塞 `with _lock:`,后来者排队补跑而非跳过;路由无条件回 `ok` | 改非阻塞 `_lock.acquire(blocking=False)` + `try/finally`;新增 `is_running()`,路由如实回报 `started`,删掉前端 30 秒假禁用窗口 |
| v2.2 | 取消 `interrupted` 任务 → 永久僵尸态,卡住重复下载拦截与「清除全部记录」 | `downloader` 两处硬编码终态元组 `("done","failed","cancelled")` 漏了 `interrupted` | 两处改引用 `taskstore.TERMINAL` |
| v2.2 | 统计「今日」按滚动 24 小时,昨晚完成的任务今早仍计入 | `compute_stats` 的 `day_start = now - 86400`,与同函数 `month_start`(自然月)口径不一 | `day_start` 改 `int(time.mktime(today.timetuple()))`,与 `month_start` 同一套写法 |
| v2.2 | 双语言字幕改名后丢掉语言标签,第二种语言变永久孤儿文件(「删除记录 + 文件」清不掉、体积统计少算) | `_rename_with_sidecars` 用 `side.suffix` 算副文件新名,而 `.suffix` 只取**最后一段**扩展名,`<stem>.zh-Hans.srt` 的 `.zh-Hans` 被算进 stem;两种语言于是撞成同一个目标名,后到的那个被 `exists()` 静默跳过、留在旧 stem 下,而 `_file_family` 按**新** stem 找族 | 副文件改取 `source.stem` 之后的整条扩展名链 `side.name[len(source.stem):]`;主文件仍用 `.suffix`(要把 `视频.f137.mp4` 改出干净的 `新名.mp4`)是刻意的不对称,依赖 glob 模式保证切片成立 |
| v2.2 | 在下载记录页重新下载一条分 P 记录,下回来的是 P1(全程不报错) | 六页共用的提交口 `libDownload()` 写死 `page: 1`;且 `loadDownloads()` 的字段白名单漏了 `page`,`it.page` 恒 `undefined`——两处缺失互相掩盖 | 白名单补 `page: t.page \|\| 1`,提交体改 `page: it.page \|\| pageFromUrl(it.url)`;沿用「任务侧读存储字段 / 查询侧解析 URL」的不对称原则 |
| v2.2 | 多链接粘贴下载时,带 `&p=` 的分享链接下成 P1;`?p=abc` 提交出 `null` | `newdl.batchDownload` 里第三份手写解析(`includes('?p=')` + `parseInt`)与唯一来源 `util.pageFromUrl`(正则 `[?&]p=(\d+)`)结论不同,而 B 站地址栏 / 分享链接普遍把 `p` 排在 `spm_id_from` / `vd_source` 之后 | 改调 `pageFromUrl`;加守卫测试:除 `util.js` 外整个 `static/js/` 禁止出现 `'?p='` 字面量(按整行扫、不排除注释) |
| v2.2 | B 站接口在局部模块各自 sleep/retry，触发风控后其他链路仍继续请求 | 没有账号级共享并发、分类间隔和冷却状态 | 新增 `RequestGovernor`：全局在途上限、分类抖动、412/429 冷却、统一 429 与状态快照 |
| v2.2 | 写盘失败后接口仍可能宣称成功，或出现有下载无线索的幽灵任务 | 内存可见/线程启动早于 durable snapshot，写入无成功返回值 | 关键状态只在 `write_json=True` 后提交；任务按“原子查重 → 持久化 → 内存可见 → 入队”创建 |
| v2.2 | 自动追更同秒投稿或第 4 页以后内容可能永久漏掉 | 单 BVID 游标且把单轮 3 页预算误当完整扫描 | `ts + same_ts_bvids` 复合游标、持久 continuation、首页漂移检测与 durable retry |
| v2.2 | 下载页旧请求/SSE 在切页后覆盖其他页面状态 | 只看当前 mode，无法识别 downloads → other → downloads 的 ABA 响应 | `_loadSeq` 请求代次 + mode 双守卫；非下载页只更新快照和角标 |
| v2.2 | 本机 Flask 接口可能被 DNS rebinding 或跨站页面调用 | 只依赖绑定地址，没有验证 Host 与浏览器来源 | 全方法 loopback Host 守卫 + Origin/Referer/Sec-Fetch-Site 严格同源校验 |