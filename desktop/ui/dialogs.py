# -*- coding: utf-8 -*-
"""
通用对话框模块
任务需求输入、Markdown 报告预览（QTextDocument 原生渲染，无 WebView）、
分类异常弹窗（网络失败 / Appium 连接失败 / 大模型调用异常）。
"""

from pathlib import Path

from PySide6.QtGui import QFont, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class TaskDialog:
    """任务需求输入对话框（静态方法封装）。"""

    @staticmethod
    def ask_requirement(parent: QWidget) -> str:
        """弹窗输入测试需求文本（多行，5~2000 字符校验）。

        Args:
            parent: 父窗口。

        Returns:
            str: 校验通过的需求文本；取消或非法时返回空字符串。
        """
        text, ok = QInputDialog.getMultiLineText(
            parent, "测试需求", "请输入 App 测试需求描述（5~2000 字符）:"
        )
        if not ok:
            return ""
        text = text.strip()
        if len(text) < 5:
            QMessageBox.warning(parent, "输入有误", "需求描述至少 5 个字符")
            return ""
        if len(text) > 2000:
            QMessageBox.warning(parent, "输入有误", "需求描述不能超过 2000 字符")
            return ""
        return text


class ReportPreviewDialog(QDialog):
    """Markdown 测试报告预览对话框（QTextDocument.setMarkdown 原生渲染）。"""

    def __init__(self, report_path: str, parent: QWidget | None = None) -> None:
        """加载并渲染 Markdown 报告。

        Args:
            report_path: 报告本地路径（客户端与后端同机部署）。
            parent: 父窗口。
        """
        super().__init__(parent)
        self.setWindowTitle("测试报告预览")
        self.resize(760, 620)
        self._report_path = report_path

        layout = QVBoxLayout(self)
        self._viewer = QTextEdit()
        self._viewer.setReadOnly(True)
        self._viewer.setFont(QFont("Microsoft YaHei", 10))
        layout.addWidget(self._viewer)

        btn_bar = QHBoxLayout()
        btn_export = QPushButton("导出 Markdown 到本地")
        btn_export.clicked.connect(self.export_markdown)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_bar.addStretch()
        btn_bar.addWidget(btn_export)
        btn_bar.addWidget(btn_close)
        layout.addLayout(btn_bar)

        self._load_report()

    def _load_report(self) -> None:
        """读取报告文件并以 Markdown 渲染（失败显示错误占位）。"""
        try:
            content = Path(self._report_path).read_text(encoding="utf-8")
            document = QTextDocument()
            document.setMarkdown(content)
            self._viewer.setDocument(document)
        except OSError as e:
            self._viewer.setPlainText(f"报告读取失败: {e}\n路径: {self._report_path}")

    def export_markdown(self) -> None:
        """导出报告 Markdown 文件到用户选择的本地方案。"""
        from PySide6.QtWidgets import QFileDialog

        default_name = Path(self._report_path).name or "test_report.md"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出报告", default_name, "Markdown 文件 (*.md)"
        )
        if not path:
            return
        try:
            content = Path(self._report_path).read_text(encoding="utf-8")
            Path(path).write_text(content, encoding="utf-8")
            QMessageBox.information(self, "导出成功", f"报告已导出到:\n{path}")
        except OSError as e:
            QMessageBox.critical(self, "导出失败", f"写入文件失败: {e}")


def show_error_popup(parent: QWidget, message: str) -> None:
    """按错误内容分类弹窗提示（网络失败 / Appium 失败 / 大模型异常 / 其他）。

    Args:
        parent: 父窗口。
        message: 原始错误描述。
    """
    if "Appium" in message or "模拟器" in message or "真机" in message:
        title = "Appium 连接失败"
        detail = (
            "无法连接 Appium 服务或设备。\n"
            "请检查：\n1. Appium 服务是否已启动（appium --port 4723）\n"
            "2. 安卓模拟器是否启动 / 真机是否连接并开启 USB 调试"
        )
    elif "LLM" in message or "大模型" in message or "API" in message or "401" in message:
        title = "大模型调用异常"
        detail = (
            "LLM 调用失败。\n请检查右侧面板填写的 API Key 是否有效，"
            "以及网络能否访问模型服务商。"
        )
    else:
        title = "任务执行异常"
        detail = "任务执行过程中发生错误，请查看日志面板详情。"
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(f"{detail}\n\n详细信息: {message}")
    box.exec()
