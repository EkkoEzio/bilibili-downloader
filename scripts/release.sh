#!/usr/bin/env bash
# 一条命令发版。
#
#   scripts/release.sh          # 自动把次版本号 +1(2.8.0 → 2.9.0)
#   scripts/release.sh 2.9.1    # 指定版本号
#
# 做的事:版本号落地(VERSION + CHANGELOG + Inno 脚本)→ 提交 → 打 tag → 推。
# 之后 GitHub Actions 自动出三个安装包(mac arm64 / mac x86_64 / windows x64)
# 并发布到 Releases 页。
#
# 不做的事:不在本机打安装包、不发布 Release——那些由 CI 负责。
#
# 版本号的唯一来源是仓库根的 VERSION 文件:
#   * CHANGELOG 顶部的「## [未发布]」在发版时转成「## [vX.Y.Z] - 日期」
#   * PyInstaller spec 读 VERSION 写进 .app 元数据
#   * Inno Setup 脚本里的 AppVersion 同步更新
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

VERSION_FILE="$ROOT/VERSION"
[[ -f "$VERSION_FILE" ]] || { echo "错误:缺少 $VERSION_FILE" >&2; exit 1; }
CURRENT="$(tr -d '[:space:]' < "$VERSION_FILE")"

if [[ -n "${1:-}" ]]; then
  VERSION="$1"
else
  # 自动 +1 次版本号:2.8.0 → 2.9.0
  VERSION="$(python3 - "$CURRENT" <<'PY'
import sys

parts = sys.argv[1].split(".")
if len(parts) != 3 or not all(p.isdigit() for p in parts):
    sys.exit(f"当前版本号格式异常: {sys.argv[1]}")
major, minor, patch = (int(p) for p in parts)
print(f"{major}.{minor + 1}.0")
PY
)"
fi

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误:版本号格式应为 x.y.z,收到: $VERSION" >&2
  exit 1
fi
if [[ "$VERSION" == "$CURRENT" ]]; then
  echo "错误:版本号与当前版本相同($CURRENT)" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "错误:工作区不干净,先提交或 stash" >&2
  git status --short >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
  echo "错误:tag v$VERSION 已存在" >&2
  exit 1
fi

echo "==> 版本号 $CURRENT → $VERSION"

python3 - "$VERSION" <<'PY'
import datetime
import re
import sys
from pathlib import Path

version = sys.argv[1]
today = datetime.date.today().isoformat()

# 1) CHANGELOG:「## [未发布]」→「## [vX.Y.Z] - 日期」
#    标题格式必须和历史一致(带方括号 + 日期),否则 README/文档里的链接对不上。
changelog = Path("CHANGELOG.md")
text = changelog.read_text(encoding="utf-8")
pattern = re.compile(r"^## \[未发布\]\s*$", re.M)
if not pattern.search(text):
    sys.exit("CHANGELOG.md 里找不到「## [未发布]」段——发版前应先把改动写进这一段")
text = pattern.sub(f"## [v{version}] - {today}", text, count=1)
changelog.write_text(text, encoding="utf-8")
print(f"CHANGELOG.md → ## [v{version}] - {today}")

# 2) VERSION 文件
Path("VERSION").write_text(version + "\n", encoding="utf-8")
print(f"VERSION → {version}")

# 3) Inno Setup 脚本里的版本宏(Windows 安装包元数据)
iss = Path("packaging/bilibili.iss")
if iss.is_file():
    content = iss.read_text(encoding="utf-8")
    updated = re.sub(r'(#define AppVersion ")[^"]+(")',
                       lambda m: m.group(1) + version + m.group(2),
                       content, count=1)
    if updated != content:
        iss.write_text(updated, encoding="utf-8")
        print(f"packaging/bilibili.iss → {version}")
PY

git add -A
git commit -m "release: v$VERSION"
git tag -a "v$VERSION" -m "v$VERSION"

echo "==> 推送提交与 tag"
git push origin main
git push origin "v$VERSION"

echo
echo "==> 已推送 v$VERSION。"
echo "    GitHub Actions 会自动出三个安装包并发布到 Releases 页。"
echo "    查看进度: https://github.com/EkkoEzio/bilibili-downloader/actions"
