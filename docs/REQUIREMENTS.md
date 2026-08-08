# bilibili download — 完整需求文档

> 本文档描述项目当前**已实现的全部功能**,基于代码实际状态整理(2026-08-08)。
> 共 50 次提交,3240 行代码(不含依赖),32 个 API 接口,1551 行前端。

---

## 一、项目定位

### R0 核心诉求
- **用户**:B 站大会员,想把视频高画质地留存到本地
- **形态**:本地网页应用(Python Flask 后端 + 单页 HTML 前端),浏览器访问 `localhost:5050`
- **启动方式**:双击 `启动.command`(Mac),首次自动建虚拟环境+装依赖,自动打开浏览器

### R1 技术栈
- 后端:Python 3 + Flask + yt-dlp(下载引擎)+ ffmpeg(音视频合并)
- 前端:原生 JS 单页(无框架,内联 CSS)+ EventSource(SSE 进度)
- 本地运行,数据全部存在 `data/` 目录,**不上传任何服务器**

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
- 任意一种登录成功后,右上角徽章立即显示「✓ 已登录 用户名 [大会员]」
- `get_login_status()` 复用下载用的同一套 cookie 来源(扫码/Edge/cookies.txt 统一)
- **退出登录**:清 cookiejar 缓存 + follow 缓存,确保退出立即生效

### R4 登录区 UX
- 登录成功后**整个登录区隐藏**,直接展示下载区
- 右上角徽章加「退出」链接,可重新登录
- 扫码成功 → **二维码收起** + 显示「✓ 登录成功!」+ 刷新徽章

---

## 三、内容浏览(「我的B站」区,5 个 tab)

> 用户原话:"不用去复制链接过来解析,而是直接在这看到一些视频,多选下载"

### R5 观看历史(history tab)
- 打开页面**自动加载**(已登录时)
- 拉取最近 200 条观看记录
- 每条显示:封面、标题、UP 主、时长、**观看进度百分比**(蓝色细进度条)
- 支持分页(见 R15)
- 多选 → 选清晰度 → 批量下载

### R6 关注更新(follow tab)
- 拉取前 30 个关注 UP 的最新投稿,合并按时间倒序
- **串行 + 1.5 秒间隔**(避免 B 站账号级风控,见 R22)
- 5 分钟缓存(切回 tab 秒回)
- 「⚡ 强制刷新」按钮可跳过缓存重新拉取
- 多选 → 批量下载

### R7 自定义 UP 清单 + 增量刷新(up tab)
- 维护一个 UP 主清单(主页链接或 mid)
- 添加 UP:支持主页 URL 或纯 mid,自动提取 mid
- 点「刷新看新增」→ 只显示**自上次以来的新增视频**(增量检测)
- 「查看全部(含已见)」按钮
- 「管理清单」面板:列出已添加 UP,可逐个移除
- 「重置 seen」可让某 UP 的全部视频重新显示为新增
- 数据持久化:`data/up_list.json`(ups 列表 + last_seen 增量记录)

### R8 收藏夹浏览(fav tab)
- 列出所有收藏夹(标题 + 视频数 + 首字母图标)
- 点进任一收藏夹 → 显示内容
- 「← 返回收藏夹」回到列表
- 多选 → 批量下载

### R9 稍后再看(later tab)
- 展示「稍后再看」列表
- 多选 → 批量下载

### R10 内容浏览通用体验
- **封面显示**:B 站 CDN 反盗链,加 `<meta referrer="no-referrer">` 让图片不带 Referer
- **列表搜索过滤**:搜索框实时按标题/UP 主过滤,勾选保留,过滤计数显示
- **空状态提示**:区分"搜索无结果"和"暂无内容(B 站限流或账号无数据)",均带「↻ 重新加载」按钮
- **分页器**:历史 200 条前端分页,可选 10/20/50/100 条/页(见 R15)

---

## 四、下载能力

### R11 基础下载
- 输入 URL → 解析 → 列出清晰度(带预估文件大小)→ 选 → 下载
- **大会员高画质**:1080P/4K 等(需带登录态 cookie)
- **DASH 音视频合并**:高画质自动用 ffmpeg 合并成单 mp4
- 文件名:`{视频标题}.mp4`(清洗非法字符),分P视频带 P 序号

### R12 进度条
- 实时显示:**蓝色渐变进度条 + 条纹动画 + 百分比 + 速度 + 剩余时间**
- SSE(Server-Sent Events)推送进度
- 修复过 status 不切换导致进度条不显示的 bug

### R13 多 P 分集视频
- 多 P 视频自动识别 → 返回分 P 列表(标题 + 时长)
- 选具体 P → 拉该 P 的清晰度 → 下载
- 文件名带 P 序号(如 `标题 p01 xxx.mp4`)

### R14 批量下载(三种)
- **① 多链接一次下**:多行输入框,每行一个 URL,解析出全部
- **② 分 P 选集下**:分 P 视频勾选要下的 P
- **③ 收藏夹/合集下**:输入收藏夹或合集链接,yt-dlp 原生支持
- 批量并发(默认 2,可配 1-4),其余排队

### R14a 分页器(R15)
- 历史列表前端分页:10/20/50/100 条/页(默认 20)
- 页码导航:上一页 / 1 2 3 ... / 下一页
- 翻页自动滚到列表顶部
- 搜索过滤与分页联动
- 数据少于一页时自动隐藏分页器

---

## 五、下载管理

### R15 下载管理
- **实时进度条**:条纹动画 + 速度 + 剩余时间
- **取消下载**:下载中点「取消」按钮
- **失败自动重试**:最多重试 2 次(共 3 次),递增退避(2/4/6 秒)
  - 用户取消不重试
  - 重试中显示「重试中」状态(橙色 spinner)
- **完成后打开文件**:「在 Finder 中显示」按钮
- **重复下载拦截**:同 URL+清晰度+分P 且仍在跑 → 返回 409

### R16 队列管理
- **单选/多选删除**:每个任务有勾选框(跨重渲染保持)
- **删除选择**:弹窗二选一 → 「只删除下载记录」或「删除记录 + 已下载文件」
- **清除所有记录**:一键清空已完成记录(可选删文件)
- **任务历史持久化**:`data/tasks.json`,重启不丢

### R17 统计面板
- 顶部统计条:📦 总下载数 / 💾 总大小 / 📅 本月 / 📆 今日
- 下载完成时自动刷新
- 任务记录含 `downloaded_bytes` + `finished_at`

---

## 六、设置

### R18 下载设置
- **下载目录**:界面可改(不用改 config.py),自动展开 ~、创建目录、持久化
- **同时下载数**:1-4 可选(默认 2)
- **单任务限速**:不限 / 1/2/5/10/20 MB/s(yt-dlp ratelimit)
- 全部持久化到 `data/config.json`,重启仍在

### R19 主题
- **浅色 / 深色 / 跟随系统** 三选一
- CSS 变量重构(`--bg-page`/`--text-main` 等),`body.dark` 覆盖
- header 有 🌙/☀ 快速切换按钮
- localStorage 即时生效,config.json 防丢

### R20 视图切换
- **列表 / 网格** 二选一
- 网格:auto-fill grid(≥220px/列),封面占顶,标题限 2 行省略,复选框叠右上角
- localStorage 持久化

---

## 七、技术约束与已知限制

### R21 端口
- 默认 `5050`(避开 macOS ControlCenter 占用的 5000 端口)

### R22 B 站限流(重要)
- **arc/search 接口**对**账号级别**限流敏感
- 实测:6 并发 × 30 UP = 30 个请求 → **30/30 全部 HTTP 412**,风控持续 5+ 分钟
- **修复**:关注更新改串行 + 1.5 秒间隔(30 UP = 45 秒),不再触发风控
- **不影响限流的**:history(watchlater/fav 各接口都是单请求,分页也只是参数)
- 遇到 412 静默返回 `[]`,不抛异常

### R23 未做项
- **B6 后台托盘/窗口置顶**:经评估不做(需打包成 Tauri/Electron 桌面应用,
  改变项目形态,投入产出比低,现有「双击启动.command + 浏览器」已满足)

---

## 八、API 接口清单(32 个)

### 登录(7)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/login/status` | 当前登录状态(用户名/大会员) |
| GET | `/api/login/qrcode` | 生成二维码(url + qrcode_key) |
| GET | `/api/login/qrcode-image` | 本地生成二维码 PNG |
| GET | `/api/login/qrcode/poll?key=` | 轮询扫码状态 |
| POST | `/api/login/use-browser` | 切换到 Edge cookie 模式 |
| POST | `/api/login/cookie-txt` | 导入 cookies.txt |
| POST | `/api/login/logout` | 退出登录(清缓存) |

### 解析/下载(6)
| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/parse` | 解析视频(返回标题/清晰度/预估大小/是否多P) |
| POST | `/api/parse-part` | 解析指定分 P 的清晰度 |
| POST | `/api/download` | 启动单视频下载(带重复拦截) |
| POST | `/api/cancel/<task_id>` | 取消下载 |
| GET | `/api/progress/<task_id>` | SSE 推送下载进度 |
| POST | `/api/open-folder` | 在 Finder 中打开下载目录 |

### 批量(2)
| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/batch/parse` | 批量解析(多链接/收藏夹/合集) |
| POST | `/api/batch/download` | 批量启动下载 |

### 队列管理(4)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/tasks` | 所有任务列表 |
| POST | `/api/task/delete` | 删除任务(可选删文件) |
| POST | `/api/tasks/clear` | 清除所有已完成记录 |
| GET | `/api/stats` | 统计面板(总数/大小/本月/今日) |

### 内容浏览(7)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/history?page=&size=` | 观看历史(带观看进度) |
| GET | `/api/follow-updates?force=` | 关注 UP 投稿(5 分钟缓存) |
| GET | `/api/fav-folders` | 收藏夹列表 |
| GET | `/api/fav-content?fid=` | 收藏夹内容 |
| GET | `/api/watchlater` | 稍后再看 |

### UP 清单(5)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/up-list` | UP 清单列表 |
| POST | `/api/up-list/add` | 添加 UP(主页 URL 或 mid) |
| POST | `/api/up-list/remove` | 移除 UP |
| POST | `/api/up-list/reset-seen` | 重置某 UP 的增量标记 |
| GET | `/api/up-updates` | 拉清单中 UP 的新增(增量检测) |

### 配置(2)
| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/config` | 当前配置(目录/并发/限速/主题) |
| POST | `/api/config` | 更新配置(部分更新,持久化) |

---

## 九、文件结构

```
bilibili-download/
├── app.py              (458行) Flask 路由层
├── bilibili_auth.py    (415行) 登录/内容API/限流
├── downloader.py       (486行) yt-dlp 解析/下载/重试/进度/取消
├── quality.py          (92行)  清晰度标注(纯函数,有单测)
├── config.py           (157行) 运行时配置(目录/并发/限速/主题)
├── up_list.py          (81行)  UP 清单数据层
├── templates/index.html(1551行) 单页前端
├── tests/              16 个单元测试
│   ├── test_quality.py
│   ├── test_auth.py
│   └── test_downloader.py
├── data/               运行时(gitignore)
│   ├── config.json     用户设置(目录/并发/限速/主题)
│   ├── tasks.json      下载任务历史
│   ├── up_list.json    UP 清单 + 增量记录
│   ├── sess.json       扫码登录缓存
│   └── cookies.txt     cookies.txt 导入
├── 启动.command         Mac 双击启动(含首次自动装依赖)
├── requirements.txt    yt-dlp/flask/requests/qrcode/pillow
└── docs/
    ├── REQUIREMENTS.md  ← 本文档
    ├── BACKLOG.md       产品待办(A/B/C 系列 + B6 决策)
    └── superpowers/     设计文档 + 实现计划
```

---

## 十、数据隐私

- 所有数据(登录态/任务/UP清单/设置)全部存在本地 `data/` 目录
- 扫码二维码走 B 站官方接口
- Edge 模式只读本机浏览器 cookie
- **不上传任何服务器**,不收集任何数据
- 本工具仅供个人留存已授权内容使用

---

## 十一、历次修复记录(关键 bug)

| 时间 | 问题 | 根因 | 修复 |
|---|---|---|---|
| 早期 | 扫码登录后状态不更新 | SESSDATA 从 Set-Cookie 提取不可靠 | 改从 `data.url` query 参数提取 |
| 早期 | 登录态永远显示未登录 | `get_login_status` 只认扫码 session | 复用下载用的同一套 cookie 来源 |
| 早期 | 进度条不显示 | hook 没把 status 从 queued 改成 downloading | hook downloading 分支设 status |
| 早期 | "加载中"一致显示 | HTML 嵌套错误,重复 fav-folder-list 未被 display:none 包裹 | 删重复块 + 唯一 ID + 容器默认 display:none |
| 中期 | find_active_by_url 漏 page | 多 P 视频第二页起无法去重 | 加 page 参数 |
| 中期 | MAX_CONCURRENT 同名 bug | 上限常量和运行时变量同名互相覆盖 | 改名 MAX_CONCURRENT_LIMIT |
| 中期 | 退出登录后接口仍用旧 cookie | cookiejar 缓存 5 分钟未清 | clear_session 清缓存 |
| 后期 | 收藏夹点下钻不显示 | `#fav-content-list` 元素被误删 | 补回 HTML 元素 + 防御性 null check |
| 后期 | 关注更新触发 B 站风控 | 6 并发 × 30 UP = 30 个 arc/search 必然触发 412 | 改串行 + 1.5 秒间隔 |
| 后期 | 切 tab 串数据 | `_libItems` 共享变量不清空 | 切 tab 清空 + 关注用独立缓存 |
| 后期 | SSE 断线不重连 | onerror 直接关闭 | 加重连(最多 3 次递增间隔) |
