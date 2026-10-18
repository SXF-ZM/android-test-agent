# -*- coding: utf-8 -*-
"""
任务状态监听线程
后台 QThread 周期性轮询后端 /api/logs 增量日志，
解析 LangGraph 节点标记，向 UI 线程推送：日志文本、任务进度、任务状态。
"""

import time
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

# 节点序号 -> (状态文案, 进度百分比) 映射；日志中出现 "节点N" 标记即触发更新
NODE_STATUS_MAP = {
    1: ("RAG 检索知识库中", 16),
    2: ("生成测试用例中", 33),
    3: ("规划 Appium 动作中", 50),
    4: ("工具调用中（Appium 执行 + adb 采集）", 66),
    5: ("缺陷分析中", 83),
    6: ("生成测试报告中", 95),
}

# 轮询间隔（秒）
POLL_INTERVAL_SECONDS: float = 1.0


class StatusMonitor(QThread):
    """任务状态监听线程：运行期间持续拉取后端日志并解析节点进度。

    Signals:
        logs_received: (lines: List[str]) 批量新日志行（已格式化 "HH:MM:SS [LEVEL] msg"）。
        progress_received: (percent: int, status_text: str) 节点进度更新。
    """

    logs_received = Signal(list)
    progress_received = Signal(int, str)

    def __init__(self, api_base_url: str, parent: Optional[object] = None) -> None:
        """初始化监听线程。

        Args:
            api_base_url: 后端服务地址。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._base_url = api_base_url.rstrip("/")
        self._running = False
        self._last_seq = 0

    def run(self) -> None:
        """线程主循环：周期轮询 /api/logs，直到 stop() 被调用。"""
        import requests

        self._running = True
        while self._running:
            try:
                resp = requests.get(
                    f"{self._base_url}/api/logs",
                    params={"after_seq": self._last_seq},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    records = data.get("logs", [])
                    self._last_seq = data.get("last_seq", self._last_seq)
                    if records:
                        lines = [
                            f"[{r['ts']}] [{r['level']}] {r['message']}" for r in records
                        ]
                        self.logs_received.emit(lines)
                        # 解析节点标记：提取最高节点序号更新进度
                        max_node = 0
                        for r in records:
                            if "节点" in r["message"]:
                                for n in NODE_STATUS_MAP:
                                    if f"节点{n}" in r["message"] and n > max_node:
                                        max_node = n
                        if max_node:
                            status_text, percent = NODE_STATUS_MAP[max_node]
                            self.progress_received.emit(percent, status_text)
            except requests.RequestException:
                pass  # 网络波动静默重试，不打断监听
            # 分片睡眠以便快速响应 stop()
            for _ in range(10):
                if not self._running:
                    break
                time.sleep(POLL_INTERVAL_SECONDS / 10)

    def stop(self) -> None:
        """请求线程停止（线程安全标志位，循环内快速退出）。"""
        self._running = False
