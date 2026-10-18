# -*- coding: utf-8 -*-
"""离屏诊断：复现 _on_start_task 流程，定位 POST 未发出的原因（临时诊断脚本）。"""
import os
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.net import api_service
from desktop.net.status_worker import StatusMonitor
from desktop.ui.main_window import MainWindow

calls = []


class FakeReply:
    def __init__(self) -> None:
        self.abort = lambda: None


def fake_run_test(requirement, on_ok, on_err, **kwargs):
    calls.append({"requirement": requirement, "kwargs": kwargs})
    print("[STUB] run_test 被调用 requirement=%r kwargs=%s" % (requirement[:30], kwargs))
    return FakeReply()


api_service.run_test = fake_run_test


from PySide6.QtCore import QObject, Signal as QSignal


class FakeMonitor(QObject):
    logs_received = QSignal(list)
    progress_received = QSignal(str)

    def __init__(self, *a, **k) -> None:
        QObject.__init__(self)

    def start(self) -> None:
        print("[STUB] StatusMonitor.start")

    def stop(self) -> None:
        pass


import desktop.ui.main_window as mw

mw.StatusMonitor = FakeMonitor

app = QApplication([])
win = MainWindow()
print("[DIAG] MainWindow 构造完成, current_task =", win._current_task)

try:
    win._on_start_task("诊断:测试计算器加法功能是否正常可用")
    print("[DIAG] _on_start_task 正常返回")
    print("[DIAG] run_test 调用次数:", len(calls))
    print("[DIAG] monitor:", win._monitor)
    print("[DIAG] run_reply:", win._run_reply)
except Exception:
    print("[DIAG] !!! _on_start_task 抛出异常:")
    traceback.print_exc()
