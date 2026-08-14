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
- 状态: done(2026-08-08)
- 实现: config.py 新增 MAX_CONCURRENT(1-4,默认2)+ RATELIMIT_MBPS(默认0=不限);
  set_config 部分更新,失败回滚。start_batch 默认用 config.MAX_CONCURRENT,
  _ydl_opts 注入 ratelimit。
- 前端: 设置区加「同时下载数」「单任务限速」下拉。
- 验证: 设 3/1/10(截到4) 全生效;重启持久化;16 测试不破。
- 坑: 'MAX_CONCURRENT' 同时作为上限4 和变量名会互相覆盖,已改名上限为 MAX_CONCURRENT_LIMIT。

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
- 状态: **不做(2026-08-08 决定)**
- 说明: 后台下载不占窗口。
- **决定不做的理由**:
  - 当前「本地网页」模式(双击 `启动.command` + 浏览器) 已流畅可用;
  - 实现 B6 = 把整个项目从 Python 网页脚本改造为 Tauri/Electron 桌面应用,
    工作量 4-8 小时,且会**改变项目形态**(变成 .app),而非增量改进;
  - 投入产出比不高,价值被现有形态已基本满足。
- 技术备注: 需打包成桌面应用(Tauri/Electron),改造大。
- 如果未来真要:用 Tauri(Rust 壳)+ 把 `app.py` 作为子进程,加托盘 API。

---

## 待办:视觉类(C)

### C1 深色模式
- 状态: done(2026-08-08)
- 实现: CSS 变量重构(--bg-page/--bg-card/--text-main 等),body.dark 覆盖;
  切换: header 🌙/☀ 按钮(light<->dark),设置区下拉(浅/深/跟随系统);
  持久化: 前端 localStorage 即时,后端 config.json 防丢;POST /api/config 支持 theme。
- 验证: GET 返回 theme=auto;POST dark 成功;POST rainbow 报错;16 测试不破。

### C2 视频卡片网格布局
- 状态: done(2026-08-08)
- 实现: 列表/网格两种模式可切换(默认列表);
  网格用 CSS auto-fill grid,卡宽≥220px,封面占顶,标题限2行省略,复选框叠右上;
  切换按钮持久化到 localStorage。
- 验证: 16 测试不破。

### C3 统计面板
- 状态: done(2026-08-08)
- 实现: downloader 成功时记 downloaded_bytes + finished_at;
  /api/stats 聚合总下载数/总大小/本月/今日 + 磁盘真实字节兜底;
  header 下方 4 项统计条(📦💾📅📆),初始化 + 任务完成时刷新。
- 验证: 下载后接口返回 1/14MB/月1/日1;16 测试不破。

---

## 2026-08-14 优化批次(已完成)

- **收藏夹内容接口修复**:曾因 `bilibili_auth` 未导入 `yt_dlp` 抛 NameError,
  又被裸 `except Exception` 吞掉,接口永远返回空列表(此前 BACKLOG 里"内容API返回0
  属账号隐私"的结论其实是这个 bug)。现:顶层导入 + 只捕获 `DownloadError`,意外
  异常上抛给路由层报 500;补回归测试。
- **取消下载立即生效**:progress hook 检查取消/删除标志,抛
  `yt_dlp.utils.DownloadCancelled` 中止下载(此前会完整下完才标"已取消")。
  顺带修复**存量死锁**:下载异常+取消 的路径在持有 `_tasks_lock` 时调 `_persist()`
  (非重入锁)→ 挂死,新测试逼出并修复。
- **/api/up-updates 防风控重构**:与关注更新同样结论——arc/search 并发触发账号级
  412,改串行 + UP 间 1.5s;mark_seen 复用首次抓取结果(不再为每个 UP 二次发
  ps=1 请求);UP 被限流返回空时**不推进 last_seen**(否则没看到的视频永远不算
  新增)。
- **任务读写健壮性**:`get_task/list_tasks` 返回快照拷贝(SSE/jsonify 与下载线程
  并发读写不再有撕裂风险);persist 节流改"每进入新的 5% 桶落盘一次"(此前同一
  桶内每次回调都写盘)。
- **观看历史「加载更早」**:走服务端分页(每页 100,B站上限)逐页追加 + bvid
  去重。此前一次拉 size=200 实际被后端截到 100,更早的历史完全看不到。
- **深色模式补漏**:删除确认弹窗、UP 管理面板、收藏夹行 hover、tabs 边框等
  硬编码浅色全部换成 CSS 变量(弹窗此前深色下标题几乎不可读)。
- **杂项**:stats「本月」改自然月口径;init 不再重复请求 /api/login/status;
  renderLibList 渲染改传 (item, idx) 对,去掉 indexOf O(n²);关注/UP 刷新
  文案改为真实耗时预期(串行 30 UP ≈ 45 秒)。
- 测试 16 → 34 个。

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
