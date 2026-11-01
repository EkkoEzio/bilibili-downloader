"""路径与配置。所有模块统一从这里取路径,避免硬编码。

DOWNLOAD_DIR / MAX_CONCURRENT / RATELIMIT_MBPS 都是运行时可变的
(用户在界面可改)。不要用 `from config import XXX` 拿快照,
改用 `import config` + `config.XXX` 访问,这样能读到最新值。

可配项全部声明在 _FIELDS 字段表里:读(启动加载/兜底)、写(校验/夹紧/
回滚)、序列化都由表驱动——加新字段只需在表里加一行,不用再改五处。
"""
import logging
import os
import threading
from pathlib import Path

import storage

log = logging.getLogger("bd.config")

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

_DEFAULT_MAX_CONCURRENT = 2
_DEFAULT_RATELIMIT_MBPS = 0
_DEFAULT_THEME = "auto"
_DEFAULT_SAVE_DANMAKU = False
_DEFAULT_SAVE_SUBTITLE = False
_DEFAULT_SAVE_COVER = False
_DEFAULT_SORT_BY_UP = False
_DEFAULT_NOTIFY = True
_DEFAULT_AUTO_REFRESH_MINUTES = 30
MIN_CONCURRENT = 1
MAX_CONCURRENT_LIMIT = 4
AUTO_REFRESH_MIN = 10
AUTO_REFRESH_MAX = 360

_FIELDS = {
    "download_dir":        {"kind": "dir", "default": _DEFAULT_DOWNLOAD_DIR,
                            "err": "无法创建目录"},
    "max_concurrent":      {"kind": "int", "lo": MIN_CONCURRENT, "hi": MAX_CONCURRENT_LIMIT,
                            "default": _DEFAULT_MAX_CONCURRENT, "err": "并发数无效"},
    "ratelimit_mbps":      {"kind": "int", "lo": 0, "hi": 10 ** 9,
                            "default": _DEFAULT_RATELIMIT_MBPS, "err": "限速值无效"},
    "theme":               {"kind": "choice", "choices": ("light", "dark", "auto"),
                            "default": _DEFAULT_THEME,
                            "err": "主题无效(light/dark/auto)"},
    "save_danmaku":        {"kind": "bool", "default": _DEFAULT_SAVE_DANMAKU},
    "save_subtitle":       {"kind": "bool", "default": _DEFAULT_SAVE_SUBTITLE},
    "save_cover":          {"kind": "bool", "default": _DEFAULT_SAVE_COVER},
    "sort_by_up":          {"kind": "bool", "default": _DEFAULT_SORT_BY_UP},
    "notify":              {"kind": "bool", "default": _DEFAULT_NOTIFY},
    "auto_refresh_minutes": {"kind": "int", "lo": AUTO_REFRESH_MIN, "hi": AUTO_REFRESH_MAX,
                             "default": _DEFAULT_AUTO_REFRESH_MINUTES,
                             "err": "追更间隔无效"},
}


def _parse_bool(raw) -> bool:
    """兼容旧真假规整，同时正确识别常见文本布尔值。"""
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("", "false", "0", "no", "off"):
            return False
        if value in ("true", "1", "yes", "on"):
            return True
        # 未知非空文本沿用旧 bool(raw) 语义，避免破坏存量配置。
        return True
    return bool(raw)


def _parse_value(spec: dict, raw):
    """按字段类型解析/校验 raw。非法抛 ValueError(文案由调用方组)。"""
    kind = spec["kind"]
    if kind == "dir":
        text = str(raw).strip()
        if not text:
            # Path("") 会静默解析成当前目录,下载会全部落进应用源码树。
            raise ValueError("目录不能为空")
        p = Path(text).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "int":
        return max(spec["lo"], min(spec["hi"], int(raw)))
    if kind == "choice":
        if raw not in spec["choices"]:
            raise ValueError(f"非法取值 {raw!r}")
        return raw
    if kind == "bool":
        return _parse_bool(raw)
    raise ValueError(f"未知字段类型 {kind}")


def _clamp_value(spec: dict, raw):
    """启动加载用的宽松版:非法/缺失一律回默认值,绝不抛。"""
    kind = spec["kind"]
    try:
        if kind == "dir":
            if isinstance(raw, str) and raw.strip():
                return _parse_value(spec, raw)
            return spec["default"]
        if kind == "int":
            value = int(raw)
            return max(spec["lo"], min(spec["hi"], value))
        if kind == "choice":
            return raw if raw in spec["choices"] else spec["default"]
        if kind == "bool":
            return _parse_bool(raw)
    except Exception:
        return spec["default"]
    return spec["default"]


def _read() -> dict:
    """读取配置对象；合法 JSON 但根不是对象时安全回退。"""
    value = storage.read_json(CONFIG_FILE, default={})
    if isinstance(value, dict):
        return value
    log.warning("配置文件根节点必须是对象，已忽略类型 %s", type(value).__name__)
    return {}


def _write(d: dict) -> bool:
    return storage.write_json(CONFIG_FILE, d, indent=1)


def _serialize(key: str, value):
    """写盘/接口输出的 JSON 形态:目录转字符串,其余原样。"""
    return str(value) if _FIELDS[key]["kind"] == "dir" else value


_cfg = _read()
for _key, _spec in _FIELDS.items():
    globals()[_key.upper()] = _clamp_value(_spec, _cfg.get(_key))

_config_lock = threading.Lock()


def get_config() -> dict:
    """返回当前所有用户可配项。"""
    return {k: _serialize(k, globals()[k.upper()]) for k in _FIELDS}


def set_config(patch: dict) -> dict:
    """部分更新配置；全部成功才落盘并切换运行时值。"""
    if not isinstance(patch, dict):
        return {"ok": False, "error": "配置必须是 JSON 对象"}
    with _config_lock:
        cur = _read()
        new_vals = {k: globals()[k.upper()] for k in _FIELDS}
        for key, raw in patch.items():
            spec = _FIELDS.get(key)
            if not spec:
                continue
            try:
                value = _parse_value(spec, raw)
            except Exception as exc:
                err = spec.get("err", "值无效")
                if spec["kind"] == "choice":
                    return {"ok": False, "error": err}
                return {"ok": False, "error": f"{err}:{exc}"}
            new_vals[key] = value
        cur.update({k: _serialize(k, value) for k, value in new_vals.items()})
        if not _write(cur):
            return {"ok": False, "error": "保存失败:配置未写入磁盘",
                    "persist_error": True}
        for key, value in new_vals.items():
            globals()[key.upper()] = value
    return {"ok": True, **get_config()}


def get_download_dir() -> Path:
    return DOWNLOAD_DIR


def get_ratelimit_bytes():
    """yt-dlp 的 ratelimit(字节/秒),None 表示不限速。"""
    if RATELIMIT_MBPS <= 0:
        return None
    return RATELIMIT_MBPS * 1024 * 1024


_DEFAULT_PORT = 5050


def _parse_port(raw, default: int = _DEFAULT_PORT) -> int:
    """解析监听端口；非法或越界值回退默认端口，避免导入阶段崩溃。"""
    try:
        port = int(raw)
        if not 1 <= port <= 65535:
            raise ValueError("端口范围应为 1..65535")
        return port
    except (TypeError, ValueError) as exc:
        log.warning("BD_PORT=%r 无效(%s)，使用默认端口 %d", raw, exc, default)
        return default


PORT = _parse_port(os.environ.get("BD_PORT", _DEFAULT_PORT))
BROWSER_COOKIE_SOURCE = "edge"
