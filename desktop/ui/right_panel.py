# -*- coding: utf-8 -*-
"""
参数配置面板（右侧栏）
LLM 选择与密钥（内存保存不落地）、设备模式与 UDID、RAG 知识库上传/重建、
执行控制（开始/终止任务）与底部进度状态。
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from desktop.core.app_log import log_info
from desktop.core.config import runtime_config
from desktop.net.api_service import api_service

# 进度条状态标签
STATUS_IDLE = "空闲"
STATUS_FINISHED = "执行完成"
STATUS_FAILED = "失败"


class ConfigPanel(QWidget):
    """右侧参数配置面板。

    Signals:
        start_requested: (requirement: str) 点击"开始任务"。
        stop_requested: 点击"终止任务"。
        log_message: (level: str, message: str) 面板操作产生的日志。
    """

    start_requested = Signal(str)
    stop_requested = Signal()
    log_message = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化三组配置区与执行控制区。"""
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # ==================== LLM 配置 ====================
        group_llm = QGroupBox("LLM 配置")
        form_llm = QFormLayout(group_llm)
        self._combo_llm = self._make_combo()
        form_llm.addRow("模型:", self._combo_llm)
        self._edit_key = QLineEdit()
        self._edit_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_key.setPlaceholderText("sk-...（仅内存使用，不落地）")
        form_llm.addRow("API Key:", self._edit_key)
        hint = QLabel("密钥仅内存保存，不写入任何文件，退出即销毁")
        hint.setStyleSheet("color: #64748b; font-size: 11px;")
        form_llm.addRow("", hint)
        layout.addWidget(group_llm)

        # ==================== 设备配置 ====================
        group_dev = QGroupBox("设备环境")
        form_dev = QFormLayout(group_dev)
        self._radio_emulator = QRadioButton("安卓模拟器")
        self._radio_real = QRadioButton("USB 真机")
        self._radio_emulator.setChecked(True)
        group_mode = QWidget()
        mode_layout = QVBoxLayout(group_mode)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.addWidget(self._radio_emulator)
        mode_layout.addWidget(self._radio_real)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._radio_emulator)
        self._mode_group.addButton(self._radio_real)
        form_dev.addRow("模式:", group_mode)
        self._edit_udid = QLineEdit()
        self._edit_udid.setPlaceholderText("真机 UDID / 模拟器设备号（选填）")
        form_dev.addRow("设备 ID:", self._edit_udid)
        layout.addWidget(group_dev)

        # ==================== RAG 知识库 ====================
        group_rag = QGroupBox("RAG 知识库")
        layout_rag = QVBoxLayout(group_rag)
        btn_upload = QPushButton("上传测试知识文档（txt）")
        btn_upload.clicked.connect(self.upload_doc)
        btn_rebuild = QPushButton("重建向量库")
        btn_rebuild.clicked.connect(self.rebuild_kb)
        layout_rag.addWidget(btn_upload)
        layout_rag.addWidget(btn_rebuild)
        layout.addWidget(group_rag)

        # ==================== 执行控制 ====================
        group_run = QGroupBox("执行控制")
        layout_run = QVBoxLayout(group_run)
        self._btn_start = QPushButton("开始任务")
        self._btn_start.setStyleSheet(
            "QPushButton { background: #16a34a; color: white; padding: 8px; font-weight: bold; border-radius: 4px; }"
        )
        self._btn_start.clicked.connect(self._emit_start)
        self._btn_stop = QPushButton("终止任务")
        self._btn_stop.setEnabled(False)
        self._btn_stop.setStyleSheet(
            "QPushButton { background: #dc2626; color: white; padding: 8px; border-radius: 4px; }"
        )
        self._btn_stop.clicked.connect(self.stop_requested.emit)
        layout_run.addWidget(self._btn_start)
        layout_run.addWidget(self._btn_stop)
        layout.addWidget(group_run)
        layout.addStretch()

        # ==================== 底部进度与状态 ====================
        group_status = QGroupBox("任务状态")
        layout_status = QVBoxLayout(group_status)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._label_status = QLabel(f"状态: {STATUS_IDLE}")
        layout_status.addWidget(self._progress)
        layout_status.addWidget(self._label_status)
        layout.addWidget(group_status)

    # ==================== 控件工厂 ====================
    @staticmethod
    def _make_combo() -> "QComboBoxType":
        """构造 LLM 选择下拉框（通义千问 / DeepSeek）。

        Returns:
            配置完成的下拉框实例。
        """
        from PySide6.QtWidgets import QComboBox

        combo = QComboBox()
        combo.addItem("通义千问（qwen-turbo）", "qwen")
        combo.addItem("DeepSeek（deepseek-chat）", "deepseek")
        return combo

    # ==================== 行为方法 ====================
    def _emit_start(self) -> None:
        """收集右侧面板配置到 runtime_config 后发出开始任务信号。

        校验失败时弹窗提示；密钥仅写入内存配置对象。
        """
        from PySide6.QtWidgets import QMessageBox

        runtime_config.llm_provider = self._combo_llm.currentData()
        key = self._edit_key.text().strip()
        if runtime_config.llm_provider == "deepseek":
            runtime_config.deepseek_key = key
        else:
            runtime_config.qwen_key = key
        runtime_config.is_real_device = self._radio_real.isChecked()
        runtime_config.device_udid = self._edit_udid.text().strip()
        runtime_config.save_persisted()  # 仅持久化非敏感项

        from desktop.ui.dialogs import TaskDialog

        requirement = TaskDialog.ask_requirement(self)
        if not requirement:
            return
        log_info(
            f"配置已收集: provider={runtime_config.llm_provider}, "
            f"device={'真机' if runtime_config.is_real_device else '模拟器'}"
        )
        self.start_requested.emit(requirement)

    def upload_doc(self) -> None:
        """选择 txt 文档并异步上传入库。"""
        path, _ = QFileDialog.getOpenFileName(self, "选择知识文档", "", "文本文件 (*.txt)")
        if not path:
            return
        self.log_message.emit("INFO", f"开始上传知识文档: {path}")

        def on_ok(data: dict) -> None:
            self.log_message.emit(
                "INFO",
                f"知识文档入库成功: {data.get('file_name')}，"
                f"切分 {data.get('chunk_count')} 个片段",
            )
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                self, "上传成功", f"入库 {data.get('chunk_count')} 个文档片段"
            )

        def on_err(msg: str) -> None:
            self.log_message.emit("ERROR", f"知识文档上传失败: {msg}")
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(self, "上传失败", msg)

        api_service.upload_doc(path, on_ok, on_err)

    def rebuild_kb(self) -> None:
        """请求后端重建 RAG 向量库（重新入库 uploads 目录全部文档）。"""
        self.log_message.emit("INFO", "开始重建 RAG 向量库（可能需要数十秒）...")

        def on_ok(data: dict) -> None:
            self.log_message.emit("INFO", data.get("message", "重建完成"))
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(self, "重建完成", data.get("message", "重建完成"))

        def on_err(msg: str) -> None:
            self.log_message.emit("ERROR", f"向量库重建失败: {msg}")
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(self, "重建失败", msg)

        api_service.rebuild_kb(on_ok, on_err)

    # ==================== 状态控制 ====================
    def set_running(self, running: bool) -> None:
        """切换执行中状态：禁用开始按钮、启用终止按钮。

        Args:
            running: 是否处于任务执行中。
        """
        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        if running:
            self.set_status(0, "规划中")

    def set_status(self, percent: int, status_text: str) -> None:
        """更新进度条与状态文本。

        Args:
            percent: 进度百分比（0~100）。
            status_text: 状态描述（空闲/规划中/工具调用中/执行完成/失败）。
        """
        self._progress.setValue(max(0, min(100, percent)))
        self._label_status.setText(f"状态: {status_text}")

    def mark_finished(self, success: bool, defect_count: int = 0) -> None:
        """任务结束：更新进度条满格与终态文案。

        Args:
            success: 是否执行成功。
            defect_count: 识别的缺陷数量。
        """
        if success:
            self._progress.setValue(100)
            extra = f"（发现缺陷 {defect_count} 个）" if defect_count else ""
            self.set_status(100, STATUS_FINISHED + extra)
        else:
            self.set_status(self._progress.value(), STATUS_FAILED)
