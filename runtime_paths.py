"""运行时路径的唯一来源。

为什么不能只写在 config.py 里:
config.py 要 import storage,而 storage 又可能被别的模块 import;
instance_lock.py 必须在任何运行态数据被读取之前拿到路径,不能绕一圈
import config(会拉起 storage)。所以路径规则放在这个零依赖的模块里,
config.py 与 instance_lock.py 都从这里取——避免出现第二个路径源
(2026-11-22 审查发现 instance_lock.py 曾经自写一份 data/app.lock 路径,
打包后会与 config.py 的数据目录分叉)。

打包成桌面应用后,运行数据必须外置:
  * macOS 的 .app bundle 与 Windows 的 Program Files 都是只读的,写进去
    会破坏代码签名(macOS 直接拒绝启动),而且 /Applications 下没有写权限;
  * PyInstaller onefile 模式下 __file__ 落在临时解压目录,退出即销毁 →
    登录态与任务史每次启动归零;
  * 同一台机器上的多个用户共用一份数据目录,等于把一个人的 SESSDATA
    暴露给同机所有用户。

所以:源码运行走项目内 data/(开发方便,已在 .gitignore 里);
打包运行走系统约定的用户数据目录。BD_DATA_DIR 可显式覆盖(便携版/自测)。
"""
import os
import sys
from pathlib import Path

# 应用在用户数据目录里的名字(安装包与数据目录同名,便于用户辨认)
APP_DIR_NAME = "BiliDownloader"


def is_frozen() -> bool:
    """PyInstaller 冻结后为真;源码运行永远为假。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """冻结后 bundle 内解包出来的目录(只读)。源码运行即项目目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def base_dir() -> Path:
    """应用代码所在目录。冻结后是 bundle 内部,只读,不要往里写。"""
    return Path(__file__).resolve().parent


def bundled_bin_dir() -> Path | None:
    """bundle 内置的可执行文件目录(ffmpeg/ffprobe 放这里)。

    不存在就返回 None——调用方据此决定是否需要自己找 PATH 上的版本。
    """
    candidate = bundle_root() / "bin"
    if candidate.is_dir():
        return candidate
    return None


def data_dir() -> Path:
    """运行数据目录(cookies / 任务史 / UP 清单 / 配置 / 页面缓存 / 日志)。"""
    override = os.environ.get("BD_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if not is_frozen():
        return base_dir() / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if os.name == "nt":
        root = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(root) / APP_DIR_NAME
    root = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(root) / APP_DIR_NAME


def ensure_data_dir() -> Path:
    """确保数据目录存在。

    建不出来时不抛异常:原来的写法在 import 阶段直接 mkdir,目录不可写
    就让整个应用崩在启动第一步、连一句错误提示都出不来。改成不抛,
    真正要用到数据时再各自报错。
    """
    target = data_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target
