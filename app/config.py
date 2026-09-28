"""运行期配置：路径、配色常量与通知参数。

所有路径默认落在仓库根目录下的 ``data/``，可用环境变量覆盖。
照片存本地目录，数据库为 SQLite，提醒由进程内后台任务处理。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GROOMING_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("GROOMING_DB_PATH", DATA_DIR / "grooming.db"))
PHOTO_DIR = Path(os.environ.get("GROOMING_PHOTO_DIR", DATA_DIR / "photos"))

# 演示系统统一会话密钥；生产环境应由环境变量注入随机值。
SECRET_KEY = os.environ.get("GROOMING_SECRET", "demo-secret-paw-care")

# 进程内通知任务参数：失败按退避重试，最多 5 次。
NOTIFY_INTERVAL_SECONDS = float(os.environ.get("GROOMING_NOTIFY_INTERVAL", "5"))
NOTIFY_MAX_ATTEMPTS = 5
NOTIFY_BACKOFF_BASE = 2  # 第 n 次重试前等待 base ** (n-1) 个调度间隔

# 预约开始超过该分钟数仍未取宠 => 超时未取事件。
OVERDUE_PICKUP_MINUTES = 180

# 工位容量（同时段在店宠物上限，跨美容师合计）。
DEFAULT_STATION_CAPACITY = 3

# 界面主题：柔和绿 / 海军蓝 / 桃色。
THEME = {
    "green": "#8fbf9f",
    "green_soft": "#e8f2ea",
    "green_deep": "#5d8a6e",
    "navy": "#2b3a55",
    "navy_soft": "#e4e8f0",
    "peach": "#f6c6a8",
    "peach_soft": "#fdeee4",
}


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
