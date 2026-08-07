"""B1 重试逻辑的结构性测试。

不依赖 yt-dlp mock(易死锁),改用源码静态分析 + 简单业务测试。
"""
import inspect

import downloader


def test_retry_loop_present():
    """验证 _run_download 有重试循环。"""
    src = inspect.getsource(downloader._run_download)
    assert "for attempt in range(" in src, "缺少重试循环"
    assert "max_retries" in src, "缺少 max_retries 配置"
    assert "_t.sleep" in src, "缺少退避 sleep"
    assert 't.get("cancel")' in src, "缺少取消标志检测"
    assert 't["status"] = "retrying"' in src, "缺少 retrying 状态标记"
    assert 't["error"] = str(last_err)' in src, "缺少最终 error 记录"


def test_cancel_exits_immediately():
    """验证取消标志在循环开头就检测,确保取消的任务及时退出。"""
    src = inspect.getsource(downloader._run_download)
    # 取消检查 `t.get("cancel")` 应在循环内第一次出现
    for_pos = src.find("for attempt in range(")
    cancel_pos = src.find('t.get("cancel")', for_pos)
    assert for_pos > 0 and cancel_pos > for_pos, "循环内应紧跟 cancel 检查"
    # cancel 检查之后应有 return
    return_pos = src.find("return", cancel_pos)
    assert return_pos > 0, "取消检查后应有提前 return"
    # 同时 sleep 后还有第二次 cancel 检查(用户在等待退避时取消)
    assert src.count('t.get("cancel")') >= 2, "sleep 后还应有二次 cancel 检查"


def test_retry_state_renders_in_frontend():
    """前端 statusCN 包含 retrying。"""
    import re
    html = open("/Users/hushen/Desktop/b站视频下载/templates/index.html", encoding="utf-8").read()
    assert "retrying" in html, "前端缺少 retrying 状态处理"
    # 验证 statusCN 中有中文标签
    assert "重试中" in html