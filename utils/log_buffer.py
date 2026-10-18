# -*- coding: utf-8 -*-
"""
日志环形缓冲模块
为桌面客户端提供后端实时日志拉取能力：将运行日志按序号缓存于内存环形列表，
客户端通过 /api/logs?after_seq=N 增量拉取新日志。

仅内存使用，服务重启后清空，不写入任何文件。
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List

# 缓冲上限（条），超出后自动淘汰最旧日志
BUFFER_MAX_SIZE: int = 2000


@dataclass
class LogRecord:
    """单条缓冲日志记录。

    Attributes:
        seq: 全局递增序号（客户端据此增量拉取）。
        ts: 时间戳字符串。
        level: 日志级别（INFO/WARNING/ERROR/DEBUG）。
        message: 日志正文。
    """

    seq: int
    ts: str
    level: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        """序列化为 JSON 可传输字典。

        Returns:
            Dict[str, str]: 含 seq/ts/level/message 的字典。
        """
        return {"seq": self.seq, "ts": self.ts, "level": self.level, "message": self.message}


class RingLogHandler(logging.Handler):
    """日志环形缓冲 Handler：将日志记录写入线程安全的内存环形队列。"""

    def __init__(self, capacity: int = BUFFER_MAX_SIZE) -> None:
        """初始化缓冲 Handler。

        Args:
            capacity: 环形队列容量（条数）。
        """
        super().__init__()
        self._buffer: Deque[LogRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        """logging 框架回调：写入一条日志到缓冲。

        Args:
            record: logging 标准日志记录。
        """
        try:
            message = self.format(record) if self.formatter else record.getMessage()
            with self._lock:
                self._seq += 1
                self._buffer.append(
                    LogRecord(
                        seq=self._seq,
                        ts=time.strftime("%H:%M:%S", time.localtime(record.created)),
                        level=record.levelname,
                        message=message,
                    )
                )
        except Exception:
            # 日志缓冲失败不影响主流程
            self.handleError(record)

    def get_after(self, after_seq: int = 0) -> List[LogRecord]:
        """获取序号大于 after_seq 的全部日志记录（增量拉取）。

        Args:
            after_seq: 客户端已持有的最大序号。

        Returns:
            List[LogRecord]: 新增日志记录列表（按序号升序）。
        """
        with self._lock:
            return [r for r in self._buffer if r.seq > after_seq]

    def last_seq(self) -> int:
        """查询当前缓冲中的最大日志序号。

        Returns:
            int: 最大序号；缓冲为空时返回 0。
        """
        with self._lock:
            return self._seq


# 模块级单例：供 server.py 挂载到 logger，并供 /api/logs 端点读取
ring_log_handler = RingLogHandler()


def attach_to_logger(logger: logging.Logger) -> None:
    """将环形缓冲 Handler 挂载到指定 logger（幂等，重复挂载自动跳过）。

    Args:
        logger: 项目统一 logger。
    """
    if ring_log_handler not in logger.handlers:
        logger.addHandler(ring_log_handler)
