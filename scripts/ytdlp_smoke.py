#!/usr/bin/env python3
"""yt-dlp 解析冒烟:升级 yt-dlp 前后各跑一次,确认 B 站解析未断。

用法(在项目根目录):
    .venv/bin/python scripts/ytdlp_smoke.py
    .venv/bin/python scripts/ytdlp_smoke.py https://www.bilibili.com/video/BVxxxx [更多URL...]

走生产解析入口 parsing.parse_video(与「新建下载」同一代码路径),只读不下载:
- 单视频:title 非空且清晰度选项非空;
- 多 P:parts 非空。
任何一条失败都以非零码退出。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsing import parse_video  # noqa: E402

DEFAULT_URLS = [
    "https://www.bilibili.com/video/BV1GJ411x7h7",  # 公开 MV,长期稳定
]


def check(url: str) -> bool:
    try:
        info = parse_video(url)
    except Exception as exc:
        print("FAIL " + url)
        print("     解析异常: " + str(exc))
        return False
    title = str(info.get("title") or "").strip()
    if info.get("is_multi_part"):
        parts = info.get("parts") or []
        ok = bool(parts) and bool(title)
        detail = "多P " + str(len(parts)) + " 个分P, title=" + repr(title)
    else:
        formats = info.get("formats") or []
        ok = bool(title) and bool(formats)
        detail = ("title=" + repr(title) + ", 清晰度选项 " + str(len(formats))
                  + " 个, 最高 " + str(info.get("best_label") or "?"))
    print(("PASS " if ok else "FAIL ") + url)
    print("     " + detail)
    return ok


def main() -> int:
    urls = sys.argv[1:] or DEFAULT_URLS
    results = [check(url) for url in urls]
    passed = sum(1 for r in results if r)
    print("")
    print(str(passed) + "/" + str(len(results)) + " 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
