"""统一 JSON 持久化:原子写 + 损坏备份 + 失败可见。

data/*.json 是各模块唯一状态载体(tasks/config/up_list/sess/pagecache 缓存),
非原子写或并发写坏即丢数据。统一规则:
- 写:tmp + os.replace 原子替换,模块级写锁防同文件并发交错;失败记日志不抛
  (调用方都按「尽力写」语义设计,不该为写盘失败打断业务流程)。
- 读:文件不存在返回 default(正常态);损坏时备份 .corrupt-<ts> 供手工找回,
  同样返回 default——绝不静默当作空数据覆盖掉。
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("bd.storage")

_write_lock = threading.Lock()


def write_json(path: Path, data: Any, *, indent: Optional[int] = None) -> None:
    """原子写 JSON。tmp + os.replace,任何一步失败记日志、不抛。"""
    with _write_lock:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent),
                           encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.exception("写 %s 失败(数据未持久化)", path)


def read_json(path: Path, default: Any = None) -> Any:
    """读 JSON。不存在 → default;损坏 → 备份 .corrupt-<ts> 后返回 default。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(backup)
            log.error("%s 解析失败,原文件已备份到 %s(按 default 处理)", path, backup)
        except OSError:
            log.exception("%s 损坏且备份失败", path)
        return default
