# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本(白名单原则)。

只有「运行时必需」进包:
    templates/  static/  顶层业务模块  内置 bin/(ffmpeg/ffprobe)

一律不进包:
    data/            登录态、任务史、UP 清单、页面缓存、日志 —— 开发者的个人信息
    docs/ tests/ scripts/ design-demos/ gui-test-screenshots/
    CHANGELOG.md README.md .git/ .venv/ __pycache__/ .pytest_cache/ .DS_Store
    启动.command

打包完由 packaging/scan_artifact.py 再扫一遍产物,命中即构建失败。
构建时务必把源码先复制到不含个人信息的临时目录再打包:
.pyc 里会留下源码路径(co_filename),在开发者机器上直接打包会把自己的
家目录路径烧进产物——scan_artifact.py 会因此判定失败(这是故意的)。
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent          # packaging/ 的上一级 = 项目根
BUNDLE_NAME = "BiliDownloader"
block_cipher = None                                  # 不做字节码加密


def _read_version() -> str:
    """版本号的唯一来源是仓库根的 VERSION 文件。

    之前这里把版本硬编码在 spec 里,发版时改了 CHANGELOG 忘了改 spec,
    安装包元数据就会比代码旧一个版本。
    """
    import re

    version_file = ROOT / "VERSION"
    if version_file.is_file():
        value = version_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", value):
            return value
    return "0.0.0"


APP_VERSION = _read_version()

# ---- 只收运行时真正需要的静态资源 ----
datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
]

# 内置 ffmpeg/ffprobe(可选):packaging/bin/ 下放静态构建版,
# 家人机器不用自己装 ffmpeg。放了就一起进包。
bundled_bin = ROOT / "packaging" / "bin"
if bundled_bin.is_dir() and any(bundled_bin.iterdir()):
    datas.append((str(bundled_bin), "bin"))

hiddenimports = collect_submodules("yt_dlp") + [
    "qrcode",
    "PIL.Image",
    "PIL.ImageDraw",
    "pync",
] + collect_submodules("pystray")   # 托盘:平台后端是动态导入的,必须显式收集

# pystray 的 macOS 后端要 AppKit;Windows 后端要 pywin32 的一部分。
# 缺了 AppKit 时托盘会静默降级(窗口关掉后就没法唤回、也没法优雅退出),
# 所以这里显式带上,缺依赖要构建期就发现。
try:
    import AppKit  # noqa: F401
    hiddenimports += ["AppKit", "Foundation", "objc", "Quartz"]
except ImportError:
    pass

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "_pytest", "IPython", "notebook", "jupyter",
        "setuptools", "pip", "wheel", "tkinter.test", "test",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 应用图标:mac 用 .icns,Windows 用 .ico。都没有也不该让构建挂掉,
# 但有图标和没图标,用户在 Dock/访达/任务栏里的观感差别很大。
_icon = ROOT / "packaging" / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
_icon_arg = str(_icon) if _icon.is_file() else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # 桌面应用:不弹终端窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,         # CI 里用 --target-arch 决定 arm64 / x86_64
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon_arg,           # Windows 可执行文件图标
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BiliDownloader",
)

# macOS:打成 .app。图标可选——没有就不设,别让构建在这里断掉。
if sys.platform == "darwin":
    icon_path = ROOT / "packaging" / "icon.icns"
    app = BUNDLE(
        coll,
        # bundle 目录名用 ASCII:B站下载 里的中文会让旧版 bash / 部分
        # Windows CI 工具在处理路径时出错(实测 bash 3.2 解析不了)。
        # 用户看到的名字仍然是中文(CFBundleName / CFBundleDisplayName)。
        name="BiliDownloader.app",
        icon=_icon_arg,
        bundle_identifier="app.bilibili.downloader",
        version=APP_VERSION,
        info_plist={
            "CFBundleName": "B站下载",
            "CFBundleDisplayName": "B站下载",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "LSMinimumSystemVersion": "11.0",
            # 不写开发者姓名/个人邮箱:安装包元数据同样不能带个人信息
            "NSHighResolutionCapable": True,
        },
    )
