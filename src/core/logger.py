"""Logging utilities."""

from __future__ import annotations

import logging
from typing import Optional

from .config import config
from .paths import APP_LOG_FILE


def setup_logging(force_debug: Optional[bool] = None) -> None:
    """初始化全局日志配置（可重复调用，按最新 config 生效）。"""
    level = logging.DEBUG if (force_debug if force_debug is not None else config.debug_enabled) else logging.INFO

    handlers: list[logging.Handler] = []

    stream = logging.StreamHandler()
    stream.setLevel(level)
    handlers.append(stream)

    if config.log_to_file:
        # 日志直接写到 fpbrowser2api 根目录，避免在 src/ 或 data/ 下产生提交噪音
        APP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(APP_LOG_FILE), encoding="utf-8")
        file_handler.setLevel(level)
        handlers.append(file_handler)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
        force=True,  # 覆盖已有 basicConfig（py>=3.8）
    )


logger = logging.getLogger("fpbrowser2api")

