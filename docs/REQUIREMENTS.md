# bilibili download — 完整需求文档

> 本文档描述项目当前**已实现的全部功能**,基于代码实际状态整理(v2.1.0, 2026-08-24)。
> 14 个 Python 模块(3027 行)+ 前端三分离(模板 388 行 / CSS 533 行 /
> 17 个 ES modules 共 2050 行),37 个 API 路径。单元测试 183 个全过。
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
- 缓存键有白名单(`pagecache.py` 的 `KEYS`),防任意文件读写;
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
- 网格首屏不可滚动修复(首屏多渲染一行 + 自动补块)

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
- 批量并发(默认 2,可配 1-4),其余排队

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
- 后台按设置间隔(默认 30 分钟,下限 10 分钟)串行检查
- 单 UP 拉取间间隔 ≥5 秒(B 站 arc/search 接口对账号级敏感,
  6 并发 × 30 UP = 30/30 全部 412,实测必串行)
- 自适应页大小:刚拉过 24h 内只用 5 条小页,降低响应量;
  爆发更新翻页兜底(上限 3 页)
- 限流时不推进「已见」标记,下一轮重试,不漏视频
- 也可手动触发 `/api/up-list/check`(后台线程运行,前端不阻塞)

### R22 数据持久化
- `data/up_list.json`:`ups`(清单)+ `last_seen`(各 UP 已见标记)+ `fetched_at`(上次拉取)
- 兼容老数据:`auto` 字段读取时丢弃,`quality` 字段补默认空串(最高画质自动)

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

### R27 端口
- 默认 `5050`(避开 macOS ControlCenter 占用的 5000 端口)

### R28 B 站限流(关键合规边界)
- **arc/search 接口**对**账号级别**限流敏感
- 实测:6 并发 × 30 UP = **30/30 全部 HTTP 412**,风控持续 5+ 分钟
- **修复**:所有 arc/search 走串行 + 间隔
- **collected/list 必须 `platform=web`**:否则返回空列表(实测踩坑,接口源码反查)
- **不影响限流的**:history / watchlater / fav-content(单请求,分页只是参数)
- 遇 412 静默返回 `[]`,不抛异常;UI 兜底为空状态文案

### R29 自动任务合规
- 自动检查属 B 站「自动程序」条款场景,间隔刻意保守(≥5 秒/UP),
  UP 间隔 ≥5 秒,轮询间隔下限 10 分钟
- 详见 `docs/COMPLIANCE.md`

### R30 未做项
- **后台托盘/窗口置顶**:经评估不做(需打包成 Tauri/Electron 桌面应用,
  改变项目形态,投入产出比低)

---

## 九、API 接口清单(37 个路径 / 38 个 route)

> `/api/config` 一个路径两个方法(GET/POST),故 route 数比路径数多 1。
> 另有 `/` 返回单页骨架。

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

### 解析/下载(7)
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
| POST | `/api/up-list/check` | 手动触发一轮检查(后台线程) |

### 配置(2)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/config` | 当前配置 |
| POST | `/api/config` | 更新配置(部分更新,持久化) |

---

## 十、文件结构

```
bilibili-downloader/
├── app.py              (501 行) Flask 路由层(@api_errors 统一异常出口)
├── bilibili_auth.py    (206 行) 登录与会话(扫码/Edge/cookies.txt + cookiejar 缓存)
├── bilibili_api.py     (394 行) B 站读接口客户端(历史/收藏/合集/关注/UP 投稿)
├── downloader.py       (384 行) 下载执行引擎(线程/重试/进度/取消/通知)
├── taskstore.py        (264 行) 任务表 + tasks.json 原子持久化 + 文件定位 + 统计
├── parsing.py          (160 行) yt-dlp 解析(单视频/多P/播放列表)
├── files.py            (232 行) 落盘名对齐/冲突处理/跨平台文件管理器唤起
├── quality.py          (126 行) 清晰度标注与 format 归一化(纯函数)
├── config.py           (182 行) 运行时配置(_FIELDS 字段表驱动)
├── up_list.py          (139 行) UP 清单数据层(per-UP quality)
├── auto_refresh.py     (130 行) 自动下载轮询(串行,新投稿直入队)
├── pagecache.py        (58 行)  页面数据缓存(打开秒显)
├── storage.py          (48 行)  统一 JSON 持久化(原子写 + 损坏备份)
├── extras.py           (203 行) 文件名清洗/组装 + bvid 提取 + 弹幕(protobuf)
├── templates/index.html(388 行) 单页骨架(纯结构)
├── static/
│   ├── css/main.css    (533 行) 全部样式(含主题变量)
│   └── js/             17 个 ES modules,共 2050 行
│       ├── app.js      入口 + window 桥(内联 onclick 兼容层)
│       ├── lib.js      页控制器框架(共享状态/无限滚/操作条/缓存优先)
│       ├── pages/*.js  六页各一控制器
│       └── *.js        util/icons/net/theme/quality/login/settings/drawer/newdl
├── tests/              183 个单元测试(conftest.py 把运行时数据隔离到临时目录)
│   ├── test_app.py                 (46) 路由 / 缓存层 / 前端结构 / window 桥
│   ├── test_auth.py                (22) 登录、B 站接口、风控参数
│   ├── test_auto_refresh.py        (15) 自动下载轮询 / 合规限频与文档一致
│   ├── test_config.py              (5)  字段表驱动配置
│   ├── test_docs.py                (4)  CHANGELOG 结构 / README 版本横幅一致
│   ├── test_downloader.py          (23) 重试/取消/状态集合/统计
│   ├── test_extras.py              (8)  弹幕解析 / 文件名清洗
│   ├── test_filename_alignment.py  (30) 落盘名对齐 / 冲突 / 文件定位
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
storage / extras / quality          纯工具层(无项目内依赖)
        ↑
config ─┤                           运行时配置(字段表驱动)
        ↑
taskstore                           任务表 + 持久化 + 文件定位
        ↑
files ──┤                           落盘名对齐(依赖 taskstore)
        ↑
parsing / bilibili_api              解析 / B 站读接口(依赖 bilibili_auth)
        ↑
downloader                          下载执行(facade,再导出下游常用名)
        ↑
app                                 路由层
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