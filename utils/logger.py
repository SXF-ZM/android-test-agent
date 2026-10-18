# -*- coding: utf-8 -*-
"""
统一日志工具模块
基于 logging 提供全局统一格式的 logger，
同时输出到控制台与日志文件，便于测试链路问题排查。
"""

import logging
import sys
from pathlib import Path

from config.settings import settings


def _build_logger() -> logging.Logger:
    """构建并返回全局 logger 实例（单例）"""
    logger_instance = logging.getLogger("app_test_agent")
    if logger_instance.handlers:
        # 避免重复添加 handler
        return logger_instance

    logger_instance.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger_instance.addHandler(console_handler)

    # 文件输出（保存到 outputs/logs 目录）
    log_file = Path(settings.log_output_dir) / "agent_runtime.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger_instance.addHandler(file_handler)

    return logger_instance


# 全局统一 logger
logger = _build_logger()
