"""路径与配置。所有模块统一从这里取路径,避免硬编码。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

COOKIE_FILE = DATA_DIR / "cookies.txt"          # 供 cookies.txt 上传 & Edge 导出写入
SESSION_COOKIE_FILE = DATA_DIR / "sess.json"     # 扫码登录拿到的 SESSDATA 缓存
TASKS_FILE = DATA_DIR / "tasks.json"             # 下载任务历史持久化
UP_LIST_FILE = DATA_DIR / "up_list.json"         # A3: 自定义 UP 清单 + 增量记录

# 默认下载目录,可在运行时改(本工具不改写用户家目录其他位置)
DOWNLOAD_DIR = Path.home() / "Downloads" / "bilibili"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PORT = 5050   # 避开 macOS ControlCenter 占用的 5000 端口
BROWSER_COOKIE_SOURCE = "edge"   # 平时用 Edge 登 B 站
