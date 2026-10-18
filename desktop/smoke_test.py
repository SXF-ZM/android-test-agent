# -*- coding: utf-8 -*-
"""桌面客户端离屏烟雾测试：验证主窗口构建、日志渲染、任务面板与报告预览。"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false")

from PySide6.QtWidgets import QApplication

from desktop.core.icon_maker import ensure_icon_files, get_app_icon
from desktop.ui.center_panel import LogPanel
from desktop.ui.dialogs import ReportPreviewDialog
from desktop.ui.left_panel import TaskItem, TaskPanel
from desktop.ui.main_window import MainWindow
from desktop.ui.right_panel import ConfigPanel

app = QApplication([])

# 1. 主窗口三栏构建
window = MainWindow()
window.resize(1280, 760)
assert window.centralWidget() is not None
print("OK main_window_3panel")

# 2. 日志面板彩色渲染与导出缓存
panel = LogPanel()
panel.append_log("INFO", "INFO 日志行")
panel.append_log("ERROR", "ERROR 日志行")
panel.append_log("WARNING", "WARN 日志行")
assert panel._text_edit.toPlainText().count("[INFO]") == 1
assert panel._text_edit.toPlainText().count("[ERROR]") == 1
assert len(panel._exported_lines) == 3
print("OK log_panel_render")

# 3. 任务面板增删与历史
tasks = TaskPanel()
tasks._tasks.append(TaskItem(task_id=1, name="计算器加法测试", requirement="测试加法"))
tasks._refresh()
assert tasks._list_widget.count() == 1
assert "计算器加法测试" in tasks._list_widget.item(0).text()
print("OK task_panel")

# 4. 配置面板控件与状态切换
cfg = ConfigPanel()
cfg.set_running(True)
assert not cfg._btn_start.isEnabled() and cfg._btn_stop.isEnabled()
cfg.set_status(66, "工具调用中")
assert cfg._progress.value() == 66
cfg.mark_finished(True, 2)
assert cfg._label_status.text().find("执行完成") >= 0
print("OK config_panel_status")

# 5. 报告预览（Markdown 原生渲染）
report = Path("outputs/reports")
if report.exists() and any(report.glob("*.md")):
    md_path = str(sorted(report.glob("*.md"))[-1])
    dlg = ReportPreviewDialog(md_path)
    assert len(dlg._viewer.toPlainText()) > 10
    print("OK report_preview_markdown")
else:
    print("SKIP report_preview (no report)")

# 6. 图标
ensure_icon_files()
icon = get_app_icon()
assert not icon.isNull()
print(f"OK icon ({'png' if Path('desktop/resources/generated/app_icon.png').exists() else 'memory'})")

print("SMOKE_ALL_PASS")
