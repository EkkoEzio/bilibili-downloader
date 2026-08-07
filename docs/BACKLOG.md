# bilibili download — 产品待办 (Backlog)

> 这是后续迭代的唯一待办清单。每条标注:状态 / 说明 / 技术可行性备注。
> 已完成项见文末。新对话里说「按 BACKLOG 继续迭代」即可。

技术栈速览:Python+Flask 后端(把 yt-dlp 当库)、单页原生 JS 前端、ffmpeg 合并、
本地运行 `http://localhost:5050`(端口在 config.py)。登录用扫码/Edge/cookies.txt 三策略
(`bilibili_auth.get_cookiejar()` 统一拿 cookiejar,带 5 分钟缓存)。

---

## 待办:内容浏览类(A)

### A2 关注 UP 主投稿
- 状态: done(2026-08-08)
- 实现: /api/follow-updates;复用 yt-dlp WBI 签名调 arc/search 拿标题/时间/封面;
  线程池并发各 UP、合并按时间倒序、2分钟缓存;前端「关注更新」标签多选下载。
- 注意: arc/search 密集调用会触发 B 站限流(部分 UP 返回空),已加错峰延迟;
  若结果偏少属限流,稍等再刷新即可。
- 说明: 拉取我关注的所有 UP 主最新投稿,合并按时间倒序展示,多选下载。
- 技术备注:
  - 关注列表 API 已验证可用:`/x/relation/followings?vmid={mid}`(返回 mid/uname/face)。
    注意旧路径 `/x/relations/followings` 是 404,要用 `/x/relation/followings`。
  - 单个 UP 的投稿**不要**直接调 `/x/space/.../arc/search`(需 WBI 签名,裸调 -403)。
    改用 yt-dlp:`space.bilibili.com/{mid}` 当 playlist,flat 模式可列出全部投稿(已验证)。
  - 关注多时逐个拉投稿慢,需并发+缓存。

### A3 自定义 UP 清单 + 一键刷新看新增(用户重点需求)
- 状态: done(2026-08-08)
- 实现: `up_list.py` 数据层(ups+last_seen);/api/up-list 增删+reset-seen;
  /api/up-updates 并发拉清单中各 UP,对比 last_seen 只返回新增(自动 mark_seen);
  主页链接支持自动提取 mid(uname 通过 arc/search 拉一次,失败也可);
  前端「UP 清单」tab 含添加输入框+管理面板+「刷新看新增/查看全部」双按钮。
- 用法: 「UP 清单」tab → 输入主页链接或 mid 添加 → 点「刷新看新增」只看到自上次以来新增的。
- 注意: 同样受 B 站 arc/search 限流影响(见 A2),部分 UP 会返回空。

### A4 我的收藏夹浏览
- 状态: done(2026-08-08)
- 实现: /api/fav-folders 拉列表; /api/fav-content?fid 拉内容(yt-dlp medialist);
  前端收藏夹 tab 显示所有收藏夹(首字母+视频数),点进看内容,多选下载,返回按钮。
- 验证: 列表3个收藏夹正确(默认/学习/python);内容接口通(本账号收藏夹内容API返回0,
  属账号隐私或限流,非功能问题)。
- 注意: 私密收藏夹或限流时内容为空。

### A5 稍后再看列表
- 状态: done(2026-08-08)
- 实现: 直接调 B站 `/x/v2/history/toview` 接口(比 yt-dlp 快,字段含 tview_at 加入时间);
  前端第 5 个 tab「稍后再看」,刷新后多选下载。
- 验证: 接口通,5 个 tab 渲染、JS 语法校验通过。

---

## 待办:体验/健壮性类(B)

### B1 下载失败自动重试
- 状态: done(2026-08-08)
- 实现: _run_download 重试循环 max_retries=2(共尝试3次),递增退避 sleep(2/4/6 秒),
  每次重建 YoutubeDL 实例;取消标志置位立刻退出;sleep 后二次检查。
  前端 statusCN 加 retrying,带 spinner 显示「第N次重试」。
- 验证: 16 测试通过(含 retry_loop_present、cancel_exits、retrying 渲染)。
- 行为: 网络抖动自动重试,永久错误不重试,3 次全败才标记 failed。

### B2 下载设置(并发数/限速)
- 状态: pending
- 说明: 界面可配同时下载数、单任务限速,避免占满带宽。
- 技术备注: yt-dlp 支持 `ratelimit`(字节/秒);并发数现在 start_batch 里用 Semaphore(默认2),
  把它做成可配置存 `data/config.json`。

### B3 下载目录界面可选
- 状态: done(2026-08-08)
- 实现: config.DOWNLOAD_DIR 运行时可变,启动从 data/config.json 读;
  set_download_dir 自动展开~+创建目录+持久化。
  downloader/app 不再快照,改用 config.DOWNLOAD_DIR 实时读。
  POST /api/config 设目录;前端 ⚙ 设置区输入路径保存。
- 验证: 默认/POST/无效路径/重启持久化全过;16 测试不破。
- 注意: 其他模块**不要** `from config import DOWNLOAD_DIR`(拿快照,改不到)。
  要 `import config; config.DOWNLOAD_DIR`。

### B4 历史卡片显示观看进度
- 状态: done(2026-08-08)
- 实现: fetch_history 归一化返回 watched_seconds + watched_pct;
  前端 renderLibList 在标题下方加蓝色细进度条 + 百分比(0<x<100% 才显示)。
- 验证: API 返回 3.7%/22.1% 真实进度,16 测试不破。

### B5 列表搜索过滤
- 状态: done(2026-08-08)
- 实现: 4 个 tab 的列表共用搜索框;输入实时过滤标题/UP主;
  勾选按 _libItems 原始索引跟踪(_libChecked Set),过滤不丢勾选;
  全选/全不选只对可见项生效;切换 tab 清空搜索框。
- 验证: JS 语法通过,16 测试不破。

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
