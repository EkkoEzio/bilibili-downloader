"""统一 JSON 持久化:原子写 + 损坏备份 + 失败可见。

data/*.json 是各模块唯一状态载体(tasks/config/up_list/sess/pagecache 缓存),
非原子写或并发写坏即丢数据。统一规则:
- 写:tmp + os.replace 原子替换,模块级写锁防同文件并发交错;失败记日志不抛并
  返回 False。关键事实由调用方拒绝提交，页面缓存等派生数据可继续按尽力写处理。
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


class JsonReadLimitError(RuntimeError):
    """JSON 文件超过调用方声明的安全读取上限；原文件保持不动。"""

    def __init__(self, path: Path, limit: int) -> None:
        super().__init__(f"{path.name} 超过安全读取上限 {limit} 字节")
        self.path = path
        self.limit = int(limit)


def write_json(path: Path, data: Any, *, indent: Optional[int] = None) -> bool:
    """原子写 JSON。成功返回 True；失败记日志、清临时文件并返回 False。"""
    with _write_lock:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent),
                           encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                log.exception("清理临时文件 %s 失败", tmp)
            log.exception("写 %s 失败(数据未持久化)", path)
            return False


def read_json(path: Path, default: Any = None, *, max_bytes: Optional[int] = None) -> Any:
    """读 JSON。

    不存在返回 ``default``；语法/编码损坏会备份为 ``.corrupt-*``。传入
    ``max_bytes`` 时只读取上限再多 1 字节，超限抛 ``JsonReadLimitError``，
    不把合法但过大的状态文件改名、截断或覆盖。
    """
    if not path.exists():
        return default
    try:
        if max_bytes is None:
            text = path.read_text(encoding="utf-8")
        else:
            limit = max(0, int(max_bytes))
            with path.open("rb") as handle:
                payload = handle.read(limit + 1)
            if len(payload) > limit:
                raise JsonReadLimitError(path, limit)
            text = payload.decode("utf-8")
        return json.loads(text)
    except JsonReadLimitError:
        raise
    except Exception:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(backup)
            log.error("%s 解析失败,原文件已备份到 %s(按 default 处理)", path, backup)
        except OSError:
            log.exception("%s 损坏且备份失败", path)
        return default
