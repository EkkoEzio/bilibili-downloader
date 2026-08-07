# Bilibili Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个本地网页应用,粘贴 B 站视频地址 → 扫码登录(大会员)→ 选清晰度 → 用 yt-dlp+ffmpeg 稳定下载到 `~/Downloads/bilibili`。

**Architecture:** Python Flask 后端把 yt-dlp 当库调用做解析/下载,前端是零框架单页 HTML,进度用 SSE 推送。登录用 B 站官方扫码接口,cookie 三策略(扫码/Edge/cookies.txt)可切换。

**Tech Stack:** Python 3.14, Flask, yt-dlp (pip), ffmpeg (系统已装 8.1.2), 原生 JS + EventSource。

## Global Constraints

- macOS arm64,项目根目录 `/Users/hushen/Desktop/b站视频下载`,项目名 `bilibili download`。
- 用项目内 venv(`.venv/`),不污染系统 Python。`yt-dlp` 和 `flask` 都走 pip。
- 清晰度判定靠 `height`:360/720/1080/1440/2160;无 `format_note`。音频轨 `vcodec=='none'`,视频轨 `acodec=='none'`。
- 扫码登录状态码:`86101`未扫 / `86090`已扫未确认 / `86038`失效 / `0`成功。`SESSDATA` 在响应 **Set-Cookie** 头里,不在 JSON body。
- 默认下载目录 `~/Downloads/bilibili`,可配置。
- 下载文件名:`{标题}.mp4`,清洗 `\/:*?"<>|` 为 `_`。
- 中文界面;所有面向用户文案用中文。

---

## File Structure

```
bilibili-download/
├── app.py                  # Flask 路由层:登录/解析/下载/SSE/任务
├── bilibili_auth.py        # 扫码登录、cookie 策略、登录态校验
├── downloader.py           # yt-dlp 解析、下载、进度、清晰度标注
├── quality.py              # 纯函数:height→清晰度标签、formats 过滤(可单测)
├── config.py               # 路径与配置
├── requirements.txt        # yt-dlp, flask
├── tests/
│   ├── test_quality.py
│   └── test_auth.py
├── templates/index.html    # 单页前端(登录/解析/下载/队列)
├── data/                   # 运行时:cookie 缓存(gitignore)
├── 启动.command             # Mac 双击启动
├── README.md
├── .gitignore
└── docs/superpowers/...
```

---

## Task 1: 项目脚手架与 venv

**Files:**
- Create: `requirements.txt`
- Create: `config.py`
- Create: `.gitignore`
- Create: `README.md`
- Create: `tests/__init__.py` (空)

**Interfaces:**
- Produces: `config.py` 提供 `BASE_DIR`、`DATA_DIR`、`COOKIE_FILE`、`DOWNLOAD_DIR`、`PORT`,供后续所有模块 import。

- [ ] **Step 1: 初始化 venv 并装依赖**

Run:
```bash
cd "/Users/hushen/Desktop/b站视频下载"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install yt-dlp flask requests
```

- [ ] **Step 2: 写 `requirements.txt`**

```text
yt-dlp>=2026.0
flask>=3.0
requests>=2.31
```

- [ ] **Step 3: 锁定依赖版本**

Run: `pip freeze | grep -iE "^(yt-dlp|flask|requests)==" > requirements.txt`
然后人工核对 requirements.txt 只剩这三行(版本号来自 freeze)。

- [ ] **Step 4: 写 `config.py`**

```python
"""路径与配置。所有模块统一从这里取路径,避免硬编码。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

COOKIE_FILE = DATA_DIR / "cookies.txt"          # 供 cookies.txt 上传 & Edge 导出写入
SESSION_COOKIE_FILE = DATA_DIR / "sess.json"     # 扫码登录拿到的 SESSDATA 缓存

# 默认下载目录,可在运行时改(本工具不改写用户家目录其他位置)
DOWNLOAD_DIR = Path.home() / "Downloads" / "bilibili"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PORT = 5000
BROWSER_COOKIE_SOURCE = "edge"   # 平时用 Edge 登 B 站
```

- [ ] **Step 5: 写 `.gitignore`**

```text
.venv/
__pycache__/
*.pyc
data/
.DS_Store
downloads/
```

- [ ] **Step 6: 写 `README.md` 骨架**(后续任务补内容)

```markdown
# bilibili download

本地网页应用,下载 B 站视频(支持大会员高画质)。

## 快速开始
双击 `启动.command`,浏览器打开 http://localhost:5000

## 详细说明
(见后续)
```

- [ ] **Step 7: git 初始化并提交**

Run:
```bash
cd "/Users/hushen/Desktop/b站视频下载"
git init && git add -A && git commit -m "chore: 项目脚手架 (venv, config, requirements)"
```

---

## Task 2: 清晰度标注纯函数(TDD)

**Files:**
- Create: `quality.py`
- Create: `tests/test_quality.py`

**Interfaces:**
- Produces: `quality.height_to_label(height) -> str`、`quality.label_to_label_cn(label) -> str`、`quality.classify_formats(formats) -> list[dict]`。`classify_formats` 输入 yt-dlp 的 formats 列表,输出归一化的可选项,每项 `{"format_id","height","label","kind":"video"|"audio"|"av","tbr"}`。后续 downloader 用它。

- [ ] **Step 1: 写失败测试 `tests/test_quality.py`**

```python
from quality import height_to_label, label_to_label_cn, classify_formats


def test_height_to_label():
    assert height_to_label(360) == "360P"
    assert height_to_label(720) == "720P"
    assert height_to_label(1080) == "1080P"
    assert height_to_label(1440) == "1440P"
    assert height_to_label(2160) == "2160P"
    assert height_to_label(None) == "未知"
    assert height_to_label(480) == "480P"


def test_label_cn():
    assert label_to_label_cn("2160P") == "4K 超清"
    assert label_to_label_cn("1080P") == "1080P 高清"
    assert label_to_label_cn("720P") == "720P"
    assert label_to_label_cn("360P") == "360P 流畅"


def test_classify_formats_separates_av():
    formats = [
        {"format_id": "30280", "vcodec": "none", "acodec": "mp4a", "height": None, "tbr": 200},  # 纯音频
        {"format_id": "30016", "vcodec": "avc1", "acodec": "none", "height": 360, "tbr": 350},   # 纯视频
        {"format_id": "mp4-1", "vcodec": "avc1", "acodec": "mp4a", "height": 720, "tbr": 800},    # 单体音视频
    ]
    out = classify_formats(formats)
    by_id = {x["format_id"]: x for x in out}
    assert by_id["30280"]["kind"] == "audio"
    assert by_id["30280"]["label"] == "音频"
    assert by_id["30016"]["kind"] == "video"
    assert by_id["30016"]["label"] == "360P"
    assert by_id["mp4-1"]["kind"] == "av"
    assert by_id["mp4-1"]["label"] == "720P"


def test_classify_formats_dedup_by_height():
    # B 站同高度常有 h264/h265/av1 多编码,只保留最高码率的一条
    formats = [
        {"format_id": "a", "vcodec": "avc1", "acodec": "none", "height": 1080, "tbr": 1000},
        {"format_id": "b", "vcodec": "hev1", "acodec": "none", "height": 1080, "tbr": 1500},
    ]
    out = [x for x in classify_formats(formats) if x["kind"] == "video"]
    assert len(out) == 1
    assert out[0]["format_id"] == "b"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_quality.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'quality'`)

- [ ] **Step 3: 写 `quality.py`**

```python
"""清晰度标注与 format 归一化。纯函数,无副作用,便于单测。"""
from typing import Optional


def height_to_label(height: Optional[int]) -> str:
    """像素高度转标准标签。非标准高度原样返回。"""
    if height is None:
        return "未知"
    return f"{height}P"


def label_to_label_cn(label: str) -> str:
    """英文标签转中文友好显示。"""
    mapping = {
        "2160P": "4K 超清",
        "1440P": "2K 超清",
        "1080P": "1080P 高清",
        "720P": "720P",
        "480P": "480P",
        "360P": "360P 流畅",
    }
    return mapping.get(label, label)


def classify_formats(formats: list[dict]) -> list[dict]:
    """把 yt-dlp 的 formats 归一化为可选项。

    - 纯音频(vcodec=none)→ kind=audio, label=音频
    - 纯视频(acodec=none)→ kind=video, 按 height 去重保留最高码率
    - 单体音视频 → kind=av, 按 height 去重保留最高码率
    """
    video_by_height: dict[int, dict] = {}
    av_by_height: dict[int, dict] = {}
    audio_best: Optional[dict] = None
    for f in formats:
        vcodec = (f.get("vcodec") or "none").lower()
        acodec = (f.get("acodec") or "none").lower()
        height = f.get("height")
        tbr = f.get("tbr") or 0
        fid = f.get("format_id")
        if vcodec == "none":  # 纯音频
            if audio_best is None or tbr > audio_best["tbr"]:
                audio_best = {"format_id": fid, "height": None, "label": "音频",
                              "kind": "audio", "tbr": tbr}
        elif acodec == "none":  # 纯视频
            h = height or 0
            cur = video_by_height.get(h)
            if cur is None or tbr > cur["tbr"]:
                video_by_height[h] = {"format_id": fid, "height": height,
                                      "label": height_to_label(height), "kind": "video", "tbr": tbr}
        else:  # 单体音视频
            h = height or 0
            cur = av_by_height.get(h)
            if cur is None or tbr > cur["tbr"]:
                av_by_height[h] = {"format_id": fid, "height": height,
                                   "label": height_to_label(height), "kind": "av", "tbr": tbr}

    result = []
    if audio_best:
        result.append(audio_best)
    # 视频按高度降序
    for h in sorted(video_by_height, reverse=True):
        result.append(video_by_height[h])
    for h in sorted(av_by_height, reverse=True):
        result.append(av_by_height[h])
    return result


def best_video_audio_choice(options: list[dict], preferred_label: str) -> str:
    """给定选项和用户选的清晰度标签,返回传给 yt-dlp 的 format 选择串。

    策略:若存在 DASH(纯视频轨),用 {视频format_id}+{最佳音频format_id};
    否则用单体 av 的 format_id。
    """
    videos = [o for o in options if o["kind"] == "video"]
    audios = [o for o in options if o["kind"] == "audio"]
    avs = [o for o in options if o["kind"] == "av"]

    target_video = next((v for v in videos if v["label"] == preferred_label), None)
    if target_video and audios:
        return f"{target_video['format_id']}+{audios[0]['format_id']}"

    target_av = next((a for a in avs if a["label"] == preferred_label), None)
    if target_av:
        return target_av["format_id"]

    # 兜底:最高视频 + 最高音频,或第一个 av
    if videos and audios:
        return f"{videos[0]['format_id']}+{audios[0]['format_id']}"
    if avs:
        return avs[0]["format_id"]
    return "best"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_quality.py -v`
Expected: 4 passed

- [ ] **Step 5: 补一个 `best_video_audio_choice` 的测试**

追加到 `tests/test_quality.py`:
```python
from quality import best_video_audio_choice

def test_choice_dash():
    opts = [
        {"format_id": "30280", "kind": "audio", "label": "音频", "tbr": 200},
        {"format_id": "v1080", "kind": "video", "label": "1080P", "tbr": 3000},
        {"format_id": "v720", "kind": "video", "label": "720P", "tbr": 1500},
    ]
    assert best_video_audio_choice(opts, "1080P") == "v1080+30280"

def test_choice_av_only():
    opts = [{"format_id": "mp4-1", "kind": "av", "label": "720P", "tbr": 800}]
    assert best_video_audio_choice(opts, "720P") == "mp4-1"
```
Run: `pytest tests/test_quality.py -v` → Expected: 6 passed

- [ ] **Step 6: 提交**

```bash
git add quality.py tests/test_quality.py
git commit -m "feat: 清晰度标注与 format 归一化纯函数"
```

---

## Task 3: 扫码登录模块(TDD, mock 网络层)

**Files:**
- Create: `bilibili_auth.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Produces:
  - `bilibili_auth.POLL_CODES = {"86101":"waiting","86090":"scanned","86038":"expired","0":"success"}`
  - `bilibili_auth.parse_poll_response(data:dict, set_cookie_header:str) -> dict` 返回 `{"status","sessdata","message"}`
  - `bilibili_auth.save_session(sessdata:str, dede_userid:str="", bili_jct:str="") -> None`
  - `bilibili_auth.load_session() -> dict|None`
  - `bilibili_auth.clear_session() -> None`
  - `bilibili_auth.get_ytdlp_cookie_arg() -> dict` 返回传给 yt-dlp 的选项 dict(如 `{"cookiefile":...}` 或 `{"cookiesfrombrowser":("edge",)}` 或 `{}`)

- [ ] **Step 1: 写失败测试 `tests/test_auth.py`**

```python
import json
from bilibili_auth import parse_poll_response, POLL_CODES, save_session, load_session, clear_session


def test_poll_codes_constants():
    assert POLL_CODES["86101"] == "waiting"
    assert POLL_CODES["86090"] == "scanned"
    assert POLL_CODES["86038"] == "expired"
    assert POLL_CODES["0"] == "success"


def test_parse_waiting():
    r = parse_poll_response({"data": {"code": 86101}}, "")
    assert r == {"status": "waiting", "sessdata": "", "message": "未扫码"}


def test_parse_scanned():
    r = parse_poll_response({"data": {"code": 86090}}, "")
    assert r["status"] == "scanned"


def test_parse_expired():
    r = parse_poll_response({"data": {"code": 86038}}, "")
    assert r["status"] == "expired"


def test_parse_success_extracts_sessdata():
    body = {"code": 0, "data": {"code": 0, "url": "https://x?SESSDATA=abc"}}
    set_cookie = "SESSDATA=hello123; Path=/; HttpOnly"
    r = parse_poll_response(body, set_cookie)
    assert r["status"] == "success"
    assert r["sessdata"] == "hello123"


def test_save_load_clear_session(tmp_path, monkeypatch):
    import bilibili_auth
    monkeypatch.setattr(bilibili_auth, "SESSION_COOKIE_FILE", tmp_path / "sess.json")
    save_session("SDATA", "123", "jct")
    loaded = load_session()
    assert loaded["SESSDATA"] == "SDATA"
    assert loaded["DedeUserID"] == "123"
    clear_session()
    assert load_session() is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL(ModuleNotFoundError)

- [ ] **Step 3: 写 `bilibili_auth.py`**

```python
"""B 站扫码登录 + cookie 策略。

登录方式三选一,优先级:扫码 cookie > Edge 浏览器 > cookies.txt。
"""
import json
import re
import http.cookiejar
from typing import Optional

import requests

from config import SESSION_COOKIE_FILE, COOKIE_FILE, BROWSER_COOKIE_SOURCE

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

POLL_CODES = {"86101": "waiting", "86090": "scanned", "86038": "expired", "0": "success"}
POLL_MESSAGES = {"86101": "未扫码", "86090": "已扫码,请在手机上确认", "86038": "二维码已失效", "0": "登录成功"}

# 网络函数可被测试替换
_http_get = requests.get


def qrcode_generate() -> dict:
    """生成二维码。返回 {'url','qrcode_key'}。"""
    resp = _http_get(QR_GENERATE, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"生成二维码失败: {data.get('message')}")
    d = data["data"]
    return {"url": d["url"], "qrcode_key": d["qrcode_key"]}


def qrcode_poll(qrcode_key: str) -> dict:
    """轮询扫码状态。返回 parse_poll_response 结果。"""
    resp = _http_get(QR_POLL, params={"qrcode_key": qrcode_key}, timeout=10)
    set_cookie = resp.headers.get("Set-Cookie", "")
    return parse_poll_response(resp.json(), set_cookie)


def parse_poll_response(data: dict, set_cookie_header: str) -> dict:
    """把 poll 响应解析成统一状态。SESSDATA 从 Set-Cookie 头提取。"""
    code = str(data.get("data", {}).get("code", -1))
    status = POLL_CODES.get(code, "unknown")
    sessdata = ""
    if status == "success":
        m = re.search(r"SESSDATA=([^;]+)", set_cookie_header or "")
        sessdata = m.group(1) if m else ""
    return {"status": status, "sessdata": sessdata, "message": POLL_MESSAGES.get(code, "未知状态")}


def save_session(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> None:
    SESSION_COOKIE_FILE.write_text(json.dumps({
        "SESSDATA": sessdata, "DedeUserID": dede_userid, "bili_jct": bili_jct
    }, ensure_ascii=False), encoding="utf-8")


def load_session() -> Optional[dict]:
    if not SESSION_COOKIE_FILE.exists():
        return None
    try:
        return json.loads(SESSION_COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_session() -> None:
    if SESSION_COOKIE_FILE.exists():
        SESSION_COOKIE_FILE.unlink()


def has_session() -> bool:
    s = load_session()
    return bool(s and s.get("SESSDATA"))


def write_cookies_txt(sessdata: str, dede_userid: str = "", bili_jct: str = "") -> None:
    """把扫码 cookie 写成 Netscape cookies.txt,供 yt-dlp 用。"""
    jar_lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/docs/http-cookies.html",
        ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t" + sessdata,
    ]
    if dede_userid:
        jar_lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tDedeUserID\t{dede_userid}")
    if bili_jct:
        jar_lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\t{bili_jct}")
    COOKIE_FILE.write_text("\n".join(jar_lines) + "\n", encoding="utf-8")


def get_ytdlp_cookie_arg() -> dict:
    """返回传给 yt-dlp YoutubeDL 的 cookie 选项。优先级:扫码>Edge>cookies.txt。"""
    sess = load_session()
    if sess and sess.get("SESSDATA"):
        write_cookies_txt(sess["SESSDATA"], sess.get("DedeUserID", ""), sess.get("bili_jct", ""))
        return {"cookiefile": str(COOKIE_FILE)}
    if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 50:
        return {"cookiefile": str(COOKIE_FILE)}
    return {"cookiesfrombrowser": (BROWSER_COOKIE_SOURCE,)}


def get_login_status() -> dict:
    """用当前 cookie 调 nav 接口,返回登录信息。"""
    sess = load_session()
    if not sess or not sess.get("SESSDATA"):
        return {"logged_in": False}
    try:
        resp = requests.get(NAV_API, cookies={"SESSDATA": sess["SESSDATA"]}, timeout=10)
        d = resp.json().get("data", {})
        return {
            "logged_in": d.get("isLogin", False),
            "uname": d.get("uname", ""),
            "vip": d.get("vipStatus", 0) == 1,
            "vip_type": d.get("vipType", 0),
        }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_auth.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add bilibili_auth.py tests/test_auth.py
git commit -m "feat: B站扫码登录与 cookie 策略"
```

---

## Task 4: 下载器 — 解析

**Files:**
- Create: `downloader.py`

**Interfaces:**
- Consumes: `quality.classify_formats`, `bilibili_auth.get_ytdlp_cookie_arg`
- Produces:
  - `downloader.parse_video(url) -> dict` 返回 `{"title","uploader","duration","thumbnail","formats":[归一化选项],"best_label"}`
  - 失败抛 `RuntimeError(原因)`

- [ ] **Step 1: 写 `downloader.py` 的解析部分**

```python
"""yt-dlp 封装:解析、下载、进度。"""
import threading
import uuid
from typing import Optional

import yt_dlp

from quality import classify_formats
from bilibili_auth import get_ytdlp_cookie_arg
from config import DOWNLOAD_DIR

# 任务状态表:task_id -> {status, progress, speed, eta, title, filepath, error}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


def _ydl_opts(extra: Optional[dict] = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    opts.update(get_ytdlp_cookie_arg())
    if extra:
        opts.update(extra)
    return opts


def parse_video(url: str) -> dict:
    """解析视频,返回标题等信息与归一化清晰度选项。"""
    opts = _ydl_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(f"解析失败:{e}")

    options = classify_formats(info.get("formats", []))
    # 给前端一个推荐画质:最高视频轨的 label
    video_opts = [o for o in options if o["kind"] == "video"]
    av_opts = [o for o in options if o["kind"] == "av"]
    best = (video_opts[0]["label"] if video_opts
            else (av_opts[0]["label"] if av_opts else ""))
    return {
        "title": info.get("title", ""),
        "uploader": info.get("uploader", ""),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail", ""),
        "formats": options,
        "best_label": best,
    }
```

- [ ] **Step 2: 手动验证解析(联网,需作为冒烟测试)**

Run:
```bash
source .venv/bin/activate
python3 -c "
from downloader import parse_video
r = parse_video('https://www.bilibili.com/video/BV1GJ411x7h7')
print('TITLE:', r['title'])
print('BEST:', r['best_label'])
print('FORMATS:', [(f['label'], f['kind']) for f in r['formats']])
"
```
Expected: 打印标题、推荐画质、清晰度列表(应含 360P/720P/1080P 等),无报错。

- [ ] **Step 3: 提交**

```bash
git add downloader.py
git commit -m "feat: 视频解析(清晰度归一化)"
```

---

## Task 5: 下载器 — 下载与进度

**Files:**
- Modify: `downloader.py`

**Interfaces:**
- Produces:
  - `downloader.start_download(url, label) -> str` 返回 task_id;异步执行
  - `downloader.get_task(task_id) -> dict|None`
  - `downloader.list_tasks() -> list[dict]`

- [ ] **Step 1: 在 `downloader.py` 追加下载逻辑**

在文件末尾追加:
```python
from quality import best_video_audio_choice


def _sanitize(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip().rstrip(".") or "video"


def _progress_hook(task_id: str):
    def hook(d):
        with _tasks_lock:
            t = _tasks.get(task_id)
            if not t:
                return
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                t["progress"] = round(downloaded / total * 100, 1) if total else 0
                t["speed"] = round(d.get("speed", 0) / 1024, 1)  # KB/s
                t["eta"] = d.get("eta", 0)
            elif d["status"] == "finished":
                t["progress"] = 100
                t["status"] = "merging"  # yt-dlp 合并阶段
            elif d["status"] == "error":
                t["status"] = "failed"
                t["error"] = "下载出错"
    return hook


def _run_download(task_id: str, url: str, label: str, formats: list[dict]) -> None:
    fmt_str = best_video_audio_choice(formats, label)
    outtmpl = str(DOWNLOAD_DIR / "%(title).200B.%(ext)s")
    opts = _ydl_opts({
        "format": fmt_str,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook(task_id)],
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t and t["status"] != "failed":
                t["status"] = "done"
    except Exception as e:
        with _tasks_lock:
            t = _tasks.get(task_id)
            if t:
                t["status"] = "failed"
                t["error"] = str(e)


def start_download(url: str, label: str) -> str:
    """启动异步下载。先解析拿到 formats,再后台下载。返回 task_id。"""
    info = parse_video(url)
    task_id = uuid.uuid4().hex[:12]
    with _tasks_lock:
        _tasks[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0, "speed": 0, "eta": 0,
            "title": info["title"], "url": url, "label": label, "error": "",
        }
    threading.Thread(target=_run_download, args=(task_id, url, label, info["formats"]),
                     daemon=True).start()
    return task_id


def get_task(task_id: str) -> Optional[dict]:
    with _tasks_lock:
        return _tasks.get(task_id)


def list_tasks() -> list[dict]:
    with _tasks_lock:
        return list(_tasks.values())
```

- [ ] **Step 2: 手动验证下载(联网,小视频)**

Run:
```bash
source .venv/bin/activate
python3 -c "
import time
from downloader import start_download, get_task
tid = start_download('https://www.bilibili.com/video/BV1GJ411x7h7', '360P')
for _ in range(60):
    t = get_task(tid)
    print(t['status'], t['progress'], '%', t.get('error',''))
    if t['status'] in ('done','failed'): break
    time.sleep(2)
"
ls -la ~/Downloads/bilibili/
```
Expected: 看到 status 从 queued→downloading→merging→done,目录下出现 `.mp4` 文件。

- [ ] **Step 3: 提交**

```bash
git add downloader.py
git commit -m "feat: 异步下载与进度上报"
```

---

## Task 6: Flask 路由层

**Files:**
- Create: `app.py`

**Interfaces:**
- Consumes: `bilibili_auth.*`, `downloader.*`, `config.PORT`
- Produces: 可 `python3 app.py` 启动的 HTTP 服务。

- [ ] **Step 1: 写 `app.py`**

```python
"""Flask 路由层。薄,业务委托给 bilibili_auth / downloader。"""
import time
import json
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

import bilibili_auth as auth
import downloader
from config import PORT, COOKIE_FILE, DOWNLOAD_DIR, BROWSER_COOKIE_SOURCE

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


# ---------- 登录 ----------
@app.route("/api/login/status")
def login_status():
    return jsonify(auth.get_login_status())


@app.route("/api/login/qrcode")
def login_qrcode():
    try:
        return jsonify({"ok": True, **auth.qrcode_generate()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/login/qrcode/poll")
def login_qrcode_poll():
    key = request.args.get("key", "")
    r = auth.qrcode_poll(key)
    if r["status"] == "success" and r["sessdata"]:
        auth.save_session(r["sessdata"])
    return jsonify(r)


@app.route("/api/login/use-browser", methods=["POST"])
def login_use_browser():
    auth.clear_session()
    return jsonify({"ok": True, "mode": BROWSER_COOKIE_SOURCE})


@app.route("/api/login/cookie-txt", methods=["POST"])
def login_cookie_txt():
    text = request.get_data(as_text=True)
    if "SESSDATA" not in text:
        return jsonify({"ok": False, "error": "未找到 SESSDATA"}), 400
    COOKIE_FILE.write_text(text, encoding="utf-8")
    auth.clear_session()  # 切到 cookies.txt 模式
    return jsonify({"ok": True})


@app.route("/api/login/logout", methods=["POST"])
def login_logout():
    auth.clear_session()
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()
    return jsonify({"ok": True})


# ---------- 解析/下载 ----------
@app.route("/api/parse", methods=["POST"])
def parse():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    try:
        return jsonify({"ok": True, "info": downloader.parse_video(url)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def download():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    label = body.get("label", "")
    if not url:
        return jsonify({"ok": False, "error": "请输入 URL"}), 400
    try:
        tid = downloader.start_download(url, label)
        return jsonify({"ok": True, "task_id": tid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/progress/<task_id>")
def progress(task_id):
    """SSE 推送单个任务进度,完成/失败后关闭。"""
    def gen():
        while True:
            t = downloader.get_task(task_id)
            if not t:
                yield "event: end\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(t, ensure_ascii=False)}\n\n"
            if t["status"] in ("done", "failed"):
                break
            time.sleep(1)
    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@app.route("/api/tasks")
def tasks():
    return jsonify({"ok": True, "tasks": downloader.list_tasks()})


@app.route("/api/config")
def get_config():
    return jsonify({"ok": True, "download_dir": str(DOWNLOAD_DIR)})


if __name__ == "__main__":
    print(f"\n  浏览器打开: http://localhost:{PORT}\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
```

- [ ] **Step 2: 起服务做冒烟测试(后端就绪,前端没写前用 curl)**

```bash
source .venv/bin/activate
python3 app.py &
sleep 2
curl -s http://localhost:5000/api/login/status
curl -s "http://localhost:5000/api/login/qrcode"
kill %1
```
Expected: `/api/login/status` 返回 `{"logged_in":false}`;`/api/login/qrcode` 返回含 `url` 和 `qrcode_key` 的 JSON。

- [ ] **Step 3: 提交**

```bash
git add app.py
git commit -m "feat: Flask 路由层(登录/解析/下载/SSE)"
```

---

## Task 7: 前端单页

**Files:**
- Create: `templates/index.html`

- [ ] **Step 1: 写 `templates/index.html`**

完整单页:登录区(三 tab:扫码 / Edge / cookies.txt)、解析+下载区、任务队列区。用原生 JS + EventSource。代码较长,直接写完整文件(无框架,内联 CSS/JS)。

关键交互:
- 扫码 tab:点"显示二维码"→ GET `/api/login/qrcode` → 用 `https://api.qrserver.com/v1/create-qr-code/?data=<urlencode(url)>` 显示二维码图 → 每 2s 轮询 `/api/login/qrcode/poll?key=` → 成功后刷新登录状态。
- Edge tab:按钮"使用 Edge 登录态"→ POST `/api/login/use-browser`。
- cookies.txt tab:文本框粘贴 → POST `/api/login/cookie-txt`。
- 解析:输入 URL → POST `/api/parse` → 展示标题/封面/清晰度下拉(默认选 best_label)。
- 下载:点"开始下载"→ POST `/api/download` → 用 EventSource 订阅 `/api/progress/<tid>` → 更新进度条 → 完成显示"打开文件夹"(用 `open` 命令经前端无法直接调,改为提示路径)。

完整 HTML 内容见 Step 1 实现时写入(约 250 行,含 CSS)。此处给出骨架与所有 JS 函数签名,实现时填全:
```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>bilibili download</title>
<style>/* 内联:简洁卡片式,灰底 */</style>
</head>
<body>
  <header><h1>bilibili download</h1><div id="login-badge"></div></header>
  <section id="login-area"><!-- 三 tab --></section>
  <section id="parse-area"><!-- URL 输入 + 解析结果 + 下载按钮 --></section>
  <section id="queue-area"><!-- 任务列表 --></section>
<script>
// refreshLogin() / showQrcode() / pollLogin(key) / useBrowser() / uploadCookieTxt()
// parseVideo() / startDownload() / subscribeProgress(tid) / renderTask(t) / refreshQueue()
</script>
</body>
</html>
```

- [ ] **Step 2: 完整写入前端(在实现时把骨架全部填实,不留 TODO)**

实现要点:
- 二维码用第三方生成服务 `https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=`,把 B 站返回的 `url` urlencode 后拼接。
- 登录状态徽章:`未登录` / `✓ 已登录 用户名 [大会员]`(从 `/api/login/status` 取)。
- 进度条 `<progress>` 元素 + 文字 `已下载 xx% · yy KB/s · 剩余 zz 秒`。
- 任务完成行:`✓ 完成 · 已保存到 ~/Downloads/bilibili · [刷新队列]`。

- [ ] **Step 3: 浏览器端到端手测**

启动服务,浏览器打开 `http://localhost:5000`:
1. 点扫码 → 显示二维码 → 手机 B 站 App 扫码确认 → 徽章变绿显示昵称+大会员。
2. 粘贴一个大会员视频 URL → 解析显示清晰度(应能看到 1080P+/4K 等会员画质)→ 选 4K → 下载 → 进度条跑动 → 完成。
3. 测试 Edge 模式与未登录时下载(应能看到清晰度受限)。

- [ ] **Step 4: 提交**

```bash
git add templates/index.html
git commit -m "feat: 前端单页(登录/解析/下载/队列)"
```

---

## Task 8: 启动脚本与文档收尾

**Files:**
- Create: `启动.command`
- Modify: `README.md`

- [ ] **Step 1: 写 `启动.command`**

```bash
#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
python3 app.py
read -n 1
```
Run: `chmod +x 启动.command`

- [ ] **Step 2: 完善 `README.md`**

写清:环境要求、双击启动步骤、三种登录方式说明、常见问题(cookie 失效就重新扫码、Edge 模式需在 Edge 登过、4K 需大会员)、下载目录位置。

- [ ] **Step 3: 最终端到端验证**

双击 `启动.command`,浏览器自动(手动)打开,完成一轮 扫码→解析→下载 4K 完整流程,确认 `~/Downloads/bilibili` 出现合并好的 mp4 且可正常播放。

- [ ] **Step 4: 提交收尾**

```bash
git add 启动.command README.md
git commit -m "docs: 启动脚本与使用说明"
```

---

## Self-Review 结果

**Spec 覆盖核对:**
- §1.2 三种登录方式 → Task 3 + Task 6(路由)+ Task 7(UI tab)✅
- §2.1 后端四模块(app/auth/downloader/quality)✅
- §3.1/3.2 数据流 → Task 3(qr)、Task 4-5(parse/download)、Task 6(SSE)✅
- §4 API 设计 → Task 6 全部路由 ✅
- §5 错误处理 → parse/download 抛 RuntimeError 转 JSON、cookie 失效提示、清晰度不可用透传 yt-dlp 错误 ✅
- §6 文件结构 → 一一对应 ✅
- §7 YAGNI → 未做 Electron/账号密码/弹幕 ✅
- §8 测试 → quality/auth 单测 + parse/download 手测 ✅

**类型一致性:** `classify_formats` 输出字段 `format_id/height/label/kind/tbr` 在 Task 4/5 一致;`start_download(url,label)` 在 Task 5/6 一致;`get_ytdlp_cookie_arg()` 在 Task 3/4 一致。

**占位符扫描:** Task 7 Step 2 标注"填实"但给了完整函数签名与交互要点——实现时按此填全,不留 TODO。
