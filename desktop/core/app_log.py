# -*- coding: utf-8 -*-
"""
桌面客户端应用日志模块
Python logging 与 Qt 信号桥接：后台线程产生的日志通过 Qt 信号
安全地投递到 UI 线程的日志面板显示。
"""

import logging
from PySide6.QtCore import QObject, Signal


class QtLogSignalBridge(QObject):
    """日志信号桥：任何线程 emit，Qt 自动排队到主线程消费。

    Signals:
        log_received: (level: str, message: str) 单条日志。
    """

    log_received = Signal(str, str)


# 模块级单例
log_bridge = QtLogSignalBridge()


class SignalLogHandler(logging.Handler):
    """将 Python logging 记录转发到 Qt 信号的 Handler（线程安全）。"""

    def emit(self, record: logging.LogRecord) -> None:
        """logging 回调：格式化后经 Qt 信号发送到 UI 线程。

        Args:
            record: logging 标准日志记录。
        """
        try:
            message = record.getMessage()
            log_bridge.log_received.emit(record.levelname, message)
        except Exception:
            self.handleError(record)


# 客户端专用 logger（与后端 logger 分离，避免递归转发）
client_logger = logging.getLogger("desktop_client")
client_logger.setLevel(logging.INFO)
if not client_logger.handlers:
    client_logger.addHandler(SignalLogHandler())
    client_logger.propagate = False


def log_info(message: str) -> None:
    """记录 INFO 级客户端日志并转发到 UI。

    Args:
        message: 日志正文。
    """
    client_logger.info(message)


def log_error(message: str) -> None:
    """记录 ERROR 级客户端日志并转发到 UI。

    Args:
        message: 日志正文。
    """
    client_logger.error(message)
