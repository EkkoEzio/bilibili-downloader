# bilibili download

本地网页应用,把 B 站视频下载到本地留存。支持大会员高画质(1080P+/4K)。

## 快速开始

1. **双击 `启动.command`**(首次会自动创建虚拟环境并安装依赖,稍等片刻)
2. 浏览器会自动打开 `http://localhost:5050`(若没自动打开,手动访问)
3. 选择一种登录方式(见下),然后粘贴视频地址 → 解析 → 选清晰度 → 下载

## 环境要求

- macOS(已在 Apple Silicon 测试)
- 已安装 ffmpeg(下载引擎依赖):`brew install ffmpeg`
- Python 3.10+(首次运行会自动创建 `.venv` 并装好 yt-dlp / flask)

## 三种登录方式

应用右上角显示当前登录状态。登录区有三个标签页:

### 1. 扫码登录(推荐)
- 点"显示二维码"
- 用**手机 B 站 App 扫码**确认(和平时登录一样)
- 一次登录长期有效;失效后重新扫一次即可
- ✅ 最安全,密码不经过本软件

### 2. 使用 Edge
- 前提:你已在 Edge 浏览器登录过 bilibili.com
- 点"使用 Edge 登录态",软件直接读取 Edge 中已有的登录态
- ✅ 最省事,如果平时用 Edge 登 B 站,推荐这个

### 3. cookies.txt(兜底)
- 浏览器装扩展 **"Get cookies.txt"**(或类似)
- 在 bilibili.com 页面用扩展导出 cookies.txt
- 把内容粘贴到文本框,点"导入 cookies.txt"

## 下载说明

- **清晰度**:解析后会列出该视频所有可用画质。大会员能看到并下载 1080P+/4K 等高画质。
- **音视频合并**:高画质是 DASH 格式(音视频分轨),软件自动用 ffmpeg 合并为单个 mp4。
- **保存位置**:默认 `~/Downloads/bilibili`,文件名用视频标题。
- **进度**:队列区实时显示下载百分比、速度、剩余时间。

## 常见问题

**Q: 下载时提示清晰度缺失 / "you have to become a premium member"?**
A: 没带登录态。换扫码登录,或确认 Edge 里已登录且是大会员账号。

**Q: 登录突然失效?**
A: B 站 cookie 会过期。重新扫一次码,或重新点"使用 Edge 登录态"刷新。

**Q: 扫码一直"等待扫码"?**
A: 二维码有效期约 3 分钟,过期后重新点"显示二维码"生成新的。

**Q: 端口 5050 被占用?**
A: 改 `config.py` 里的 `PORT`。

**Q: 想改下载目录?**
A: 改 `config.py` 里的 `DOWNLOAD_DIR`。

## 项目结构

```
├── app.py              # Flask 后端(路由)
├── bilibili_auth.py    # 扫码登录与 cookie 策略
├── downloader.py       # yt-dlp 封装(解析/下载/进度)
├── quality.py          # 清晰度标注(纯函数)
├── config.py           # 配置
├── templates/index.html# 单页前端
├── 启动.command         # Mac 双击启动
├── requirements.txt    # 依赖
└── tests/              # 单元测试
```

## 技术说明

- 下载引擎:[yt-dlp](https://github.com/yt-dlp/yt-dlp)(工业级,自动处理 WBI 签名、音视频合并)
- 合并:本机已装的 ffmpeg
- 后端:Python + Flask(本地运行,127.0.0.1,不对外暴露)
- 前端:原生 JS 单页,无框架,零构建

本工具仅供个人留存已授权内容使用。
