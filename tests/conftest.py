"""全局隔离:所有测试的页面缓存写入一律落临时目录。

曾因路由测试未隔离 CACHE_DIR,把单条 mock 合集写进真实 data/cache,
用户端每次跑完测试再打开「追更合集」必弹一次「查看最新」提示。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_pagecache(tmp_path, monkeypatch):
    import pagecache
    monkeypatch.setattr(pagecache, "CACHE_DIR", tmp_path)
