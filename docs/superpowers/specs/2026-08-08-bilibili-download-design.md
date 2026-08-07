# Bilibili Download — 设计文档

- **日期**: 2026-08-08
- **项目名**: bilibili download
- **目标用户**: B 站大会员用户,希望把视频高画质地留存到本地
- **一句话目标**: 粘贴 B 站视频地址 → 选清晰度 → 稳定下载到本地,支持大会员专属画质(1080P+/4K)

---

## 1. 背景与关键约束

### 1.1 流媒体技术事实
- 高画质(1080P+/4K/杜比)走 **DASH 格式**,音视频分轨为两个文件,下载后必须用 ffmpeg 合并成单 MP4。
- 低画质才有单文件 MP4。
- 未登录时 B 站后端把画质限制到 480P。**必须带登录态 cookie(主要是 `SESSDATA`)才能拿到高画质。**

### 1.2 登录方案选型(已定)
排除"账号密码+验证码"路线:B 站网页登录触发极验滑块/点选验证码,无法无人值守通过,体验极差。

采用 **三种登录方式并存,UI 可切换**:
| 方式 | 体验 | 用途 |
|---|---|---|
| **① 扫码登录**(主推) | 网页显示二维码,手机 B 站 App 扫码确认 | 首次登录 / cookie 失效时 |
| **② 读 Edge cookie** | 一键,`--cookies-from-browser edge` | 用户平时用 Edge 登 B 站 |
| **③ 导入 cookies.txt** | 装扩展导出文件上传 | 兜底 |

下载时后端按优先级使用 cookie:扫码 cookie > Edge > cookies.txt。

### 1.3 下载引擎(已定)
- **yt-dlp** 当作 Python 库调用(比命令行解析更稳,能拿 progress_hooks)。
- **ffmpeg** 合并音视频(环境已装 8.1.2)。
- 不自研下载逻辑——稳定性远不如工业级工具。

## 2. 架构

```
浏览器 (localhost:5000)              Python 后端 (Flask)
┌────────────────────┐            ┌──────────────────────┐
│ ① 登录区(扫码/Edge/ │            │ /api/login/*         │
│    cookie三种方式)  │◄──────────►│  扫码生成/轮询接口     │
│ ② 粘贴URL           │            │                      │
│ ③ 列出清晰度+视频信息│◄──────────►│ /api/parse           │
│ ④ 选画质→点下载      │            │   yt-dlp extract_info│
│ ⑤ 实时进度条        │◄───SSE────│ /api/download        │
│ ⑥ 完成打开文件夹     │            │   yt-dlp 下载+合并    │
└────────────────────┘            └──────────────────────┘
                                         │
                                   yt-dlp (引擎)
                                   ffmpeg (合并)
```

### 2.1 后端模块划分
- **`app.py`** — Flask 路由层,薄。负责 HTTP/SSE 接口,业务委托给下面模块。
- **`bilibili_auth.py`** — 登录鉴权模块。
  - `qrcode_generate()` → 调 `passport.bilibili.com/x/passport-login/web/qrcode/generate`,返回二维码 URL + qrcode_key
  - `qrcode_poll(qrcode_key)` → 轮询 poll 接口,状态码:`86101`未扫 / `86090`已扫未确认 / `86038`失效 / `0`成功(拿到 SESSDATA)
  - `get_cookie_arg()` → 返回传给 yt-dlp 的 cookie 参数(扫码 cookie / edge / cookies.txt 之一),带优先级
  - `check_login()` → 用当前 cookie 调 nav 接口验证登录态,返回用户昵称/是否大会员
- **`downloader.py`** — 下载封装模块。
  - `parse(url, cookie_arg)` → 用 `yt_dlp.YoutubeDL.extract_info(listformats=True)` 拿标题/封面/UP主/时长/可用清晰度列表
  - `download(url, format_id, cookie_arg, task_id)` → 用 progress_hooks 上报进度,异步执行
  - 进度状态存内存 dict,task_id 索引,供 SSE 读取
- **`config.py`** — 配置(下载目录、端口、cookie 缓存路径)。

### 2.2 前端
- 单个 `templates/index.html`,原生 JS,零框架零构建。
- 三个区块:登录区 / 解析+下载区 / 任务队列区。
- 进度用 SSE(`EventSource`)订阅 `/api/progress/<task_id>`。

## 3. 数据流

### 3.1 扫码登录流
```
前端                  后端                      B站
  │──generate──────►│──GET generate──────────►│
  │◄─二维码URL──────│◄─url + qrcode_key────────│
  │ 显示二维码        │                          │
  │ 用户手机扫码确认   │                          │
  │──poll(每2秒)──►│──GET poll──────────────►│
  │  (循环)          │◄─code: 86101/86090/0─────│
  │◄─status────────│                          │
  │ code=0时:        │                          │
  │  存SESSDATA到本地 │                          │
  │  刷新登录状态显示  │                          │
```

### 3.2 下载流
```
前端                       后端                    yt-dlp/ffmpeg
  │──parse(url)──────────►│──extract_info─────────►│
  │◄─info+formats─────────│◄─清晰度列表────────────│
  │ 用户选画质              │                        │
  │──download(url,fmt)──►│ 启动后台线程             │
  │  task_id=xxx           │──YDL.download─────────►│
  │──EventSource(/progress/xxx)                     │
  │  (SSE长连接)            │  progress_hooks──────►│
  │◄─{pct,speed,eta}──────│  (合并完成)             │
  │◄─{status:done}────────│                        │
```

## 4. API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/login/status` | 当前登录状态(昵称/大会员/登录方式) |
| GET | `/api/login/qrcode` | 生成二维码,返回 `{url, qrcode_key}` |
| GET | `/api/login/qrcode/poll?key=xxx` | 轮询扫码状态 |
| POST | `/api/login/cookie-txt` | 上传 cookies.txt(兜底) |
| POST | `/api/login/use-browser` | 切换到"读 Edge cookie"模式 |
| POST | `/api/login/logout` | 清除本地 cookie |
| POST | `/api/parse` | `{url}` → `{title, cover, uploader, duration, formats:[]}` |
| POST | `/api/download` | `{url, format_id}` → `{task_id}` |
| GET | `/api/progress/<task_id>` | SSE 流,推送下载进度 |
| GET | `/api/tasks` | 当前所有任务列表 |

## 5. 错误处理
- **未登录就下载**: 提示去登录,不静默失败。
- **cookie 失效(401/-352)**: 提示重新扫码,不继续。
- **清晰度不可用**(如非会员想下 4K): yt-dlp 会报错,前端友好提示。
- **音视频合并失败**: 捕获 ffmpeg 异常,任务标记 failed,保留已下载分片供续传。
- **yt-dlp 报 WBI 签名/网络错**: 重试 1 次,仍失败标记 failed 并显示原始错误。

## 6. 文件结构
```
bilibili-download/
├── app.py                  # Flask 主入口 + 路由
├── bilibili_auth.py        # 登录/鉴权
├── downloader.py           # yt-dlp 封装
├── config.py               # 配置
├── requirements.txt        # yt-dlp, flask
├── templates/
│   └── index.html          # 单页前端
├── static/                 # (图标等,可空)
├── data/                   # 运行时数据(cookie缓存,gitignore)
│   └── cookies.txt
├── 启动.command             # Mac 双击启动
├── README.md               # 使用说明
└── docs/                   # 设计文档
```

## 7. 不做(YAGNI)
- 不做 Electron 打包。
- 不做番剧整季/收藏夹批量下载复杂 UI(先单视频+简易队列)。
- 不做弹幕/字幕(后续按需)。
- 不做账号密码登录(技术上不可行,见 1.2)。

## 8. 测试策略
- **登录模块**: 单元测试 mock B 站接口,覆盖四种状态码。
- **解析模块**: 对一个公开视频跑 `extract_info`,断言返回 formats 非空。
- **端到端手测**: 跑一个普通视频(无需登录)+ 一个大会员视频(需扫码)。
- 自动化测试用 pytest;HTTP 接口用 Flask test client。

## 9. 环境前提(已确认满足)
- macOS (darwin 25.5.0 arm64)
- ffmpeg 8.1.2 ✅
- yt-dlp 2026.07.04 ✅
- python3 3.14 ✅
- node 26 (不依赖,但存在)
