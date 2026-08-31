"""路径与配置。所有模块统一从这里取路径,避免硬编码。

DOWNLOAD_DIR / MAX_CONCURRENT / RATELIMIT_MBPS 都是运行时可变的
(用户在界面可改)。不要用 `from config import XXX` 拿快照,
改用 `import config` + `config.XXX` 访问,这样能读到最新值。

可配项全部声明在 _FIELDS 字段表里:读(启动加载/兜底)、写(校验/夹紧/
回滚)、序列化都由表驱动——加新字段只需在表里加一行,不用再改五处。
"""
import os
import threading
from pathlib import Path

import storage

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
_DEFAULT_RATELIMIT_MBPS = 0     # B2: 默认不限速(0)
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

# 可配项字段表。key = config.json 键名;模块全局名恒为 key.upper()。
# kind:
#   dir    Path + 展开家目录 + 自动建目录;非法时回退默认(启动)或报错(set)
#   int    整数,夹紧到 [lo, hi];非法时回退默认(启动)或报错(set)
#   choice 枚举取值
#   bool   勾选框语义,按真假规整
# err: set_config 校验失败时的错误文案(int/dir 追加具体异常)。
# 序:get_config()/写盘的字段顺序即此表顺序。
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


def _parse_value(spec: dict, raw):
    """按字段类型解析/校验 raw。非法抛 ValueError(文案由调用方组)。"""
    kind = spec["kind"]
    if kind == "dir":
        p = Path(str(raw).strip()).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "int":
        # 越界一律夹紧到边界(旧行为:只有非数字才报错)
        return max(spec["lo"], min(spec["hi"], int(raw)))
    if kind == "choice":
        if raw not in spec["choices"]:
            raise ValueError(f"非法取值 {raw!r}")
        return raw
    if kind == "bool":
        return bool(raw)
    raise ValueError(f"未知字段类型 {kind}")


def _clamp_value(spec: dict, raw):
    """启动加载用的宽松版:非法/缺失一律回默认值,绝不抛。"""
    kind = spec["kind"]
    try:
        if kind == "dir":
            s = raw
            if isinstance(s, str) and s.strip():
                return _parse_value(spec, s)
            return spec["default"]
        if kind == "int":
            v = int(raw)
            return max(spec["lo"], min(spec["hi"], v))
        if kind == "choice":
            return raw if raw in spec["choices"] else spec["default"]
        if kind == "bool":
            return bool(raw)
    except Exception:
        return spec["default"]
    return spec["default"]


def _read() -> dict:
    """读完整 config.json(没有文件返回空字典)。"""
    return storage.read_json(CONFIG_FILE, default={})


def _write(d: dict) -> bool:
    return storage.write_json(CONFIG_FILE, d, indent=1)


def _serialize(key: str, v):
    """写盘/接口输出的 JSON 形态:目录转字符串,其余原样。"""
    return str(v) if _FIELDS[key]["kind"] == "dir" else v


# 启动加载:按字段表读一次,写入模块全局(名字恒为 key.upper())
_cfg = _read()
for _key, _spec in _FIELDS.items():
    globals()[_key.upper()] = _clamp_value(_spec, _cfg.get(_key))

_config_lock = threading.Lock()


def get_config() -> dict:
    """返回当前所有用户可配项。"""
    return {k: _serialize(k, globals()[k.upper()]) for k in _FIELDS}


def set_config(patch: dict) -> dict:
    """部分更新用户配置。patch 可含 get_config() 中任意字段。

    全部成功才生效并写盘(任一字段失败回滚,不动任何全局)。"""
    with _config_lock:
        cur = _read()
        new_vals = {k: globals()[k.upper()] for k in _FIELDS}
        for key, raw in patch.items():
            spec = _FIELDS.get(key)
            if not spec:
                continue  # 未识别字段忽略(与旧行为一致)
            try:
                v = _parse_value(spec, raw)
            except Exception as e:
                err = spec.get("err", "值无效")
                # choice 的文案是完整的;int/dir 追加具体异常
                if spec["kind"] == "choice":
                    return {"ok": False, "error": err}
                return {"ok": False, "error": f"{err}:{e}"}
            new_vals[key] = v
        cur.update({k: _serialize(k, v) for k, v in new_vals.items()})
        # 先提交完整候选配置；写盘失败时运行时全局值保持原状。
        if not _write(cur):
            return {"ok": False, "error": "保存失败:配置未写入磁盘",
                    "persist_error": True}
        for key, v in new_vals.items():
            globals()[key.upper()] = v
    return {"ok": True, **get_config()}


def get_download_dir() -> Path:
    return DOWNLOAD_DIR


def get_ratelimit_bytes():
    """yt-dlp 的 ratelimit(字节/秒),None 表示不限速。"""
    if RATELIMIT_MBPS <= 0:
        return None
    return RATELIMIT_MBPS * 1024 * 1024


PORT = int(os.environ.get("BD_PORT", 5050))
BROWSER_COOKIE_SOURCE = "edge"
