# -*- coding: utf-8 -*-
"""
实时日志输出面板（中间栏）
QTextEdit 彩色日志渲染：按级别着色（INFO 蓝 / DEBUG 灰 / WARN 橙 / ERROR 红），
支持自动滚动开关与导出 txt。
"""

from datetime import datetime
from pathlib import Path
from typing import List

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# 各日志级别对应的显示颜色（RGB）
LEVEL_COLORS = {
    "INFO": "#2563eb",
    "DEBUG": "#6b7280",
    "WARNING": "#d97706",
    "ERROR": "#dc2626",
    "CRITICAL": "#991b1b",
}


class LogPanel(QWidget):
    """实时日志面板组件：彩色输出 + 自动滚动 + 导出 txt。

    Attributes:
        _text_edit: 日志显示控件（只读）。
        _auto_scroll: 自动滚动开关。
        _exported_lines: 已输出的日志行缓存（供导出）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化面板布局与控件。"""
        super().__init__(parent)
        self._exported_lines: List[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlaceholderText("等待日志输出...")
        self._text_edit.setStyleSheet(
            "QTextEdit { background: #0f172a; color: #e2e8f0; "
            "font-family: Consolas, 'Microsoft YaHei'; font-size: 12px; "
            "border: 1px solid #cbd5e1; border-radius: 4px; }"
        )
        layout.addWidget(self._text_edit)

        bottom_bar = QHBoxLayout()
        self._auto_scroll = QCheckBox("自动滚动")
        self._auto_scroll.setChecked(True)
        bottom_bar.addWidget(self._auto_scroll)
        bottom_bar.addStretch()

        from PySide6.QtWidgets import QPushButton

        btn_clear = QPushButton("清空日志")
        btn_clear.clicked.connect(self.clear)
        btn_export = QPushButton("导出日志 txt")
        btn_export.clicked.connect(self.export_txt)
        bottom_bar.addWidget(btn_clear)
        bottom_bar.addWidget(btn_export)
        layout.addLayout(bottom_bar)

    def append_log(self, level: str, message: str) -> None:
        """追加一条彩色日志（线程安全：调用方需在主线程）。

        Args:
            level: 日志级别（INFO/DEBUG/WARNING/ERROR）。
            message: 日志正文。
        """
        color = LEVEL_COLORS.get(level.upper(), "#e2e8f0")
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts} [{level}] {message}"
        self._exported_lines.append(line)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # 超长保护：超过 5000 行时移除最早的 1000 行
        if self._text_edit.document().blockCount() > 5000:
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.NextBlock,
                QTextCursor.MoveMode.KeepAnchor,
                1000,
            )
            cursor.removeSelectedText()
            cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(line + "\n", fmt)
        if self._auto_scroll.isChecked():
            self._text_edit.setTextCursor(cursor)
            self._text_edit.ensureCursorVisible()

    def append_lines(self, lines: List[str]) -> None:
        """批量追加日志行（用于后端日志批量拉取，按级别着色）。

        Args:
            lines: 形如 "HH:MM:SS [LEVEL] message" 的日志行列表。
        """
        for line in lines:
            level = "INFO"
            if "[" in line and "]" in line:
                try:
                    level = line.split("[")[1].split("]")[0]
                except IndexError:
                    pass
            self.append_log(level, line)

    def clear(self) -> None:
        """清空日志显示与导出缓存。"""
        self._text_edit.clear()
        self._exported_lines.clear()

    def export_txt(self) -> None:
        """导出全部日志为 txt 文件（QFileDialog 选择保存路径）。"""
        if not self._exported_lines:
            QMessageBox.information(self, "提示", "当前没有可导出的日志")
            return
        default_name = f"agent_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", default_name, "文本文件 (*.txt)"
        )
        if not path:
            return
        try:
            Path(path).write_text("\n".join(self._exported_lines), encoding="utf-8")
            QMessageBox.information(self, "成功", f"日志已导出到:\n{path}")
        except OSError as e:
            QMessageBox.critical(self, "导出失败", f"写入文件失败: {e}")
