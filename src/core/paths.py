"""Runtime path helpers.

这个模块统一管理 fpbrowser2api 的运行目录，目的：

- 源码运行时：目录指向项目根目录，即包含 main.py/config/static/data 的目录。
- PyInstaller 打包后：目录指向可执行文件所在目录，方便用户把 config/data/static
  放在 exe / Linux 可执行文件旁边。

也可以通过环境变量 FPBROWSER2API_APP_ROOT 显式指定运行根目录，便于服务化部署。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller/Nuitka 等冻结后的可执行环境中。"""
    return bool(getattr(sys, "frozen", False))


def get_app_root() -> Path:
    """返回应用运行根目录。"""
    env_root = (os.environ.get("FPBROWSER2API_APP_ROOT") or "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()

    if is_frozen():
        return Path(sys.executable).resolve().parent

    # src/core/paths.py -> src/core -> src -> fpbrowser2api
    return Path(__file__).resolve().parents[2]


APP_ROOT = get_app_root()

CONFIG_DIR = APP_ROOT / "config"
DATA_DIR = APP_ROOT / "data"
STATIC_DIR = APP_ROOT / "static"
RULES_DIR = APP_ROOT / "rules"
LOGS_DIR = APP_ROOT / "logs"
ANALYZE_DIR = APP_ROOT / "analyze"

APP_LOG_FILE = APP_ROOT / "app.log"
MONITOR_LOG_FILE = APP_ROOT / "logs.txt"
PID_FILE = APP_ROOT / "fpbrowser2api.pid"


def ensure_runtime_dirs() -> None:
    """创建运行时常用可写目录。"""
    for path in (DATA_DIR, LOGS_DIR, ANALYZE_DIR):
        path.mkdir(parents=True, exist_ok=True)
