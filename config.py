"""路径与配置。所有模块统一从这里取路径,避免硬编码。

DOWNLOAD_DIR / MAX_CONCURRENT / RATELIMIT_MBPS 都是运行时可变的
(用户在界面可改)。不要用 `from config import XXX` 拿快照,
改用 `import config` + `config.XXX` 访问,这样能读到最新值。
"""
import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

COOKIE_FILE = DATA_DIR / "cookies.txt"
SESSION_COOKIE_FILE = DATA_DIR / "sess.json"
TASKS_FILE = DATA_DIR / "tasks.json"
UP_LIST_FILE = DATA_DIR / "up_list.json"
CONFIG_FILE = DATA_DIR / "config.json"

_DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "bilibili"
_DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_MAX_CONCURRENT = 2     # B2: 默认并发数
_DEFAULT_RATELIMIT_MBPS = 0    # B2: 默认不限速(0)
_DEFAULT_THEME = "auto"         # C1: 主题 light/dark/auto
# 2026-08-15 批次:附件保存 / 归档 / 通知 / 订阅追更
_DEFAULT_SAVE_DANMAKU = False   # 下载后另存弹幕 .xml
_DEFAULT_SAVE_SUBTITLE = False  # 下载 CC 字幕(srt)
_DEFAULT_SAVE_COVER = False     # 下载封面图
_DEFAULT_SORT_BY_UP = False     # 按 UP 主建子目录归档
_DEFAULT_NOTIFY = True          # 下载完成发 macOS 通知
_DEFAULT_AUTO_REFRESH_MINUTES = 30  # 订阅追更轮询间隔
MIN_CONCURRENT = 1
MAX_CONCURRENT_LIMIT = 4  # 上限常量(不要复用这个名字,会和运行时变量冲突)
AUTO_REFRESH_MIN = 10     # 下限:再低就是持续轰炸接口,风控必触发
AUTO_REFRESH_MAX = 360


def _read() -> dict:
    """读完整 config.json(没有文件返回空字典)。"""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(d: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


# 启动加载(读一次,写入默认值兜底)
_cfg = _read()

# DOWNLOAD_DIR
s = _cfg.get("download_dir")
if isinstance(s, str) and s.strip():
    try:
        p = Path(s.strip()).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_DIR = p
    except Exception:
        DOWNLOAD_DIR = _DEFAULT_DOWNLOAD_DIR
else:
    DOWNLOAD_DIR = _DEFAULT_DOWNLOAD_DIR

# MAX_CONCURRENT(整数 1-MAX_CONCURRENT_LIMIT)
try:
    mc = int(_cfg.get("max_concurrent", _DEFAULT_MAX_CONCURRENT))
    MAX_CONCURRENT = max(MIN_CONCURRENT, min(MAX_CONCURRENT_LIMIT, mc))
except Exception:
    MAX_CONCURRENT = _DEFAULT_MAX_CONCURRENT

# RATELIMIT_MBPS(0 表示不限速)
try:
    RATELIMIT_MBPS = max(0, int(_cfg.get("ratelimit_mbps", _DEFAULT_RATELIMIT_MBPS)))
except Exception:
    RATELIMIT_MBPS = _DEFAULT_RATELIMIT_MBPS

# THEME(light/dark/auto)
THEME = _cfg.get("theme", _DEFAULT_THEME)
if THEME not in ("light", "dark", "auto"):
    THEME = _DEFAULT_THEME

# 保存开关 / 归档 / 通知 / 追更间隔
SAVE_DANMAKU = bool(_cfg.get("save_danmaku", _DEFAULT_SAVE_DANMAKU))
SAVE_SUBTITLE = bool(_cfg.get("save_subtitle", _DEFAULT_SAVE_SUBTITLE))
SAVE_COVER = bool(_cfg.get("save_cover", _DEFAULT_SAVE_COVER))
SORT_BY_UP = bool(_cfg.get("sort_by_up", _DEFAULT_SORT_BY_UP))
NOTIFY = bool(_cfg.get("notify", _DEFAULT_NOTIFY))
try:
    AUTO_REFRESH_MINUTES = max(AUTO_REFRESH_MIN,
                               min(AUTO_REFRESH_MAX, int(_cfg.get("auto_refresh_minutes", _DEFAULT_AUTO_REFRESH_MINUTES))))
except Exception:
    AUTO_REFRESH_MINUTES = _DEFAULT_AUTO_REFRESH_MINUTES


_config_lock = threading.Lock()


def get_config() -> dict:
    """返回当前所有用户可配项。"""
    return {
        "download_dir": str(DOWNLOAD_DIR),
        "max_concurrent": MAX_CONCURRENT,
        "ratelimit_mbps": RATELIMIT_MBPS,
        "theme": THEME,
        "save_danmaku": SAVE_DANMAKU,
        "save_subtitle": SAVE_SUBTITLE,
        "save_cover": SAVE_COVER,
        "sort_by_up": SORT_BY_UP,
        "notify": NOTIFY,
        "auto_refresh_minutes": AUTO_REFRESH_MINUTES,
    }


def set_config(patch: dict) -> dict:
    """部分更新用户配置。patch 可含 get_config() 中除 download_dir 外的任意字段。
    全部成功才写入磁盘(任一字段失败回滚)。"""
    global DOWNLOAD_DIR, MAX_CONCURRENT, RATELIMIT_MBPS, THEME
    global SAVE_DANMAKU, SAVE_SUBTITLE, SAVE_COVER, SORT_BY_UP, NOTIFY, AUTO_REFRESH_MINUTES
    with _config_lock:
        cur = _read()
        new_dir = DOWNLOAD_DIR
        new_mc = MAX_CONCURRENT
        new_rl = RATELIMIT_MBPS
        new_theme = THEME
        new_save_dm = SAVE_DANMAKU
        new_save_sub = SAVE_SUBTITLE
        new_save_cover = SAVE_COVER
        new_sort_up = SORT_BY_UP
        new_notify = NOTIFY
        new_arm = AUTO_REFRESH_MINUTES

        if "download_dir" in patch:
            try:
                new_dir = Path(str(patch["download_dir"]).strip()).expanduser()
                new_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {"ok": False, "error": f"无法创建目录:{e}"}

        if "max_concurrent" in patch:
            try:
                mc = int(patch["max_concurrent"])
                new_mc = max(MIN_CONCURRENT, min(MAX_CONCURRENT_LIMIT, mc))
            except Exception as e:
                return {"ok": False, "error": f"并发数无效:{e}"}

        if "ratelimit_mbps" in patch:
            try:
                new_rl = max(0, int(patch["ratelimit_mbps"]))
            except Exception as e:
                return {"ok": False, "error": f"限速值无效:{e}"}

        if "theme" in patch:
            t = patch["theme"]
            if t not in ("light", "dark", "auto"):
                return {"ok": False, "error": "主题无效(light/dark/auto)"}
            new_theme = t

        if "auto_refresh_minutes" in patch:
            try:
                new_arm = max(AUTO_REFRESH_MIN,
                              min(AUTO_REFRESH_MAX, int(patch["auto_refresh_minutes"])))
            except Exception as e:
                return {"ok": False, "error": f"追更间隔无效:{e}"}

        new_save_dm = bool(patch["save_danmaku"]) if "save_danmaku" in patch else new_save_dm
        new_save_sub = bool(patch["save_subtitle"]) if "save_subtitle" in patch else new_save_sub
        new_save_cover = bool(patch["save_cover"]) if "save_cover" in patch else new_save_cover
        new_sort_up = bool(patch["sort_by_up"]) if "sort_by_up" in patch else new_sort_up
        new_notify = bool(patch["notify"]) if "notify" in patch else new_notify

        DOWNLOAD_DIR = new_dir
        MAX_CONCURRENT = new_mc
        RATELIMIT_MBPS = new_rl
        THEME = new_theme
        SAVE_DANMAKU = new_save_dm
        SAVE_SUBTITLE = new_save_sub
        SAVE_COVER = new_save_cover
        SORT_BY_UP = new_sort_up
        NOTIFY = new_notify
        AUTO_REFRESH_MINUTES = new_arm
        cur.update({
            "download_dir": str(DOWNLOAD_DIR),
            "max_concurrent": MAX_CONCURRENT,
            "ratelimit_mbps": RATELIMIT_MBPS,
            "theme": THEME,
            "save_danmaku": SAVE_DANMAKU,
            "save_subtitle": SAVE_SUBTITLE,
            "save_cover": SAVE_COVER,
            "sort_by_up": SORT_BY_UP,
            "notify": NOTIFY,
            "auto_refresh_minutes": AUTO_REFRESH_MINUTES,
        })
        try:
            _write(cur)
        except Exception as e:
            return {"ok": False, "error": f"保存失败:{e}"}
    return {"ok": True, **get_config()}


def get_download_dir() -> Path:
    return DOWNLOAD_DIR


def get_ratelimit_bytes() -> Optional[int]:
    """yt-dlp 的 ratelimit(字节/秒),None 表示不限速。"""
    if RATELIMIT_MBPS <= 0:
        return None
    return RATELIMIT_MBPS * 1024 * 1024


PORT = 5050
BROWSER_COOKIE_SOURCE = "edge"