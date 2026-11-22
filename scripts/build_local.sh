#!/usr/bin/env bash
# 本机构建:mac .app → dmg / Windows onedir → setup.exe,并跑产物隐私扫描。
#
# 关键点:先把源码复制到「不含个人信息的临时目录」再打包。
# .pyc 的 co_filename 会留下编译时的源码路径,直接在
# ~/Desktop/b站视频下载/ 下打包,安装包里就会烧进家目录路径——
# packaging/scan_artifact.py 会因此判定构建失败(这是故意的)。
#
# 用法:
#   ARCH=arm64  scripts/build_local.sh     # mac Apple Silicon
#   ARCH=x86_64 scripts/build_local.sh     # mac Intel
#   ARCH=x64    scripts/build_local.sh     # Windows(在 Git Bash 里跑)
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ARCH="${ARCH:-arm64}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
# CI 上没有项目 venv(用 setup-python 装的解释器),回退到 PATH 上的 python。
# 2026-11-22 首次发版时 Windows job 就死在这:找不到 .venv/bin/python 直接退出。
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || true)"
fi
# 版本号来自 VERSION 文件(唯一来源)。之前从 CHANGELOG 里 grep,
# 顶部是「## [未发布]」时就会抓到上一个正式版号,产物名跟着错。
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || true)"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误:VERSION 文件里没有合法版本号" >&2
  exit 1
fi

echo "==> 构建 $ARCH (v${VERSION:-未知})"

if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "错误:找不到 Python 解释器(可用 PYTHON=... 指定)" >&2
  exit 1
fi

# ---- 1. 把源码复制到干净的临时目录 ----
STAGE="$(mktemp -d "/tmp/bili-build-${ARCH}-XXXXXX" 2>/dev/null || mktemp -d)"
cleanup() {
  cd "$ROOT" 2>/dev/null || true
  rm -rf "$STAGE" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> 暂存源码到 $STAGE"
mkdir -p "$STAGE/packaging"
# 只复制运行时必需 + 打包所需的东西。data/ docs/ tests/ 等一律不进暂存目录,
# 它们连「被打包的机会」都没有。
for item in app.py desktop.py runtime_paths.py config.py storage.py instance_lock.py \
            bilibili_api.py bilibili_auth.py parsing.py downloader.py online.py \
            up_list.py auto_refresh.py extras.py quality.py request_governor.py \
            pagecache.py files.py taskstore.py requirements.txt requirements-build.txt \
            VERSION; do
  [[ -f "$ROOT/$item" ]] && cp "$ROOT/$item" "$STAGE/"
done
cp -R "$ROOT/templates" "$ROOT/static" "$STAGE/"
cp -R "$ROOT/packaging/." "$STAGE/packaging/"
if [[ -d "$ROOT/packaging/bin" ]]; then cp -R "$ROOT/packaging/bin" "$STAGE/packaging/"; fi

# ---- 2. 构建依赖(装进独立的构建 venv,不碰项目运行时 venv) ----
# 之前直接往 $ROOT/.venv 里装打包依赖:那正是开发/日常运行用的环境,
# 装卸包会打断正在运行的服务(实测服务在构建期间掉线)。构建环境必须独立。
echo "==> 安装构建依赖(独立 venv)"
BUILD_VENV="$STAGE/.venv-build"
"$PYTHON" -m venv "$BUILD_VENV"
BUILD_PY="$BUILD_VENV/bin/python"
[[ -x "$BUILD_PY" ]] || BUILD_PY="$BUILD_VENV/Scripts/python.exe"
"$BUILD_PY" -m pip install -q --upgrade pip
"$BUILD_PY" -m pip install -q -r "$STAGE/requirements.txt" -r "$STAGE/requirements-build.txt"

# ---- 3. 打包 ----
echo "==> PyInstaller"
cd "$STAGE"
"$BUILD_PY" -m PyInstaller --noconfirm --clean \
    --distpath "$STAGE/dist" --workpath "$STAGE/build" \
    "$STAGE/packaging/bilibili.spec"

# macOS 产出 B站下载.app;Windows 产出 BiliDownloader/ 目录。
APP_BUNDLE="$STAGE/dist/BiliDownloader.app"
ONEDIR="$STAGE/dist/BiliDownloader"
if [[ ! -d "$APP_BUNDLE" && ! -d "$ONEDIR" ]]; then
  echo "错误:没找到打包产物" >&2
  exit 1
fi

# ---- 4. 产物隐私扫描(失败即构建失败) ----
echo "==> 产物隐私扫描"
for target in "$APP_BUNDLE" "$ONEDIR"; do
  [[ -d "$target" ]] || continue
  "$BUILD_PY" "$STAGE/packaging/scan_artifact.py" "$target" --data-dir "$ROOT/data"
done

# ---- 5. 输出到 dist/ ----
echo "==> 收集产物"
mkdir -p "$ROOT/dist"
rm -rf "$ROOT/dist/BiliDownloader.app" "$ROOT/dist/BiliDownloader"
rm -f "$ROOT/dist/"*.dmg "$ROOT/dist/"*.exe
[[ -d "$APP_BUNDLE" ]] && cp -R "$APP_BUNDLE" "$ROOT/dist/"
[[ -d "$ONEDIR" ]] && cp -R "$ONEDIR" "$ROOT/dist/"

# ---- 6. 平台安装包 ----
if [[ "$(uname -s)" == "Darwin" ]]; then
  # 产物文件名全用 ASCII:bash 3.2 处理含中文的绝对路径会出现解析失败
  # (实测 dist 下的中文名文件无法被 cp 打开)。应用在 Finder 里显示的名字
  # 仍是中文(CFBundleDisplayName)。
  DMG="$ROOT/dist/BiliDownloader-${VERSION:-dev}-${ARCH}.dmg"
  STAGE_DMG="$STAGE/dmg"
  mkdir -p "$STAGE_DMG"
  if [[ -d "$ROOT/dist/BiliDownloader.app" ]]; then
    cp -R "$ROOT/dist/BiliDownloader.app" "$STAGE_DMG/"
  else
    echo "错误:没找到 .app,无法打 dmg" >&2
    exit 1
  fi
  # 拖进 Applications 的软链:安装体验跟官方 dmg 一致
  ln -s /Applications "$STAGE_DMG/Applications"
  hdiutil create -volname "B站下载" -srcfolder "$STAGE_DMG" -ov -format UDZO "$DMG"
  echo "==> 产物: $DMG"
else
  # Windows:Inno Setup 出单文件安装程序
  if command -v iscc >/dev/null 2>&1; then
    echo "==> Inno Setup"
    (cd "$ROOT" && iscc packaging/bilibili.iss)
    echo "==> 产物: $ROOT/dist/BiliDownloader-Setup-*.exe"
  else
    echo "==> 跳过安装程序(未找到 iscc;choco install innosetup 可装)"
    echo "    可直接分发 dist/BiliDownloader/ 目录"
  fi
fi

echo "==> 完成。产物在 $ROOT/dist/"
