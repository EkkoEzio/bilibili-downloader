# b站视频下载 · Agent 笔记（项目知识库）

> 从 mmx agent/user 记忆迁入的项目级事实，2026-11-24 整理。
> 入口文档是仓库根 AGENTS.md；本文件是逐条原始记录，按主题分节，改动相关模块前先读对应小节。
> 版本流水查 CHANGELOG.md，待办与需求查 docs/BACKLOG.md、docs/REQUIREMENTS.md。

## S1 架构与链路（解析、播放 token、直连 API）

### b站下载:播放「莫名卡顿」定案=CDN节点间歇停顿+前端硬seek放大,已落软纠偏+埋点(2026-11-24)

- 规则:B 站 playurl v3 下发的 CDN/PCDN 节点(upos-sz-estgoss、mcdn.bilivideo.cn 家用节点族)存在 0.6-2s 的**流中停顿**,长读探针(连续读 16MB 记逐 64KB 间隔)6/6 轮复现于 17:35-37 窗口,17:40 同主机转净、停顿漂到 mirrorcoso1——停顿随时间/节点漂移,是节点自身负载抖动,非本地中继(同窗内 estgoss 两轮全净即为主机特异性证据)。瞬时吞吐探测(单次 curl 计速)抓不到这种抖动,均速 600KB/s+ 看着健康照样卡;日志零 403/零异常≠链路干净。
- 排除项都有实测:mihomo 实时连接表(connections API,unix-socket)确认 api.bilibili.com/bilivideo.com/mcdn 全 DIRECT(11-23 的 403 是当时全局模式,规则文件 10-27 起就有 DIRECT);当天无下载任务;取流路由(Range 透传/断连关流/连接池)与 werkzeug threaded 无问题。
- 卡顿机制:停顿撞上浏览器缓冲低位→视频 underrun→音画偏差超阈值→前端硬 seek 音轨→「画面停+声音跳」双重观感。前端放大器已改(player.js syncAudioToVideo):0.25s 内不动、0.25-0.6s 用 1±0.15 倍速微调追平(基准读 SPEED_KEY,不覆盖用户档位)、>0.6s 才硬 seek。
- 新增埋点闭环:player.js 把 video/audio 的 waiting/stalled(2s 节流)与 resume(绕节流,带 stallMs)POST /api/client-log(app.py 新路由,bd.client logger,≤8 条/次越界 400),落 app.log 后可与取流访问日志按时刻对上——下次报障直接看「播放诊断 kind=waiting ... ahead=...s」。
- 契约测试:test_player_audio_drift_soft_correction / test_player_buffering_diagnostics_reported / test_client_log_endpoint_validation(test_app.py 末尾区);全量 521 passed。遗留项:后端读缓存(每 token 常驻预读连接+环形缓存,浏览器 Range 全部命中缓存)是治本方向,若软纠偏后观感仍卡再评估;埋点数据显示 ahead 低企+waiting 密集即坐实缓冲击穿,可反向指导要不要降档。

### b站下载:在线播放解析已切直连 API 快速路径(2026-11-20)

- 未签名 x/player/playurl 带应用登录 Cookie 完整生效(qn=127&fnval=4048&fourk=1,实测 1080P 档位齐全,与 yt-dlp 路径一致);view(0.26s)+playurl(0.28s)≈0.55s,稳态含开销 0.76s;yt-dlp 路径 2.4-2.6s。多 P 用 view.pages[p-1].cid 取 cid 与时长;失败(番剧/-404/风控/未登录)一律回落 yt-dlp;resolve 按 URL 缓存 10min、只缓存成功结果。
- 杜比/FLAC 在 dash.dolby/dash.flac,故意不取(<audio> 解码不稳,标准 AAC 是口径);档位标签复用 quality._format_label({"quality":id},height)+同高度留码率最高,与 classify_formats 归一化一致。
- bilibili_api 新增 fetch_video_view/fetch_playurl(失败静默 None,意外异常才 warning);online.py CDN 转发用 _CDN_SESSION 连接池 + getter 兼容模式(requests.get is not _requests_get 让路测试桩,与 bilibili_api._get 同款)。
- 契约测试只整体 stub online.resolve/open_upstream,内部重构安全。.venv 才是项目解释器(服务器 ps 显示 Cellar framework 路径是 venv symlink 的解析结果);服务器运行时 import app 会撞 instance_lock(锁检查在启动期),冒烟靠 pytest 全量。
- README v2.7.0 曾漏底部链接引用(docs 存量失败),发 v2.8.0 时已补;test_auth 两个失败是工作区另一批未提交改动的存量问题。
- 用户再提 iframe 嫁接:维持 v2.5.3 结论(跨站 Cookie 拿不到登录态只给 360P/480P,tests/test_app.py:3520 契约测试锁死),用提速后的内置播放器替代,完整体验走播放器右上角「B 站」按钮,不嫁接。

### B站在线播放「解析200取流403」:CDN直链绑定签发出口IP,TUN抖动是根因(2026-11-23实测定案)

- 规则:playurl v3 直链带 oi=签发出口IP,CDN 校验取流 IP 一致性,不一致 403。TUN 切节点/分流让 api.bilibili.com 与 CDN 域族(akamaized/bilivideo.com·cn/mcdn PCDN,xy115x231x41x14xy 这类 xy 编码主机=IP 编码 PCDN 节点)出口错位或抖动时,表现为解析全 200、/online/stream 全 502、app.log 刷「在线播放上游返回 403」+偶发 SSL EOF;前端断流自恢复(重解析×2)救不了持续错位窗口。出口稳定即自愈;取流本身没坏,缺的是自愈——2026-11-23 晚已加固:open_upstream 撞 403 就 _resolve_uncached 绕过解析缓存重签,把同档位新直链换到同一 token 上重试一跳(3 秒冷却内同视频只重签一次,找不到同档不乱换,重试仍 403 即认输),解析缓存同步换新;真网实测签名改坏→403→自动重签→206。海外出口还会让部分视频(实测 Rickroll 官方 MV)报 geo-restricted,已改:online.VideoUnavailableError→路由 404 video_unavailable(含 BV 不存在的 KeyError(bvid)),前端照服务端原因提示且不重发。实测定案补充:故障窗口出口是海外 IP,23:16 变成国内联通直连——换出口=根因的第二证据,再遇「点谁都播不了+部分视频地区限制」先看 TUN 是否把 B 站流量切到了海外出口。
- 诊断套路:①app.log 先分 403(被拒)与 SSL EOF(链路断);②直链 oi= 整数解码成点分 IPv4,对比 api.bilibili.com/x/web-interface/zone 的出口;③standalone 探针(.venv python 直接 import bilibili_auth/BROWSER_HEADERS 复刻解析链)实测 CDN,区分「码坏了」vs「网分家了」。open_upstream 失败日志已带 CDN host 且请求行脱敏。重启 app.py 会把活跃任务改标 interrupted(终态不续传)——重启前先查 tasks.json 无非终态任务;直接 nohup python app.py 拉起即可(别走启动器,它会 open 浏览器)。
- 适用:本项目任何「解析成功播不了」类报障;普适于「API 正常但媒体/下载 403」的代理链路。

### b站下载:token滑动续期修「播一半自动暂停」+长沙发分段+Esc统一(2026-11-17 第三轮)

- 自动暂停根因:online.py 播放 token 30 分钟 TTL 硬过期不续期;播放中浏览器取下一段 Range → lookup 判过期 → 404 token_expired → <video> 静默停住(在线模式原来没有任何 error 处理,310 行的 error 提示只挂本地模式)。修复两件套:①lookup() 滑动续期(每次取用续 30 分钟,created+105 分钟绝对上限,防续过 CDN 直链自带约 2h 的 e= 有效期);②player.js 加 v.onerror 在线模式断流自恢复 recoverOnlineStream(重新解析 /api/online/qualities → fillQualities → playOnlineAt(keepTime:true) 断点续播;额度 2 次,onplay 回满,onlineUrl/onlineMeta 模块级保存;用尽提示手动重开)。
- 长沙发分段(用户原话「做成一个长沙发点击切换的那种」):uc-auto-row/uc-auto-pill(UP/追更/收藏档位)与 pl-segs/pl-seg(播放器倍速/清晰度)统一——容器 border-radius:999px+overflow-x:auto 裁圆头、gap:0、分段无边框同底色相邻连成整条(浅底 var(--bg-soft)/封面暗底 rgba 白);「不自动下载」选中态白卡面改反色 var(--text-main) 底+var(--bg-card) 字(浅色=黑底白字),原 var(--text-mute)+bg-soft 灰底几乎看不见被打回。纯 CSS 改造,JS 模板与 onclick 全部不动。
- 关键坑:main.css 受契约测试锁定(test_app.py):.uc-auto-pill.on{color:#fff;background:var(--accent-bright 前缀、三上下文 marker 规则内必须保留首个 color:var() 与 background:var()(对比度测试用正则提取 var 名算 WCAG,改成 transparent 会 IndexError)、.pl-seg.on{、.up-list/.fav-folder/.fc-head 的行规则前缀。改 UI CSS 前先 grep tests 里的断言字符串。
- 运维:改 online.py/app.py 需重启;lsof -ti:5050 拿 PID 后 kill,用启动脚本同款 .venv/bin/python app.py(run_in_background)重启;系统 python3 没装 requests,验证脚本必须走 .venv/bin/python。
- 验证:全量 pytest 179 passed;浏览器实测 Esc(history 输入→Esc→val 空且 meta 复位)、长沙发四语境(封面网格/fc-head/fav-folder/up-list 全部单行 rowH22 不换行不溢出)、倍速 2×↔1× 与清晰度切换正常;滑动 TTL 离线单测通过(lookup 续期、上限、过期拒绝)。

### b站下载:ES module 导入带 ?v= = 双实例,已全量移除(2026-11-17 修复)

- 根因类:同一模块被不同 specifier 导入(?v=x vs 裸路径)= 两个 URL = 两个模块实例 = document 级监听器注册两遍。症状按模块而异:player.js toggle 先 pause 又 play 互相抵消(播放/暂停必坏);follow.js 关注点击会双 POST;search.js 回车双搜索但被 inflight.abort() 掩盖(第二次 abort 第一次,所以长期没暴露)。
- 修复:static/js 下 11 处导入的 ?v= 全部剥掉(app.js 9 处/newdl.js 1 处/pages/fav.js 1 处),跨文件配对(downloads 被 app.js+newdl.js 导、seasons 被 app.js+fav.js 导)同步变裸,static/js 已零 ?v=。index.html 入口 app.js?v= 与 main.css?v= 保留:单次加载不冲突,且 test_app.py:3297 锁定这两个串。
- 规则:ES module 的 import 路径永不带 ?v=;静态文件 no-cache+ETag 本就不需要版本号,以后改 JS 只改文件、不 bump 版本号、新导入一律裸路径。
- 验证法:浏览器 run_script 给 HTMLMediaElement.prototype.play/pause 打补丁记录调用栈后真实点击,一次点击应只产生一对调用(video+audio 双元素同翻是本应用的音画同步设计,不是双触发)。

### yt-dlp cookiesfrombrowser 只读一个 profile(2026-11-22 实测)

- **规则**:yt-dlp 的 `cookiesfrombrowser=('edge',)`(不带 profile)在 profile=None 时,会在浏览器用户目录里递归找 **mtime 最新**的那个 Cookies 库——只读一个 profile。同一台 Edge/Chrome 通常有多个 profile(Default + Profile N),站点登录态一般只在其中一个 → 读错 profile 时上游接口照样 200,但没有登录态,表现为「浏览器里明明登录了,程序却显示未登录」。
- **证据**:b站下载项目实测:Default(娱乐)=449 cookie / 20 个 bilibili / **无 SESSDATA**;Profile 2(工作)=2329 / 40 / **有 SESSDATA**。yt-dlp 源码 `cookies.py` 的 `_extract_chrome_cookies`:profile=None → `_find_files(search_root,'Cookies')` → `_newest()` 取 mtime 最大那个。
- **修法**:自己枚举 profile(读 `<browser_dir>/Local State` 的 `profile.info_cache` + 目录兜底),逐个用 `cookiesfrombrowser=(browser, profile)` 显式提取,取第一个真带目标站点 SESSDATA 的;**不要重写解密**(yt-dlp 对 macOS 钥匙串、Chromium v10/v20 的处理最完整)。解析与下载必须用同一个 profile,否则「界面已登录、下载却带另一个 profile 的 Cookie」。
- **适用**:任何用 yt-dlp 读浏览器 Cookie 的项目。另:Windows 上 Edge 自 127 起对 Cookie 做了 App-Bound 加密,yt-dlp 多数情况解不开,浏览器免登录在 Windows 不可靠。

## S2 前端定稿（卡片、封面覆盖件、分段选择器、顶栏）

### 合集/收藏自动下载分段与网格卡胶囊布局(2026-11-17)

规则:追更合集/我的收藏的下载配置已从下拉迁为与 UP 卡相同的平铺分段——seasons.js collectionAutoPick(app.js 桥同名,已弃 collectionAutoChanged/_qualityOptionsHtml;点当前态不重复请求,__off→/api/collection/remove、未订阅→add(title 从卡片 data-title 带入)、已订阅→set-quality,后端 already:true 兜底);「已订阅在前」排序走 _orderDirty 缓存,只在列表加载/订阅状态迟到回调时重算,点分段/订阅永不重排(位置不跳原则扩展到这两页)。网格卡「标题+分段」同排放不下:7 颗胶囊 max-content≈357px vs 卡内容宽≈366px,flex-basis:0 的标题只分剩余被挤到 1.2px——正文 flex-wrap:wrap + 分段行 flex:0 0 100%(标题一行、分段整行在下),行式视图 nowrap+overflow-x:auto 横滚。
Why: 多胶囊同排时 max-content 与标题剩余空间同量级,会静默把标题挤没,浏览器实测才发现。
Apply when: 往卡片标题行/正文同排塞分段、徽标等多控件前,先按 max-content 估算容量,放不下就换行成「标题一行+控件整行」。

### 模板字符串少一个收尾引号=属性被吞:子串契约测不出,要行为级解析断言(2026-11-17 合集分段 bug 复盘)

规则:给卡片/控件写 JS 模板串时,跨模板段拼接的属性(段1结尾是属性值中间、引号在下一段开头)必须在**每个反引号段结尾**核对引号闭合;契约测试不能只查子串(「data-value 在源码里」≠「属性活到 DOM」),对内联 HTML 模板要新增行为级断言——把模板段按真实插值拼成字符串走 html.parser 解析,断言关键属性独立存活(class 只含样式类、无幽灵属性)。upAutoControlHtml(有引号,工作正常)与 collectionAutoControl(缺引号)逐字对照是定位关键。
Why: 2026-11-17 生产 bug:seasons.js collectionAutoControl 按钮模板 class="uc-auto-pill${...}${...} 段尾少一个 ",浏览器把 data-value="__off" 吞进 class——DOM 7 颗胶囊文本/高亮/onclick 全对(旧子串测试全绿、浏览器 DOM 检查也全对),但 dataset.value 永远空串:点「不自动下载」被守卫当成点当前档静默 return,0 请求;点具体档静默按「最高」提交;未订阅点 off 反而误订阅。取证链:服务器日志 0 条 remove + 用户 15:50 拉过 200(排除旧缓存)+ 磁盘=下发(mtime 对照)+ 浏览器 elementFromPoint/getAttribute 诊断出 data-value=null。
Apply when: 写/改任何拼段式 HTML 模板串后,逐段核对引号闭合;给它交付涉及内联模板的前端改动时,契约测试补一条「插值+解析」行为断言,别只用 grep 子串。

### b站下载:合集/收藏卡升级为视频卡式+切档就地同步+22014幂等修复(2026-11-17 第四轮)

- 卡片结构:合集/收藏网格卡与 UP 卡统一——档位沙发压封面左下角(复用 .uc-actions 暗渐变条,容器 padding-right:96px 给「n 个视频」角标避让),标题下加 .fc-meta 元信息行(UP 名 · 更新时间/最近收藏 · .vi-follow 关注胶囊);行式视图把 UP 名/时间并进 .fav-count 行。卡片 onclick 守卫用 `closest(.uc-auto-row,.fc-meta,.vi-follow)||goPage(...)` 短路表达式——裸 `if(...)` 会被「内联处理器必须挂桥」契约测试打回。
- 数据源:fetch_collected_seasons 已补 upper.name/mid(列表接口自带,零额外请求;注意 upper 必须在列表推导内取,曾写在外面 NameError);收藏夹列表接口没有 fav_time,「最近收藏 x天前」从 /api/fav-content 条目的 fav_at 学习进 localStorage(bd_fav_latest),学到才显示不硬编。UP 卡「共 n 个视频」没有现成免费字段:订阅 UP 可从后台检查的 arc/search data.page.count 顺带回写,非订阅 UP 要额外 sensitive 请求(未实施,已给用户方案)。
- 切档卡顿根因不是后端建任务(切档不发下载请求),是前端收尾 renderPage()/renderUps() 整页重渲染重建全部卡片(封面/头像重挂)闪一下;修复=syncRowSegs(btn 的 .uc-auto-row)就地同步 aria-pressed/class,不重渲染。订阅/退订仍走接口,换档纯本地+set-quality。
- 新增带 .vi-follow 的卡片后必须在对应渲染器末尾调 refreshFollowChips()(renderOverview/renderFolders 原来不调,胶囊永远停模板态「关注」——实测远古野已关注却显示关注才暴露)。follow.js toggleFollow 用 dataset.followed 判方向,依赖 refreshFollowChips 落态。
- 真 bug:B 站「重复关注」实际返回 code=22014(message=已经关注用户无法重复关注),modify_relation 只豁免 22013 → 对已关注 UP 点关注必 500;已改 22013/22014 都按成功(幂等)。
- 运维坑:bash run_in_background 有 1 小时硬上限(bg 任务超时被杀),不能用来托管用户的长驻服务器;重启一律 kill(lsof -ti:5050)后 `open 启动.command`(Terminal 独立进程)。追更检查机制备忘:守护线程每 AUTO_REFRESH_MINUTES(下限10min)一轮+手动 /api/up-list/check,非阻塞锁忙则丢弃;UP 串行间隔 5s 防风控;UP 用时间游标(last_seen ts+bvid+同秒集),合集/收藏用 bvid 基线集合;入队前 downloader.is_downloaded 过滤,失败靠任务史指数退避(≤8 次)。

### b站下载:换档「卡一下」真凶=守卫误吞换档点击(2026-11-18 第五轮)

- 真凶不是网络也不是重渲染:`if (turnOff === !existing) return;` 这条「点当前状态不重复」守卫在**订阅状态下恒为真**(turnOff=false 且 !existing=false),把换档点击整个吞掉——零请求零高亮,看起来像卡死。之前测试「换档正常」的场景全在播放器(另一套代码),卡片档位换档从 Day 1 就被吞。UP 卡同样写法同样中招。修法:守卫只拦真正的重复——`if (!existing && turnOff) return; if (existing && normAutoQuality(existing.quality) === value) return;`。教训:**恒等式守卫要代入两态验一遍**,别只验当初写它的那个场景。
- 顺手落了乐观切换(先亮后请求,失败回滚,`row.dataset.busy` 防叠加):换档 3ms,真正慢的是订阅分支(add 要打 B 站拉基线,0.5-1.6s);app.py /api/up-list/add 加快速路径——清单里已有且游标齐全直接返回,重订阅 1.6s→75ms。游标缺失必须仍走 register_up(否则 fetch_new 游标为空会把历史投稿全部当新增入队)。
- 追更页「追更中」角标已撤(用户:本页全部是追更中,零信息量;网格 uc-badge+行式 fav-tag 都撤),收藏页的条件角标保留+切档收尾用 bd-coll-sub 自定义事件就地补/摘。契约测试断言已翻转。
- 合集卡 UP 名与视频卡 .vi-ident a 同构:space 链接 target=_blank,样式对齐(12px text-soft,hover 品牌粉),不加粗。
- 浏览器实测协议补充:MutationObserver 测高亮延迟时 observer 要在 evaluate 里装、点击用 page.mouse 真实点击;若页面 reload 后还在首页,先点导航再测。data-value='2160P' 等档位值是内部值,与胶囊文案(4K/2K/1080/720)不同,定位分段要用文案匹配。

### b站下载:沙发两端自圆+收藏角标撤下+toview发布时间(2026-11-18 第六轮)

- 长沙发左圆右方的根因:分段本体 radius 0、圆头靠容器 border-radius+overflow 裁出,但容器(flex:1 拉伸)比分段链宽时右端裁不到——UP 卡 uc-actions 宽 617px、分段链只到 317px。修法:`.uc-auto-row .uc-auto-pill:first-child{border-radius:999px 0 0 999px}` + `:last-child{...999px 0}`,播放器 .pl-seg 同款;容器裁切保留作滚动兜底。测试断言已加。
- 收藏页「追更中」角标(网格 uc-badge+行式 fav-tag)也撤了:档位沙发本身就是订阅状态,角标重复;上轮的 patchFavBadge/bd-coll-sub 广播机制随角标一起删(唯一消费者)。追更页上轮已撤。共享模板的 ${badge} 插槽保留(测试断言)。.fav-tag 样式规则保留但已无使用方。
- fetch_watchlater 的 _video_item 原映射 view_at_key='tview_at'——toview 接口条目根本没有该字段(是 add_at),发布时间一直为空;改 'pubdate'(条目自带)与首页卡片口径一致。改 bilibili_api.py 需重启。
- 浏览器验证注意:run_script 30s 硬上限(外层 timeout_ms 无效),location 跳转+reload+多页遍历的超长脚本会 SCRIPT_TIMEOUT,拆小段;每段开头先用 evaluate 确认当前 active 页再动作。

### b站下载:收藏夹拼图封面+合集/收藏发布时间(2026-11-18 第七轮)

- 收藏夹列表接口(created/list-all)的 cover 常为空,卡片一直「暂无封面」。新链路:/api/fav-folder-covers(app.py,内存 TTL 600s)→ fetch_fav_folder_covers(bilibili_api.py,一夹一请求 x/v3/fav/resource/list?order=mtime&ps=18,取前 9 张 https 封面,单夹失败不影响其他夹)→ lib.js collectionCoverMedia 按实际张数选版式:>=9→3x3、4-8→2x2、1-3→单图,皆无才落「暂无封面」;collectionCard 新增 covers 参数向后兼容(追更合集卡不受影响)。fav.js 数据到货后 patch-in-place:媒体插到 .fc-placeholder 之后才能盖住占位(占位符是首个子节点),不整页重渲染;重渲染路径 folderCard 直接带 covers Map。
- 发布时间:x/v3/fav/resource/list 与 x/space/fav/season/list 条目自带 pubtime,两处 view_at_key="pubtime" 零额外请求补齐;fav_time 是收藏时间别混淆。B 站各接口时间字段名不一(watchlater=pubdate/首页=pubdate/这两处=pubtime),先探测原始条目 key 再映射。
- 浏览器 run_script 协议补充:script worker 里没有 document/setTimeout,DOM 读写与等待一律放进 page.evaluate(evaluate 上下文里能用 setTimeout);mcp_browser 在 run_code 内用 tools.mcp_browser 调用。run_script 的 code 是字符串、访问不到外层闭包变量(自由变量直接 ReferenceError),动态参数要用 JSON.stringify 内联进代码字面量或作为 JSON 值传给 page.evaluate 第二参(2026-11-19 实测)。

### b站下载:账号头像+播放默认最高档+Finder按钮挪位(2026-11-18 第八轮)

- 头像:get_login_status(bilibili_auth.py)本来就调 NAV_API,加 "face" 字段(http→https)零额外请求;index.html .acct-avatar 内加 <img id="acct-face" hidden> 压在占位 SVG 上,CSS absolute inset 0;login.js setAccountFace(登录落图/未登录清空/onerror 回落),bd_acct_last 扩展存 face 供下次直接渲染。
- 播放清晰度默认档:原 fillQualities 是「记住上次档,否则 1080P」;且 markQualitySegs 每次把自动选的档写进 bd_play_quality,导致默认 1080P 被自己记忆固化。改为 markQualitySegs(0)(qualities 按高度降序 0=最高)并整个去掉 QUALITY_KEY 记忆;断流自恢复 recoverOnlineStream 必须先存 prevIndex 再 fillQualities(它会重置 state),markQualitySegs(prevIndex) 保档续播。
- 下载记录 Finder 按钮:原与时长胶囊同排(thumb-row)挤得时间左移;挪到 thumb-actions 里 mainPlay 之前(column flex 顺序=上方),CSS .thumb-actions > .vi-reveal 做 34px 圆钮与 vi-play-main 同款(position:static 必须,基类 .vi-play 是 absolute),lv 行视图 28px;lib.js 列表/网格两分支与 downloads.js taskCardHtml 同步改。
- 播放器实测协议:点首页卡 .vi-play-main[data-play-url] 直接 openOnlinePlayer;档位 sofa 读 #pl-quality-segs .pl-seg 的 .on;run_script 里轮询等待(每 500ms evaluate 一次)比单次长 sleep 稳。

### b站下载:顶栏统一刷新/视图/昼夜三钮(2026-11-18 第九轮)

- 顶栏顺序:新建下载 → tb-refresh → tb-view → tb-theme → 设置齿轮 → 账号;_page_toolbar.html 宏简化为只留搜索框,8 处调用点已清;四钮统一 32×32。
- 刷新分发:lib.js refreshCurrentPage() 优先 registerPage 的 ctrl.refresh(订阅页 refresh: refreshSubs 保绕缓存,收藏/追更 retry 兼顾详情态),无控制器则 refreshLib();新页面记得注册 ctrl.refresh。
- 昼夜:theme.js toggleTheme 按当前明暗取反(auto 也落成明确值),syncThemeBtn 跟 body.dark 换 sun/moon 图标;同步写 bd_theme + config-theme select + /api/config;暗色变量块 main.css 原本就有。
- 契约测试:顶栏顺序断言 html.index(tb-refresh)<tb-view<tb-theme<title="设置"(title="设置" 全文唯一);订阅绕缓存断言从 onclick 改为源码断言 refresh: refreshSubs + refreshCurrentPage 分发。
- 浏览器协议:mcp_browser network query 会剥 URL query(验不了 force=1),改用 afterSequence 序列边界对照实验——点按钮后只看新增请求(实测新增 cache/followings+followings 两条即证明强制拉新);改模板已重启服务器。

### b站下载:顶栏 ≤1180px 导航收成纯图标,截图目检别误判(2026-11-18)

- 视口宽 ≤1180px 时 @media(max-width:1180px) 把 .nav-item span display:none,顶栏只剩图标、新建下载只剩 +;浏览器截图验证导航文字时先看 innerWidth,窄了用 page.evaluate 读 span.textContent,别把响应式收纳当成改动丢字。账号钮在 920px 以下同样收(旧媒体规则)。

## S3 发版与打包

### b站下载发版流程(2026-11-17 v2.6.0 实跑)

- 用户说「打个包发一版」= 全套:①CHANGELOG 定版(## [vX.Y.Z] - 日期,补漏条目)→ ②README 首页版本行(> **vX.Y.Z**:一句话亮点)→ ③CHANGELOG 底部链接引用 [vX.Y.Z]: releases/tag/…(test_docs.py 契约,漏了必红)→ ④全量 pytest → ⑤git add -A && commit "release: vX.Y.Z"(带中文要点 body)→ ⑥git tag → ⑦git push origin main vX.Y.Z → ⑧nohup ./启动.command > log 2>&1 & disown 重启并 curl 探针验证。
- GitHub Release 对象自 v2.4.1 起停建(gh release list 最新=v2.4.0),发版只推 tag,不建 Release 页。
- 版本号口径:未发布段有新功能/交互改版 → minor bump;CHANGELOG 的 v2.5.2~v2.5.5 曾长期只写不发,一次 commit 收口时 tag 只打最终号,中间段保留在 CHANGELOG 里作记录。
- 档位契约(2026-11-17 二轮改版后):UP/合集/收藏三处「自动下载」分段共用同一字典 quality.js AUTO_QUALITY_SEGMENTS ↔ up_list.QUALITY_CHOICES(订阅走 import 别名 UP_QUALITY_SEGMENTS;旧精确标签在 up_list._norm_quality/前端 normAutoQuality 读写归一,COLLECTION_QUALITY_CHOICES 双轨表已删除);手动下载独留旧精确标签下拉 quality.js QUALITY_OPTIONS ↔ index.html ab-quality(test_quality_choices_single_source 绑定)。改档位先看这套绑定,新档值别只加进一处。

## S4 测试纪律

### b站下载 前端改动的两个必查项（2026-11-11 修复轮实测）

- 该项目 tests/test_app.py 用「整个前端拼接包」的子串断言锁定大量实现细节（updateLibCount 必须 DOM 口径且不得引用 state.checked、hasVisibleChecks 必须含 DOM 查询串、.uc-auto.on 必须保留 accent-soft、fav-tag/uc-badge 追更中 类名、minmax 轨道公式等）。改前端前先 grep test_app.py 的相关断言，修复要与测试同步改（本次只动过 1 处 mgr 死类标记），改完跑 pytest 全量。
- 本机 Flask 服务（默认 5050）生产模式缓存 Jinja 模板：改 templates/index.html 后 curl 仍是旧 HTML，必须重启进程才生效；static/ 下 CSS/JS 是逐请求读盘、改完即生效。判断「样式没生效」先分清这两种路径。edit 工具在此环境不接受 replaceAll 参数，批量替换用带上下文的唯一匹配逐个改。

### b站下载:模板切片测试的锚点选择(2026-11-21 修 4 个存量失败)

- 规则:①测试切模板区间时锚点必须唯一——data-page="downloads" 侧栏导航与页区块都含,且导航里 history 排在 downloads 前,index(downloads):index(history) 切出空串;要用完整 "<div class=\"page\" data-page=\"...\">" 前缀。顶栏切片必须以 </header> 收口,无界 [:] 会把后续页区块包进去,「顶栏无 X」断言必炸。②HTML 注释会残留函数名,count 断言要数真实属性形态(onclick="..."),别数裸函数名。③改断言的批次交付前必须以落盘后的最终状态重跑全量 pytest——上一批记录「全绿」但 4 个测试实际必炸(切片锚点先天错),别把改前或局部绿当交付依据。
- 实例:topbar_pages 的 right 切片到 EOF 含下载页 newdl;downloads_head 用 data-page= 定位撞导航。test_auth 两处漂移:合集映射新增 uploader/uploader_mid 未同步期望;22014 实为重复关注(2026-11-17 实测,与 22013 同按幂等成功),错误样例改用白名单外 code。
- 适用:任何「slice 大文件/模板做子串断言」的测试;多轮改测试后的全量验证。

## S5 运维

### b站下载:Flask 模板缓存坑——改模板不重启=版本号白发(2026-11-16 对勾死区排障确认)

- app.py 是 debug=False + use_reloader=False,Jinja 编译缓存不查 mtime:改 index.html(含 ?v= 版本号)后不重启服务器,浏览器永远拿旧 HTML。11-16 对勾死区报障的第二层根源:上一批 pointer-events:none 修复+版本号 o 从未到达用户浏览器(服务器一直发 n)。排查口令:curl 首页 grep 版本号,与磁盘模板不一致=该重启了。
- 重启方式:./启动.command(自动优雅接管同项目旧实例、单实例锁、ready 后开浏览器)。从会话后台任务起的服务器必须 nohup ./启动.command > log 2>&1 & disown 脱离,否则会话结束可能带走用户服务器。

## S7 用户定稿的界面口径（原 user 记忆迁入）

### 前端设计终选落地(2026-11-10 第三轮补充)

- 从 4 个存活方向里选定 **B 站 Next 顶栏版**为正式重构基线(先做 A 左侧/B 顶部导航对比后用户落 B);指挥台保留待选,放映室/三舱淘汰。
- 正式重构已落地:templates/index.html + static/css/main.css 全量重写为顶栏版,**JS 层零改动**;7 个旧设计契约测试同步改写为锁定新设计。

### b站下载:改静态 JS 不需要重启服务器,也不需要版本号(2026-11-17 实测)

- 静态文件响应头是 Cache-Control: no-cache + ETag,浏览器每次都会回源校验,改 static/js/*.js 后用户刷新页面(F5)即拿到新代码;服务器只发磁盘文件,无需重启。
- 需要重启的只有:改 app.py(Python 进程代码)和改 templates/*.html(Jinja 编译缓存不查 mtime,见上一条缓存坑)。改完 JS 就去动 ?v= 版本号或重启属于多余动作。

### b站下载:yt-dlp 解析是进程级 CPU 大户,后台预热必须限并发(2026-11-17 实测)

- 单进程 Flask(threaded)里,一次 yt-dlp 解析占一个核秒级 CPU;前端「静默预热」功能若并发放开(曾 6 个并发),整进程被拖住,其它接口实测被堵 23.5s(搜索)。
- 修法三件套:①预热单飞(同一时间 1 个在途);②同一 URL 的解析只跑一次(online.resolve 曾对同一视频连跑两遍 yt-dlp,合并成 parse_video_with_info 一次两用);③重复请求走内存缓存(搜索 5min TTL,同词 1.6s→0.002s)。
- 通用结论:给这个项目加任何「后台预取/预热/自动刷新」时,默认限并发 + 去重 + 缓存,否则会互相拖慢。悬停/点击等用户主动操作的解析不受预热限流影响,走各自路径。

### 视频卡片信息层级定稿：标题后不放任何胶囊（2026-11-17 确认）

- **规则**：视频标题后不放任何衍生胶囊/标签（推荐理由、下载状态等都不要）；状态信息一律放封面（右上角角标体系）或元信息行。关注按钮做成名字后面的内联胶囊（与视频卡元信息行同款），不占封面控件条；长文本截断字段（如 UP 签名）要支持悬停即时气泡看全文。
- **证据/原因**：他先指出标题后「2w点赞」蓝胶囊与封面点赞胶囊重复，让我列全量清单后定稿「标题后面的胶囊全去掉，不要有胶囊」，并点名「下载状态本来就说好放封面右上角，标题后展示纯属多此一举」。
- **适用**：任何视频/UP/合集卡片类 UI；与「精简时不要删状态标记」「状态标记复用既有控件」同一条审查线——信息要有，但只放封面和元信息行两个位置。

### 订阅档位与交互定稿（2026-11-17 第二轮，修正「沙发」语义）

- **规则**：①「沙发」= **平铺的分段选择器**（一排胶囊点哪个切哪个），不是开关——他上一条消息里的「改成沙发的交互」被我误读成开关，这条消息纠正了。②订阅档位只选分辨率：不下载/最高/4K/2K/1080/720，去掉 60 帧独立档与音频；「最高画质」更名「最高」。③同分辨率默认最高帧率（有 144 用 144，没有看 60，再没有就是 30）；选了具体分辨率但没有时，自动降档到所选以下最高的可用档。④切换档位/关注后**列表位置不动**、停用或取关的卡片不立刻消失——即时生效但不重排，重排与移卡只发生在页面加载/刷新时。
- **证据/原因**：原话「统一一起做成沙发，平铺让我快速切换」「相当于只选择分辨率，至于帧率，相同分辨率默认选择帧率高的那个」「位置先不要动，取消关注的也不要立刻消失，而是当我页面刷新后消失」。
- **适用**：任何分段选择器交互与「快速切换」类控件设计；给他做列表管理类 UI 时，切换状态一律不重排当前位置。

### b站下载:搜索能力边界与提速空间(2026-11-17 二轮)

- B 站搜索接口没有「合集」类型 → 合集页不能搜全站,与收藏夹一样只按名称本地过滤(用户接受)。
- 新词首查物理下限 ≈ 上游 0.5-1s;提速三驾马车:前端会话缓存(0ms)→ 后端内存缓存(5min TTL)→ 连接池会话(每次省 0.05-0.15s)。
- requests.Session 接入:测试用 monkeypatch 替换顶层 requests.get,_get 里判断 `requests.get is not _requests_get` 被替换时让路给桩。
- sensitive 1.5s 间隔是风控参数,不在提速范围动(412=全局冷却10分钟)。

### 订阅档位修订：720 退役（2026-11-22 实测 quality.js）

- **规则**：修正「订阅档位与交互定稿(2026-11-17)」②——订阅档位现为 不自动下载/最高/4K/2K/1080/仅音频；720 档已退役（AUTO_LEGACY_QUALITY 把旧 720/720P/720P60/1080P60 选择归一到 1080），最低可选 1080，视频最高不足 1080 时按所选以下最高可用档下载。音频实际保留为「仅音频」档，并未去掉。
- **证据**：static/js/quality.js:15-24，与后端 up_list.QUALITY_CHOICES 绑定测试通过。
- **适用**：任何涉及该订阅档位列表的 UI/后端改动。
