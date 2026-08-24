"""统一 JSON 持久化层:原子写 + 损坏备份 + 失败可见(2026-08-19 评审 P1)。"""
import inspect
import json
import threading

import pytest

import storage


def test_write_then_read_roundtrip(tmp_path):
    f = tmp_path / "x.json"
    storage.write_json(f, {"a": 1, "中文": "值"})
    assert storage.read_json(f, default=None) == {"a": 1, "中文": "值"}


def test_write_is_atomic_and_locked(tmp_path):
    """写必须 tmp + os.replace 原子替换;同路径并发写后仍是合法 JSON。"""
    assert "os.replace" in inspect.getsource(storage.write_json), "必须原子 rename"
    f = tmp_path / "c.json"
    threads = [threading.Thread(target=lambda: storage.write_json(f, {"n": i, "pad": "x" * 200}))
               for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert isinstance(json.loads(f.read_text(encoding="utf-8")), dict)
    assert not list(tmp_path.glob("*.tmp")), "不应残留临时文件"


def test_write_failure_logged_not_raised(tmp_path, monkeypatch):
    """写盘失败记日志、不抛——所有调用方都按『尽力写』语义设计。"""
    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("pathlib.Path.write_text", boom)
    storage.write_json(tmp_path / "x.json", {"a": 1})  # 不抛即通过


def test_read_corrupt_backs_up_and_returns_default(tmp_path):
    """损坏文件:备份 .corrupt-<ts> 供手工找回,返回 default,原路径移走。"""
    f = tmp_path / "x.json"
    f.write_text('{"broken": [', encoding="utf-8")
    assert storage.read_json(f, default={"d": 1}) == {"d": 1}
    backups = list(tmp_path.glob("x.json.corrupt-*"))
    assert backups and not f.exists()


def test_read_missing_returns_default_no_backup(tmp_path):
    """文件不存在是正常态:直接 default,不产生任何备份。"""
    assert storage.read_json(tmp_path / "none.json", default=None) is None
    assert list(tmp_path.iterdir()) == []


def test_all_persistence_uses_storage():
    """五个模块的 JSON 读写统一走 storage(tasks/config/up_list/sess/pagecache)。"""
    import bilibili_auth
    import config
    import downloader
    import pagecache
    import up_list
    for mod, fn in (
        (downloader, "_persist"), (downloader, "_load_persisted"),
        (config, "_read"), (config, "_write"),
        (up_list, "_read"), (up_list, "_write"),
        (bilibili_auth, "save_session"), (bilibili_auth, "load_session"),
        (pagecache, "save"), (pagecache, "load"),
    ):
        src = inspect.getsource(getattr(mod, fn))
        assert "storage.write_json" in src or "storage.read_json" in src, \
            f"{mod.__name__}.{fn} 应走 storage 统一持久化"


def test_app_does_not_touch_cookie_file_directly():
    """app 路由层不得越权写/删 cookies.txt(该文件归 bilibili_auth 管)。"""
    import app as app_module
    src = inspect.getsource(app_module)
    assert "COOKIE_FILE.write_text" not in src
    assert "COOKIE_FILE.unlink" not in src


# ---------- 测试隔离:绝不写用户真实运行时数据 ----------

def test_runtime_data_is_isolated_from_tests():
    """conftest 必须把任务史与页面缓存的落盘位置都指向临时目录。

    `taskstore` 是模块级全局状态,导入时就从真实 data/tasks.json 恢复存量任务;
    任何触发 `_persist()` 的测试都会把「真实记录 + 测试残留」写回真实文件,
    删掉真实记录后一 persist 就永久丢下载史(data/ 在 .gitignore,无从找回)。
    这条测试保证隔离 fixture 在位。
    """
    import config
    import pagecache
    import taskstore
    tmp_markers = ("pytest", "tmp", "/T/")

    assert taskstore.TASKS_FILE != config.TASKS_FILE, \
        "任务史落盘未被隔离,测试会写用户真实的 data/tasks.json"
    assert any(m in str(taskstore.TASKS_FILE) for m in tmp_markers), \
        f"任务史应落在临时目录,实际是 {taskstore.TASKS_FILE}"
    assert any(m in str(pagecache.CACHE_DIR) for m in tmp_markers), \
        f"页面缓存应落在临时目录,实际是 {pagecache.CACHE_DIR}"


def test_task_table_starts_clean_each_test():
    """每个用例拿到的是干净任务表(不受存量记录/别的用例残留影响)。"""
    import taskstore
    assert taskstore._tasks == {}, \
        f"任务表应为空,实际有 {len(taskstore._tasks)} 条:{list(taskstore._tasks)}"


def test_persist_writes_only_to_isolated_file():
    """_persist 落到隔离文件上,真实 data/tasks.json 一个字节都不该变。"""
    import json
    import config
    import taskstore
    real = config.TASKS_FILE
    before = real.read_bytes() if real.exists() else None

    taskstore._tasks["iso1"] = {"task_id": "iso1", "status": "done",
                                "title": "隔离验证", "url": "", "cancel": False}
    taskstore._persist()

    written = json.loads(taskstore.TASKS_FILE.read_text(encoding="utf-8"))
    assert [t["task_id"] for t in written] == ["iso1"]
    after = real.read_bytes() if real.exists() else None
    assert after == before, "真实 tasks.json 被测试改写了"
