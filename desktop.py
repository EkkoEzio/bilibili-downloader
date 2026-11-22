"""桌面窗口壳:原生窗口 + 内嵌本机服务 + 后台常驻。

双击 .app 之后:不开终端、不进浏览器,直接开一个系统原生窗口
(mac 用 WKWebView,Windows 用 WebView2)。

这个应用本质上是个「监控器」——盯住 UP 的新投稿并自动下载。所以:

  * **关窗口 ≠ 退出**:点左上角关闭只是把窗口收起来,服务、追更轮询、
    下载队列全部继续在后台跑(托盘图标常驻,菜单里可再唤出窗口/退出)。
    此前关闭窗口会让进程直接死掉——后台线程被强杀,macOS 还会弹崩溃
    提示,正在下的任务也一起断(2026-11-22 用户报障)。
  * **退出要走托盘菜单**:菜单里的「退出应用」会先优雅停掉 HTTP 服务,
    再关窗口、退出进程,不产生崩溃弹窗。

业务逻辑一行没改——本文件只做四件事:
  1. 把 bundle 内置的可执行目录补进 PATH(ffmpeg/ffprobe 的硬依赖);
  2. 把 app.py 的 Flask 服务跑在后台线程里(用 make_server,便于优雅停);
  3. 等服务 ready 后用 webview 打开窗口;
  4. 接管「关窗口」,改成收起到托盘。

源码运行(开发调试)也能用:python desktop.py。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_TITLE = "B站下载"
READY_TIMEOUT = 60.0


def prepare_environment() -> None:
    """打包后把 bundle 里的可执行目录补进 PATH。

    ffmpeg/ffprobe 是音视频合并的硬依赖。源码运行时靠系统 PATH(启动器
    启动.command 会检查 command -v);打包后 .app 启动时 PATH 不含
    /opt/homebrew/bin,yt-dlp 找不到 ffmpeg → 合并失败。这里在导入任何
    业务模块之前补上。
    """
    import runtime_paths

    bundled = runtime_paths.bundled_bin_dir()
    if bundled:
        current = os.environ.get("PATH", "")
        os.environ["PATH"] = (f"{bundled}{os.pathsep}{current}"
                              if current else str(bundled))


def show_error(title: str, message: str) -> None:
    """启动失败时给一个能看见的提示(GUI 里没有终端可看日志)。"""
    print(f"{title}: {message}", file=sys.stderr)
    if sys.platform == "darwin":
        try:
            script = (f'display alert {json_quote(title)} '
                      f'message {json_quote(message)}')
            subprocess.run(["osascript", "-e", script], check=False,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def json_quote(text: str) -> str:
    """给 AppleScript 用的引号转义。"""
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'


def wait_ready(port: int, timeout: float = READY_TIMEOUT) -> bool:
    """等后端 ready 再开窗口——否则窗口会先显示一个连接失败页。"""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/config"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if 200 <= response.status < 400:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.4)
    return False


def tray_image():
    """托盘图标:用应用自己的图标,找不到就画一个粉色方块。"""
    try:
        from PIL import Image
        import runtime_paths

        candidates = [
            runtime_paths.bundle_root() / "static" / "icons" / "app-icon-32.png",
            Path(__file__).resolve().parent / "static" / "icons" / "app-icon-32.png",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return Image.open(candidate).convert("RGBA")
        return Image.new("RGBA", (32, 32), (251, 114, 153, 255))
    except Exception:
        return None


def start_server(app_module, port: int):
    """起 Flask 服务并返回 (server, 启动函数)。

    用 werkzeug 的 make_server 而不是 app.run():app.run() 把 server 对象
    藏在内部,退出时没法优雅 shutdown——只能靠杀进程,那会连带把后台线程
    强杀(正在下的任务断掉,macOS 还会弹崩溃提示)。
    """
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, app_module.app, threaded=True)

    def loop() -> None:
        try:
            server.serve_forever()
        except Exception:
            pass

    return server, loop


def _port_is_free(port: int) -> bool:
    """探一下端口能不能绑。

    先探再绑:直接让 werkzeug 去绑,失败时它会 sys.exit(1)
    (SystemExit,不是 OSError)→ GUI 模式下没有任何输出,应用直接静默消失。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def start_server_on_free_port(app_module, preferred: int, attempts: int = 20):
    """在 preferred 及之后的端口上找一个能绑定的,返回 (port, server, loop)。

    打包版是双击启动的,不该因为「本机已经跑着一个实例」就打不开——
    源码版、装完的副本、浏览器版都可能占着默认端口(5050)。
    这里往后找一个空闲端口,窗口照常打开(它指向自己的端口)。
    """
    last = None
    for offset in range(attempts):
        port = preferred + offset
        if port > 65535:
            break
        if not _port_is_free(port):
            last = OSError(f"端口 {port} 已被占用")
            continue
        try:
            server, loop = start_server(app_module, port)
            return port, server, loop
        except (OSError, SystemExit) as exc:
            # werkzeug 绑定失败时走的是 sys.exit(1)(SystemExit);
            # 只接 OSError 会漏掉它 → 整个应用静默退出(2026-11-22 实测)。
            last = exc
            continue
    raise last if last else OSError("没有可用端口")


def selftest() -> int:
    """打包后冒烟自检:起服务 → 探活 → 退出。

    打包成 GUI 之后 stdout/stderr 没有终端可看,所以用退出码 + 数据目录里
    的 selftest.json 汇报:
        0 = 服务已就绪(模板/静态资源/数据目录都正常)
        3 = 服务没起来
        4 = 导入 app 失败(单实例锁拿不到等)

    CI 用它验证「冻结后的包真的能启动」——这是打包链路唯一能自动判定的
    端到端检查:模板能不能找到、静态资源能不能出、数据目录有没有被外置。
    """
    prepare_environment()
    try:
        import app as app_module
    except SystemExit as exc:
        print(f"启动失败:{exc}", file=sys.stderr)
        return 4

    import config
    import pagecache
    import auto_refresh

    try:
        port, server, loop = start_server_on_free_port(app_module,
                                                      int(config.PORT))
    except OSError as exc:
        # 端口被占用是自检最常见的失败:直接给出可判定的退出码,别抛栈
        print(f"端口不可用:{exc}", file=sys.stderr)
        return 3

    def run() -> None:
        try:
            pagecache.purge_stale()
            auto_refresh.start_loop()
        except Exception:
            pass
        loop()

    threading.Thread(target=run, daemon=True, name="bd-selftest").start()
    if not wait_ready(port, timeout=30):
        return 3
    try:
        report = config.DATA_DIR / "selftest.json"
        report.write_text(
            json.dumps({"ok": True, "data_dir": str(config.DATA_DIR),
                        "port": port}, ensure_ascii=False),
            encoding="utf-8")
        print(f"data_dir={config.DATA_DIR}")
    except OSError:
        pass
    server.shutdown()
    return 0


def instance_info_path():
    """桌面实例的运行时信息(端口等),给「第二个实例」用。"""
    import runtime_paths

    return runtime_paths.data_dir() / "instance.json"


def write_instance_info(port: int) -> None:
    """记录本实例的端口。

    再双击一次时,第二个实例靠它找到已有实例的服务端口,
    于是变成「只开窗口的客户端」,而不是弹一句「已有实例在运行」
    (2026-11-22 用户报障:退不出去也打不开)。
    """
    try:
        instance_info_path().write_text(
            json.dumps({"pid": os.getpid(), "port": int(port)},
                       ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_instance_port(default: int = 5050) -> int:
    """已有实例的端口:优先读 instance.json,读不到就试默认端口。"""
    try:
        payload = json.loads(instance_info_path().read_text(encoding="utf-8"))
        port = int(payload.get("port") or 0)
        if 1 <= port <= 65535:
            return port
    except (OSError, ValueError, TypeError):
        pass
    return int(default)


def make_closing_handler(window, state):
    """生成「关窗口」的处理器:收进后台,而不是退出。

    返回 False 表示「这次关闭不算数」——进程继续跑,下载与追更都不受影响
    (2026-11-22 用户要求:这东西本质是个监控器)。

    用**最小化**而不是隐藏:最小化的窗口留在 Dock 里,点一下 Dock 图标
    就回来了;隐藏掉则窗口凭空消失,用户不知道去哪找、也以为应用死了
    (用户报障「退不出去」就是这么来的)。最小化不可用时才回退到隐藏。
    """
    def on_closing():
        if state.get("quitting"):
            return True
        try:
            window.minimize()
        except Exception:
            try:
                window.hide()
            except Exception:
                return True
        return False

    return on_closing


def _open_client_window() -> int:
    """已有实例在跑时的行为:开一个连到它的窗口,而不是报错退出。

    这样「再双击一次」一定有反应:第二个进程只是个窗口客户端,
    关掉它不影响已有实例(服务、下载、追更都还在那里跑)。
    (2026-11-22 用户报障:双击后弹「已有另一个实例在运行」,打不开。)
    """
    try:
        import webview
    except ImportError as import_error:
        show_error("缺少依赖", f"pywebview 未安装:{import_error}")
        return 1

    port = read_instance_port()
    window = webview.create_window(APP_TITLE, f"http://127.0.0.1:{port}/",
                                   width=1320, height=900, min_size=(960, 640))

    def quit_client() -> None:
        try:
            window.destroy()
        except Exception:
            pass

    # 窗口客户端也盯着退出请求:界面上按「退出应用」时,
    # 所有窗口一起关掉,不留一个连着已停服务的空壳。
    threading.Thread(target=watch_quit_request, args=(quit_client,),
                     daemon=True, name="bd-quit-watch").start()

    try:
        webview.start()
    except Exception:
        pass
    return 0


def watch_quit_request(on_quit) -> None:
    """盯着数据目录里的 quit.request:界面里的「退出应用」按下去时,
    桌面壳优雅停服务、关窗口、退出进程。

    走文件而不是直接 os._exit:直接退出会打断任务记录的持久化。
    """
    import runtime_paths

    path = runtime_paths.data_dir() / "quit.request"
    while True:
        try:
            if path.exists():
                # 不删这个文件:守护进程与窗口客户端都要看到它,
                # 谁先删谁就把信号吃掉了。残留由下次启动清理。
                on_quit()
                return
        except OSError:
            pass
        time.sleep(0.4)


def clear_stale_quit_request() -> None:
    """启动时清掉上一次的退出请求残留。"""
    import runtime_paths

    try:
        path = runtime_paths.data_dir() / "quit.request"
        if path.exists():
            path.unlink()
    except OSError:
        pass


def main() -> int:
    prepare_environment()

    try:
        import app as app_module
    except SystemExit:
        # 已经有实例在跑(app.py 拿不到单实例锁就会退出)。
        # 这里不报错退出,改为「只开窗口的客户端」——
        # 再双击一次应该能打开窗口,而不是弹一句「已有另一个实例在运行」。
        # 注意:这一步不能清退出请求,否则会把主实例的退出信号吃掉。
        return _open_client_window()

    # 拿到单实例锁 = 本进程是主实例,这时才清上一次的退出请求残留
    clear_stale_quit_request()

    import config
    import pagecache
    import auto_refresh

    try:
        port, server, loop = start_server_on_free_port(app_module,
                                                      int(config.PORT))
    except OSError as exc:
        show_error("无法启动", f"找不到可用端口:{exc}")
        return 1

    def run() -> None:
        try:
            pagecache.purge_stale()          # 清掉超 30 天没更新的缓存
            auto_refresh.start_loop()        # 订阅追更轮询
        except Exception:
            pass
        loop()

    threading.Thread(target=run, daemon=True, name="bd-server").start()

    if not wait_ready(port):
        show_error("无法启动",
                   f"服务在 {int(READY_TIMEOUT)} 秒内没有就绪。"
                   "端口可能已被占用,或依赖缺失。")
        return 1

    # 记录端口:再双击一次时,第二个实例靠它连到我们
    write_instance_info(port)

    try:
        import webview
    except ImportError as exc:
        show_error("缺少依赖", f"pywebview 未安装:{exc}")
        return 1

    window = webview.create_window(APP_TITLE, f"http://127.0.0.1:{port}/",
                                   width=1320, height=900, min_size=(960, 640))

    state = {"tray": None, "quitting": False}

    def show_window() -> None:
        try:
            window.show()
        except Exception:
            pass

    def open_download_dir() -> None:
        try:
            import config as cfg
            from downloader import open_dir
            open_dir(cfg.DOWNLOAD_DIR)
        except Exception:
            pass

    def quit_app() -> None:
        """优雅退出:先停 HTTP 服务,再关窗口。"""
        if state["quitting"]:
            return
        state["quitting"] = True
        tray = state.get("tray")
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    window.events.closing += make_closing_handler(window, state)

    # 界面里的「退出应用」:后端写 quit.request,这里收信号后优雅退出
    threading.Thread(target=watch_quit_request, args=(quit_app,),
                     daemon=True, name="bd-quit-watch").start()

    # 托盘:没有它就没有「再唤出窗口」和「真正退出」的入口。
    #
    # 两个坑(实测踩过):
    #  1. 必须在**主线程**建 —— AppKit 的 NSStatusBar 项不允许在别的线程创建;
    #  2. macOS 上只能用 run_detached(),不能用 run()。pystray 的 run() 会
    #     再跑一次 NSApp.run(),和 pywebview 的事件循环撞车 → 直接 SIGTRAP
    #     崩溃(状态项本身在构造时就已挂进共享的 NSApp,run_detached 只是
    #     「标记就绪」,不另起循环)。
    try:
        import pystray

        image = tray_image()
        if image is not None:
            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", lambda icon, item: show_window(),
                                 default=True),
                pystray.MenuItem("打开下载目录",
                                 lambda icon, item: open_download_dir()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出应用", lambda icon, item: quit_app()),
            )
            tray = pystray.Icon("bili-download", image, APP_TITLE, menu)
            state["tray"] = tray
            if sys.platform == "darwin":
                tray.run_detached()
            else:
                threading.Thread(target=tray.run, daemon=True,
                                 name="bd-tray").start()
    except Exception as exc:      # 没有托盘也不该起不来
        print(f"托盘不可用(关闭窗口将直接退出):{exc}", file=sys.stderr)
        state["tray"] = None

    try:
        webview.start()
    finally:
        if state.get("tray") is not None:
            try:
                state["tray"].stop()
            except Exception:
                pass
        try:
            server.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
