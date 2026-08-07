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
MIN_CONCURRENT = 1
MAX_CONCURRENT_LIMIT = 4  # 上限常量(不要复用这个名字,会和运行时变量冲突)


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


_config_lock = threading.Lock()


def get_config() -> dict:
    """返回当前所有用户可配项。"""
    return {
        "download_dir": str(DOWNLOAD_DIR),
        "max_concurrent": MAX_CONCURRENT,
        "ratelimit_mbps": RATELIMIT_MBPS,
    }


def set_config(patch: dict) -> dict:
    """部分更新用户配置。patch 可含 download_dir / max_concurrent / ratelimit_mbps。
    全部成功才写入磁盘(任一字段失败回滚)。"""
    global DOWNLOAD_DIR, MAX_CONCURRENT, RATELIMIT_MBPS
    with _config_lock:
        cur = _read()
        new_dir = DOWNLOAD_DIR
        new_mc = MAX_CONCURRENT
        new_rl = RATELIMIT_MBPS

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

        DOWNLOAD_DIR = new_dir
        MAX_CONCURRENT = new_mc
        RATELIMIT_MBPS = new_rl
        cur.update({
            "download_dir": str(DOWNLOAD_DIR),
            "max_concurrent": MAX_CONCURRENT,
            "ratelimit_mbps": RATELIMIT_MBPS,
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