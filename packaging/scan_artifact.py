#!/usr/bin/env python3
"""产物隐私扫描:安装包里不能出现开发者的个人信息。

三层检查,任一命中即 exit 1(让构建失败):

  1. 清单层 —— 产物里出现禁止路径(data/ docs/ tests/ .git/ .DS_Store …)
     就失败。这层最先失败、成本最低,也最难绕过。
  2. 文本层 —— py/pyc/js/css/html/json/txt/md/plist 里的关键词命中。
  3. 二进制层 —— pyc/so/dylib/可执行里 strings 出来的关键词命中。

关键词的来源:
  * 自动:构建机的家目录名、项目目录名、家目录路径正则;
  * 自动:构建机 data/ 里真实的账号指纹、UP UID、真实文件名(不写进仓库,
    否则扫描脚本自己就把个人信息带进了仓库);
  * 手动:--keyword / --keywords-file / 环境变量 SCAN_EXTRA_KEYWORDS。

用法:
    python packaging/scan_artifact.py dist/BiliDownloader
    python packaging/scan_artifact.py dist/B站下载.app --keyword SomeName
"""
from __future__ import annotations

import argparse
import json
import os
import re
import string
import sys
from pathlib import Path

# Windows 控制台默认是 charmap(chcp 437/1252),print 中文会 UnicodeEncodeError
# 直接把扫描脚本打崩 —— 2026-11-22 首次发版时 Windows job 就死在这。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 产物里不允许出现的顶层路径(相对产物根)。只查顶层,避免依赖自带的
# tests/scripts 子包造成误报。
FORBIDDEN_TOP_LEVEL = {
    "data", "docs", "tests", "scripts", "design-demos", "artifacts",
    "gui-test-screenshots", "__pycache__", ".pytest_cache", ".venv",
    ".git", ".DS_Store", "CHANGELOG.md", "README.md", "启动.command",
    "build", "dist", ".idea", ".vscode",
}

# 允许出现的路径前缀(人工评审过)。CI 的工作区在 /home/runner/work 下,
# 那是流水线机器的路径,不含个人信息。
DEFAULT_ALLOW_PREFIXES = [
    "/tmp/", "/var/tmp/", "/home/runner/", "/Users/runner/",
    "/opt/hostedtoolcache/", "/Library/Developer/",
]

# 家目录路径正则:任何开发者用户名都会被这层抓住,不需要把名字写进脚本
HOME_PATH_PATTERNS = [
    re.compile(r"/Users/[A-Za-z0-9._-]+"),
    re.compile(r"/home/[A-Za-z0-9._-]+"),
    re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+"),
]

TEXT_SUFFIXES = {".py", ".pyc", ".js", ".css", ".html", ".json", ".txt",
                 ".md", ".plist", ".cfg", ".ini", ".yaml", ".yml", ".xml",
                 ".spec", ".sh"}

# 家目录/目录名里的通用词:出现在 CI 机器路径中,不能当泄露关键词
GENERIC_HOME_NAMES = {
    "runner", "admin", "user", "root", "build", "host", "ci", "docker",
    "nobody", "github", "actions", "home", "tmp", "temp", "usr", "default",
    "_temp", "work", "project", "src", "app",
}
BINARY_SUFFIXES = {".so", ".dylib", ".dll", ".pyd", ".bin"}
TEXT_CHARS = set(string.printable.encode("ascii"))


def is_probably_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return False
    if not chunk:
        return True
    printable = sum(1 for byte in chunk if byte in TEXT_CHARS)
    return printable / len(chunk) > 0.85


def collect_keywords(extra: list[str], data_dir: Path | None) -> list[str]:
    """汇总关键词。开发者标识从构建机环境推导,不写死在仓库里。"""
    keywords: set[str] = set(extra)
    env_extra = os.environ.get("SCAN_EXTRA_KEYWORDS", "")
    keywords.update(part.strip() for part in env_extra.split(",") if part.strip())

    # 构建机的家目录名与项目目录名
    #
    # 家目录名要排除通用词:CI 上家目录是 /home/runner,"runner" 这种词在
    # 产物里到处都是(/home/runner/work/...),拿它当关键词会误伤整条构建
    # (2026-11-22 首次发版 mac 与 Windows 同时死在扫描这一步)。
    # 路径形式的泄露由 HOME_PATH_PATTERNS 负责,这里只补「路径之外的裸名字」。
    home = Path.home()
    if home.name and home.name.lower() not in GENERIC_HOME_NAMES:
        keywords.add(home.name)
    project_name = Path.cwd().name
    if project_name and project_name.lower() not in GENERIC_HOME_NAMES:
        keywords.add(project_name)

    # data/ 里的真实标识:账号指纹、UP UID、真实文件名
    if data_dir and data_dir.is_dir():
        for path in data_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() in {".json", ".txt"}:
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                try:
                    payload = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    continue
                for fingerprint in _walk_strings(payload):
                    if len(fingerprint) >= 16 and re.fullmatch(r"[0-9a-f]+", fingerprint):
                        keywords.add(fingerprint)
                    if re.fullmatch(r"\d{6,12}", fingerprint):
                        keywords.add(fingerprint)
    return sorted(k for k in keywords if k)


def _walk_strings(payload) -> list[str]:
    out: list[str] = []
    if isinstance(payload, str):
        out.append(payload)
    elif isinstance(payload, dict):
        for value in payload.values():
            out.extend(_walk_strings(value))
    elif isinstance(payload, list):
        for value in payload:
            out.extend(_walk_strings(value))
    return out


def scan_paths(root: Path) -> list[str]:
    problems = []
    for current, dirnames, filenames in os.walk(root):
        rel_dir = Path(current).relative_to(root)
        parts = rel_dir.parts
        if parts and parts[0] in FORBIDDEN_TOP_LEVEL:
            problems.append(f"[清单] 禁止的顶层路径: {rel_dir}")
            dirnames[:] = []
            continue
        for name in filenames:
            rel = Path(current, name).relative_to(root)
            if rel.parts and rel.parts[0] in FORBIDDEN_TOP_LEVEL:
                problems.append(f"[清单] 禁止的文件: {rel}")
            elif name == ".DS_Store":
                problems.append(f"[清单] Finder 痕迹: {rel}")
    return problems


def scan_text(root: Path, keywords: list[str],
               allowed: list[str] | None = None) -> list[str]:
    allowed = allowed or []
    problems = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in TEXT_SUFFIXES and not is_probably_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # 上游 Python 运行时与动态库里的构建机路径(如 python.org 发行版的
        # /Users/nad)与开发者无关,两路扫描都要豁免。
        if is_upstream_binary(str(path.relative_to(root))):
            continue
        for keyword in keywords:
            if not keyword:
                continue
            # 命中处若落在允许前缀内(如 CI 的 /home/runner/work/...),
            # 不算泄露——否则 CI 机器自己的路径会让每次构建都失败。
            index = text.find(keyword)
            while index != -1:
                window = text[max(0, index - 80):index + len(keyword) + 80]
                if not any(prefix in window for prefix in allowed):
                    problems.append(
                        f"[文本] {path.relative_to(root)} 命中关键词 {keyword!r}")
                    break
                index = text.find(keyword, index + 1)
        for pattern in HOME_PATH_PATTERNS:
            match = pattern.search(text)
            if match:
                problems.append(
                    f"[文本] {path.relative_to(root)} 含家目录路径 {match.group(0)!r}")
                break
    return problems


# 上游二进制与运行时豁免(2026-11-23 审查,遗留项 18 收窄):里面嵌的是
# CPython/依赖/ffmpeg 自己的构建机路径(python.org 发行版里就是 /Users/nad),
# 与开发者无关,不能算泄露。不豁免的话,CI 上每个 mac 包都会因为这条误判
# 失败(2026-11-22 实测)。豁免按「上游特征」判定——后缀(.so/.dylib/.pyd)、
# 运行时目录(frameworks/、python.framework)、内置 ffmpeg/ffprobe——不再按
# _internal/ 位置整体豁免:PyInstaller 6+ onedir 把我们自己的代码与静态资源
# 也放 _internal/,整体跳过等于对 bundle 主体失明。
BINARY_SKIP_MARKERS = (
    "frameworks/", "contents/resources/python", "python.framework",
    ".dylib", ".so", ".pyd", "ffmpeg", "ffprobe",
)


def is_upstream_binary(rel_path: str) -> bool:
    low = rel_path.lower().replace("\\", "/")
    return any(marker in low for marker in BINARY_SKIP_MARKERS)


def scan_binary(root: Path, keywords: list[str]) -> list[str]:
    problems = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if is_upstream_binary(rel):
            continue
        suffix = path.suffix.lower()
        if suffix not in BINARY_SUFFIXES and not is_probably_text(path):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            blob = data
        else:
            continue
        for keyword in keywords:
            if keyword and keyword.encode("utf-8", "ignore") in blob:
                problems.append(f"[二进制] {rel} 命中关键词 {keyword!r}")
        for pattern in HOME_PATH_PATTERNS:
            match = pattern.search(blob.decode("latin-1", "ignore"))
            if match:
                problems.append(
                    f"[二进制] {rel} 含家目录路径 {match.group(0)!r}")
                break
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="安装包个人信息扫描")
    parser.add_argument("artifact", help="产物目录(.app / onedir 目录)")
    parser.add_argument("--keyword", action="append", default=[],
                        help="额外关键词(可重复)")
    parser.add_argument("--keywords-file", default="",
                        help="额外关键词文件(每行一个)")
    parser.add_argument("--data-dir", default="",
                        help="构建机的运行数据目录,用于提取真实指纹/UID")
    parser.add_argument("--allow-prefix", action="append", default=[],
                        help="允许出现的路径前缀(可重复)")
    args = parser.parse_args()

    artifact = Path(args.artifact).resolve()
    if not artifact.exists():
        print(f"[失败] 产物不存在: {artifact}", file=sys.stderr)
        return 1

    extra = list(args.keyword)
    if args.keywords_file:
        for line in Path(args.keywords_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                extra.append(line)

    data_dir = Path(args.data_dir) if args.data_dir else None
    keywords = collect_keywords(extra, data_dir)
    print(f"[扫描] 产物: {artifact}")
    print(f"[扫描] 关键词 {len(keywords)} 个(含构建机环境推导)")

    allowed = DEFAULT_ALLOW_PREFIXES + list(args.allow_prefix)
    problems = scan_paths(artifact)
    problems += scan_text(artifact, keywords, allowed)
    problems += scan_binary(artifact, keywords)

    problems = [item for item in problems
                if not any(prefix in item for prefix in allowed)]

    if problems:
        print("[失败] 产物含个人信息或禁止内容:", file=sys.stderr)
        for item in problems:
            print("  " + item, file=sys.stderr)
        return 1
    print("[通过] 产物未发现个人信息")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
