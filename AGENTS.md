# AGENTS.md — b站视频下载

B 站视频本地保存工具：Flask + 原生 ES module 前端，默认端口 5050。
README.md 是功能、登录、打包与发布的权威说明；本文件只放 agent 上手要点与硬规则。
项目知识库：docs/agent-notes.md。版本流水：CHANGELOG.md。待办与需求：docs/BACKLOG.md、docs/REQUIREMENTS.md。

## 运行与验证

    ./启动.command                        # BD_PORT 可覆盖；单实例锁，换端口不绕过
    ./.venv/bin/python -m pytest tests/ -q
    ARCH=arm64 scripts/build_local.sh     # 本机构建，产物在 dist/
    scripts/release.sh [版本号]            # 发版：bump 三处 → 提交 → tag → 推，CI 出五个包

- 解释器一律 .venv/bin/python，系统 python3 没 requests。
- 重启：lsof -ti:5050 拿 PID 后 kill，再用 .venv/bin/python app.py 拉起。不要用 pkill -f 'python app.py' —— 实际进程名首字母大写（Python），静默匹配不到，旧实例继续服务旧模板。
- 重启会把活跃任务改标 interrupted（终态不续传）→ 重启前先查 tasks.json 有无非终态任务。
- 服务运行时 import app 会撞 instance_lock（锁检查在启动期），冒烟靠 pytest 全量。

## 结构

    app.py              路由 / 同源守卫 / SSE / 日志
    online.py           在线播放：DASH 直链解析、token 化媒体流转发
    bilibili_api.py     B 站读接口 + 认证上下文缓存
    bilibili_auth.py    登录、认证快照、Cookie 来源
    browser_cookies.py  Edge 免登录（遍历全部 profile 找真正带 SESSDATA 的）
    parsing.py          yt-dlp 解析与播放列表容量边界
    downloader.py       worker/gate、重试、取消、通知、删除编排
    taskstore.py        任务身份、manifest、容量、持久化与安全文件操作
    auto_refresh.py     追更、原子预约、续扫、失败退避
    quality.py          档位定义与归一化
    up_list.py          订阅清单与档位
    request_governor.py 直接 HTTP 调用的并发 / 间隔 / 冷却
    static/js/          ES module 前端（import 路径零 ?v=）
    packaging/ scripts/ 打包与发版脚本
    docs/agent-notes.md 项目知识库

## 硬规则

- tests/test_app.py 用「整包前端拼接串」的子串断言锁死大量实现细节：改 CSS / 模板 / JS 前先 grep tests 里的断言字符串，否则改对了也红。
- 模板切片测试的锚点必须唯一（data-page="downloads" 这类在侧栏导航和页区块各有一份）。
- ES module 的 import 路径永不带 ?v=：两个 specifier = 两个模块实例 = document 级监听器注册两遍。index.html 的入口 app.js?v= / main.css?v= 保留。
- 改 static/js、static/css 不重启、不 bump 版本号（响应头 no-cache + ETag）。
- 跨模板段拼接的属性必须做行为级解析断言，子串契约测不出「属性被吞」。
- 在线播放 token 走滑动续期（取用即续，带绝对上限防超 CDN 直链 e= 有效期），不要改回硬 TTL —— 那会表现为播一半自动暂停。
- 取流 403 自动重签一次（3 秒冷却内同视频只重签一次），仍失败即如实报错；「解析全 200 但点谁都播不了」先查 TUN 是否把 B 站流量切到了海外出口。
- 视图形态（列表 / 网格）是用户选择，响应式只调密度，不得按宽度改形态。
- 封面底部多枚覆盖件并进同一条 flex 底部条；压在封面上的覆盖件统一实底暗底胶囊。
- 顶栏只留跨页工具，同一目标只留一个入口；主 CTA 放其语义归属页的页头。
- 打包隐私红线：白名单进包 + 暂存目录编译 + 产物扫描；安装包不得含开发者个人信息、家目录路径、账号指纹。
- yt-dlp 解析是进程级 CPU 大户，后台预热必须限并发。

## 改动前必读（docs/agent-notes.md）

| 要改的东西 | 先读 |
|---|---|
| 解析 / 播放 token / 直连 API / 取流 403 | S1 |
| 卡片 / 封面覆盖件 / 分段选择器 / 顶栏 | S2 |
| 发版 / 打包 / 版本号 | S3（另见 README「打包与发布」） |
| 测试 | S4 |
| 重启 / 运维 | S5 |

## 版本号与提交

- 版本号唯一来源是仓库根 VERSION；release.sh 同步 VERSION、CHANGELOG、Inno 脚本，别在别处再写版本号。
- CHANGELOG 顶部「## [未发布]」在发版时转成「## [vX.Y.Z] - 日期」。
- 用户说「推」= 核对改动清单 → 按仓库风格写中文 conventional commit → push → 验证工作区干净、ahead/behind 0/0。
- 交接提示词.md 不入库。
