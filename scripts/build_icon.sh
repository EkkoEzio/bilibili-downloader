#!/usr/bin/env bash
# 生成应用图标的全部尺寸。
#
#   packaging/icon.svg  →  packaging/icon.icns (macOS)
#                        →  packaging/icon.ico  (Windows)
#                        →  static/icons/app-icon.png (界面/favicon)
#
# 产物 .icns / .ico 要提交进仓库:Windows CI 上没有 rsvg-convert,
# 不能在流水线里现生成。
#
# macOS 的 .icns 需要留边(方形图标占画布约 80%),Windows 的 .ico 是满幅,
# 所以两种平台用同一份源文件、不同的留边策略。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PKG="$ROOT/packaging"
SVG="$PKG/icon.svg"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

command -v rsvg-convert >/dev/null 2>&1 || { echo "错误:需要 rsvg-convert(brew install librsvg)" >&2; exit 1; }
[[ -f "$SVG" ]] || { echo "错误:找不到 $SVG" >&2; exit 1; }

WORK="$(mktemp -d)"
cleanup() { cd "$ROOT" 2>/dev/null || true; rm -rf "$WORK" 2>/dev/null || true; }
trap cleanup EXIT

echo "==> 渲染满幅 PNG(Windows .ico / 界面用)"
FULL="$WORK/full.png"
rsvg-convert -w 1024 -h 1024 "$SVG" -o "$FULL"

echo "==> 生成 icon.ico(Windows,满幅)"
"$PYTHON" - "$FULL" "$PKG/icon.ico" <<'PY'
import sys
from PIL import Image

src = Image.open(sys.argv[1]).convert("RGBA")
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
src.save(sys.argv[2], format="ICO", sizes=sizes)
print("icon.ico ->", [f"{w}x{h}" for w, h in sizes])
PY

echo "==> 生成 icon.icns(macOS,方形图标留 80% 边)"
"$PYTHON" - "$FULL" "$WORK/inset.png" <<'PY'
import sys
from PIL import Image

# macOS 图标规范:圆角方块约占画布 80%,四周留透明边,
# 这样在 Dock / 访达里与系统图标视觉重量一致。
src = Image.open(sys.argv[1]).convert("RGBA")
side = int(src.width * 0.82)
inset = src.resize((side, side), Image.LANCZOS)
canvas = Image.new("RGBA", src.size, (0, 0, 0, 0))
canvas.paste(inset, ((src.width - side) // 2, (src.height - side) // 2), inset)
canvas.save(sys.argv[2])
print("inset", side, "in", src.width)
PY

SET="$WORK/icon.iconset"
mkdir -p "$SET"
# 用 Pillow 从留边后的 PNG 缩放出各个尺寸(rsvg-convert 只吃 SVG)
"$PYTHON" - "$WORK/inset.png" "$SET" <<'PY'
import sys
from PIL import Image

src = Image.open(sys.argv[1]).convert("RGBA")
out = sys.argv[2]
for px, name in [(16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
                  (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
                  (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
                  (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
                  (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png")]:
    src.resize((px, px), Image.LANCZOS).save(f"{out}/{name}")
print("iconset 10 张写好")
PY
iconutil -c icns "$SET" -o "$PKG/icon.icns"
echo "icon.icns -> $PKG/icon.icns"

echo "==> 生成界面用 PNG"
mkdir -p "$ROOT/static/icons"
rsvg-convert -w 512 -h 512 "$SVG" -o "$ROOT/static/icons/app-icon.png"
rsvg-convert -w 32 -h 32 "$SVG" -o "$ROOT/static/icons/app-icon-32.png"

echo "==> 完成"
ls -la "$PKG/icon.icns" "$PKG/icon.ico" "$ROOT/static/icons/"
