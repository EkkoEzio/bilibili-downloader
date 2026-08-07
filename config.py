"""路径与配置。所有模块统一从这里取路径,避免硬编码。

DOWNLOAD_DIR 是运行时可变的(用户在界面可改下载目录)。
不要用 `from config import DOWNLOAD_DIR` 拿快照,改用 `import config` +
`config.DOWNLOAD_DIR` 访问,这样能读到最新值。
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
CONFIG_FILE = DATA_DIR / "config.json"           # B3: 用户可配置的下载目录等

_DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "bilibili"
_DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 启动加载
def _load_initial() -> Path:
    if not CONFIG_FILE.exists():
        return _DEFAULT_DOWNLOAD_DIR
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        s = d.get("download_dir")
        if isinstance(s, str) and s.strip():
            p = Path(s.strip()).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    return _DEFAULT_DOWNLOAD_DIR


DOWNLOAD_DIR = _load_initial()  # 模块级全局,可通过赋值修改

_config_lock = threading.Lock()


def get_download_dir() -> Path:
    return DOWNLOAD_DIR


def set_download_dir(p) -> dict:
    """设置下载目录。展开 ~、创建目录,持久化到 config.json。
    返回 {'ok': bool, 'path': str?, 'error': str?}。"""
    global DOWNLOAD_DIR
    with _config_lock:
        try:
            new = Path(str(p).strip()).expanduser()
            new.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"无法创建目录:{e}"}
        DOWNLOAD_DIR = new
        try:
            CONFIG_FILE.write_text(json.dumps({"download_dir": str(new)}, ensure_ascii=False),
                                   encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": f"保存失败:{e}"}
    return {"ok": True, "path": str(new)}


PORT = 5050
BROWSER_COOKIE_SOURCE = "edge"