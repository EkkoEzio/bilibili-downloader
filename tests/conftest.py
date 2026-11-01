"""全局隔离:测试绝不碰用户真实的运行时数据(data/)。

这里挡三类污染,都是真实踩过的:

1. **页面缓存**:曾因路由测试未隔离 CACHE_DIR,把单条 mock 合集写进真实
   `data/cache/`,用户每次跑完测试再打开「追更合集」必弹一次「查看最新」。

2. **任务史**:`taskstore` 是模块级全局状态,导入时就从真实 `data/tasks.json`
   恢复存量任务;任何触发 `_persist()` 的测试都会把「真实记录 + 测试残留」
   一起写回真实文件。轻则把 `merging` 改写成 `interrupted`(_load_persisted
   的正常语义,但发生在用户正下载时就是脏写),重则测试删掉了某条真实记录后
   一 persist 就永久丢失下载史——而 data/ 在 .gitignore 里,没有任何找回途径。

   所以:把 TASKS_FILE 指到临时目录,并给每个测试一张干净的任务表,
   结束后原样还回去。

3. **实例锁与导入期迁移写**:pytest 先导入 conftest 再导入测试模块,而测试
   模块一 import 就会触发 taskstore 的 `acquire_instance_lock()` 和
   `_load_persisted()`(后者会把迁移快照写回 tasks.json)。此前这要求应用
   必须先停掉才能跑测试——应用开着时整个测试套件在收集阶段就报
   AlreadyRunningError。这里在导入任何业务模块之前,把实例锁文件与任务史
   都重定向到本进程专属的沙盒目录:测试与应用可以并行,导入期迁移写也
   只会发生在沙盒里。
"""
import atexit
import shutil
import tempfile
from pathlib import Path

import pytest

import config
import instance_lock

# 每个测试进程一个独立沙盒;进程结束时尽力清理。
_TEST_SANDBOX = Path(tempfile.mkdtemp(prefix="bd-tests-sandbox-"))
atexit.register(lambda: shutil.rmtree(_TEST_SANDBOX, ignore_errors=True))

# 必须发生在任何 import taskstore 的模块之前(conftest 先于测试模块导入):
# - config.TASKS_FILE 是 taskstore `from config import TASKS_FILE` 的取值来源;
# - instance_lock._lock_path 在 acquire_instance_lock 调用时才读取,模块级
#   替换即可生效。真实 data/app.lock 与 data/tasks.json 从此与测试无关。
config.TASKS_FILE = _TEST_SANDBOX / "tasks.json"
instance_lock._lock_path = lambda: _TEST_SANDBOX / "app.lock"


@pytest.fixture(autouse=True)
def _isolate_pagecache(tmp_path, monkeypatch):
    import pagecache
    monkeypatch.setattr(pagecache, "CACHE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def _isolate_taskstore(tmp_path, monkeypatch):
    """任务史落盘重定向到 tmp,任务表每个用例独立。"""
    import taskstore
    monkeypatch.setattr(taskstore, "TASKS_FILE", tmp_path / "tasks.json")
    saved = dict(taskstore._tasks)
    taskstore._tasks.clear()
    try:
        yield
    finally:
        taskstore._tasks.clear()
        taskstore._tasks.update(saved)


@pytest.fixture(autouse=True)
def _isolate_up_list(tmp_path, monkeypatch):
    """UP/合集订阅落盘重定向到 tmp:测试绝不读写真实 data/up_list.json。

    第 4 类污染,2026-10-31 补。`auto_refresh.refresh_once` 现在会读
    `up_list.list_collections()`:不隔离时测试会拿真实订阅里的合集去打 B 站
    请求(实测把一轮检查拖到秒级),并可能把 `seen_bvids`/`checked_at` 写回
    真实文件——那条订阅属于用户,写坏了没有找回途径。

    注意必须打在 `up_list` 模块上:`up_list` 是 `from config import
    UP_LIST_FILE`,改 config 的常量对它无效。
    """
    import up_list
    monkeypatch.setattr(up_list, "UP_LIST_FILE", tmp_path / "up_list.json")
