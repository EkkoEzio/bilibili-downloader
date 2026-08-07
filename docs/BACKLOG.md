# bilibili download — 产品待办 (Backlog)

> 这是后续迭代的唯一待办清单。每条标注:状态 / 说明 / 技术可行性备注。
> 已完成项见文末。新对话里说「按 BACKLOG 继续迭代」即可。

技术栈速览:Python+Flask 后端(把 yt-dlp 当库)、单页原生 JS 前端、ffmpeg 合并、
本地运行 `http://localhost:5050`(端口在 config.py)。登录用扫码/Edge/cookies.txt 三策略
(`bilibili_auth.get_cookiejar()` 统一拿 cookiejar,带 5 分钟缓存)。

---

## 待办:内容浏览类(A)

### A2 关注 UP 主投稿
- 状态: pending
- 说明: 拉取我关注的所有 UP 主最新投稿,合并按时间倒序展示,多选下载。
- 技术备注:
  - 关注列表 API 已验证可用:`/x/relation/followings?vmid={mid}`(返回 mid/uname/face)。
    注意旧路径 `/x/relations/followings` 是 404,要用 `/x/relation/followings`。
  - 单个 UP 的投稿**不要**直接调 `/x/space/.../arc/search`(需 WBI 签名,裸调 -403)。
    改用 yt-dlp:`space.bilibili.com/{mid}` 当 playlist,flat 模式可列出全部投稿(已验证)。
  - 关注多时逐个拉投稿慢,需并发+缓存。

### A3 自定义 UP 清单 + 一键刷新看新增(用户重点需求)
- 状态: pending
- 说明: 维护一个 UP 主清单(mid 或主页链接),点「刷新」拉每个 UP 的最新视频,
  只显示上次刷新之后的**新增**,免去复制链接。
- 技术备注:
  - 复用 A2 的 yt-dlp space 解析。
  - 在 `data/` 存「up 清单」+「每个 UP 的已读时间戳/已见 bvid 集合」,刷新时做增量对比。
  - 需要前端一个「管理 UP 清单」的小界面(增删 UP)。

### A4 我的收藏夹浏览
- 状态: pending
- 说明: 列出我所有收藏夹,点进去看内容,多选下载(比粘收藏夹链接更直接)。
- 技术备注:
  - 收藏夹列表已验证:`/x/v3/fav/folder/created/list-all?up_mid={mid}`(返回 id/fid/title/media_count)。
  - 单个收藏夹内容可用 yt-dlp `medialist/detail/ml{fid}` 或 fav resource list API。

### A5 稍后再看列表
- 状态: pending
- 说明: 直接展示「稍后再看」,多选下载。
- 技术备注: yt-dlp 支持 `https://www.bilibili.com/watchlater`(已验证,空列表也正常返回)。

---

## 待办:体验/健壮性类(B)

### B1 下载失败自动重试
- 状态: pending
- 说明: 网络抖动导致的失败自动重试 2 次,减少手动重下。
- 技术备注: 在 `downloader._run_download` 捕获网络类异常后重试;注意区分「真失败」与「取消」。

### B2 下载设置(并发数/限速)
- 状态: pending
- 说明: 界面可配同时下载数、单任务限速,避免占满带宽。
- 技术备注: yt-dlp 支持 `ratelimit`(字节/秒);并发数现在 start_batch 里用 Semaphore(默认2),
  把它做成可配置存 `data/config.json`。

### B3 下载目录界面可选
- 状态: pending
- 说明: 界面上改下载目录,不用改 config.py。
- 技术备注: DOWNLOAD_DIR 现在是 config 常量,改成运行时可变+持久化到 `data/config.json`。

### B4 历史卡片显示观看进度
- 状态: pending
- 说明: 观看历史里标注「看到 xx%」,方便续看/决定要不要下。
- 技术备注: 历史项顶层有 `progress`(秒)和 `duration`,相除即百分比。

### B5 列表搜索过滤
- 状态: pending
- 说明: 历史/收藏/批量列表长,按标题/UP主实时筛选。
- 技术备注: 纯前端,对已渲染的 items 做 filter 重渲染即可。

### B6 后台托盘/窗口置顶
- 状态: pending(工作量大,慎做)
- 说明: 后台下载不占窗口。
- 技术备注: 需打包成桌面应用(Tauri/Electron),改造大。当前「本地网页+后台线程」已基本满足,
  优先级最低,除非用户明确要桌面形态。

---

## 待办:视觉类(C)

### C1 深色模式
- 状态: pending
- 说明: 夜间护眼。
- 技术备注: 前端 CSS 变量 + `prefers-color-scheme`,或加个手动切换开关存 localStorage。

### C2 视频卡片网格布局
- 状态: pending
- 说明: 历史/收藏列表改网格(带封面),更像 B 站、更直观。
- 技术备注: 前端 CSS grid,复用现有卡片渲染。

### C3 统计面板
- 状态: pending
- 说明: 显示已下载总数、总大小、本月下载等。
- 技术备注: 遍历 `data/tasks.json` 的 done 记录统计;文件大小可在下载完成时记录进 task。

---

## 已完成(供参考,不重复做)

- 核心:扫码/Edge/cookies.txt 登录、解析选清晰度、yt-dlp+ffmpeg 下载合并、SSE 实时进度条
  (进度条曾修过 status 不切换的 bug)、取消下载、重复下载拦截、任务历史持久化、
  完成后「在 Finder 中显示」、未登录提示、多P分集、本地二维码(不依赖第三方)。
- 批量:多链接/分P选集/收藏夹合集三种批量下载(限2并发)。
- A1 观看历史:浏览+多选下载+打开页面自动刷新+封面修复(B站CDN反盗链需 no-referrer)。
- 队列管理:单选/多选删除(可选删记录或删记录+文件)、清除所有记录。

## 迭代建议顺序

A3(用户最想要)→ A4 → A2 → A5 → B1/B3/B5 → C1/C2 → B2/C3 → B6(最后/可选)
