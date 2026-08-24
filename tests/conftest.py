"""全局隔离:测试绝不碰用户真实的运行时数据(data/)。

这里挡两类污染,都是真实踩过的:

1. **页面缓存**:曾因路由测试未隔离 CACHE_DIR,把单条 mock 合集写进真实
   `data/cache/`,用户每次跑完测试再打开「追更合集」必弹一次「查看最新」。

2. **任务史**:`taskstore` 是模块级全局状态,导入时就从真实 `data/tasks.json`
   恢复存量任务;任何触发 `_persist()` 的测试都会把「真实记录 + 测试残留」
   一起写回真实文件。轻则把 `merging` 改写成 `interrupted`(_load_persisted
   的正常语义,但发生在用户正下载时就是脏写),重则测试删掉了某条真实记录后
   一 persist 就永久丢失下载史——而 data/ 在 .gitignore 里,没有任何找回途径。

   所以:把 TASKS_FILE 指到临时目录,并给每个测试一张干净的任务表,
   结束后原样还回去。
"""
import pytest


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
