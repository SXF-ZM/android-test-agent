# -*- coding: utf-8 -*-
"""
主窗口模块（QMainWindow）
三分布局：左侧任务管理 | 中间实时日志 | 右侧参数配置。
编排任务执行全流程：开始任务 → 状态监听线程 → 结果回调 → 报告预览。
"""

from typing import Optional

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QWidget,
)
from PySide6.QtCore import Qt, QTimer

from desktop.core.app_log import log_bridge, log_error, log_info
from desktop.core.config import runtime_config
from desktop.core.icon_maker import get_app_icon
from desktop.net.api_service import api_service
from desktop.net.status_worker import StatusMonitor
from desktop.ui.center_panel import LogPanel
from desktop.ui.dialogs import ReportPreviewDialog, show_error_popup
from desktop.ui.left_panel import TaskItem, TaskPanel
from desktop.ui.right_panel import ConfigPanel

# 后端离线时健康检查自动重试间隔（毫秒）
RECONNECT_INTERVAL_MS: int = 5000


class MainWindow(QMainWindow):
    """应用主窗口：三栏布局 + 任务全流程编排。"""

    def __init__(self) -> None:
        """初始化布局、信号连接与后端连通性检查。"""
        super().__init__()
        runtime_config.load_persisted()
        self.setWindowTitle("安卓App智能测试Agent系统")
        self.resize(1280, 760)
        self.setWindowIcon(get_app_icon())

        self._monitor: Optional[StatusMonitor] = None
        self._run_reply = None
        self._current_task: Optional[TaskItem] = None

        self._build_menu()
        self._build_layout()
        self._connect_signals()
        self._check_backend()

    # ==================== 布局构建 ====================
    def _build_menu(self) -> None:
        """构建顶部菜单栏（后端地址设置、报告预览、关于）。"""
        menu = self.menuBar().addMenu("设置(&S)")

        act_backend = QAction("后端地址...", self)
        act_backend.triggered.connect(self._change_backend_url)
        menu.addAction(act_backend)

        act_report = QAction("预览最近测试报告", self)
        act_report.triggered.connect(self._preview_last_report)
        menu.addAction(act_report)

        act_about = QAction("关于", self)
        act_about.triggered.connect(
            lambda: QMessageBox.about(
                self,
                "关于",
                "安卓App智能测试Agent系统（PySide6 桌面客户端）\n"
                "LangGraph + RAG + Appium + ADB\n本科毕业设计原型系统",
            )
        )
        menu.addAction(act_about)

    def _build_layout(self) -> None:
        """构建三栏分割布局。"""
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._task_panel = TaskPanel()
        self._log_panel = LogPanel()
        self._config_panel = ConfigPanel()

        splitter.addWidget(self._panel_wrapper(self._task_panel, "任务管理"))
        splitter.addWidget(self._panel_wrapper(self._log_panel, "实时日志"))
        splitter.addWidget(self._panel_wrapper(self._config_panel, "参数配置"))
        splitter.setSizes([280, 620, 320])
        self.setCentralWidget(splitter)

    @staticmethod
    def _panel_wrapper(inner: QWidget, title: str) -> QDockWidget:
        """将面板包装为带标题的 Dock 容器。

        Args:
            inner: 内嵌面板。
            title: 栏目标题。

        Returns:
            QDockWidget: 带标题的容器。
        """
        dock = QDockWidget(title)
        dock.setWidget(inner)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        return dock

    # ==================== 信号编排 ====================
    def _connect_signals(self) -> None:
        """连接各面板信号与状态监听信号。"""
        self._config_panel.start_requested.connect(self._on_start_task)
        self._config_panel.stop_requested.connect(self._on_stop_task)
        self._config_panel.log_message.connect(self._log_panel.append_log)
        self._task_panel.current_task_changed.connect(self._on_task_selected)

        # 客户端本地日志 → 日志面板
        log_bridge.log_received.connect(self._log_panel.append_log)

    # ==================== 后端连通性 ====================
    def _check_backend(self) -> None:
        """启动时检查后端健康状态，离线则定时重试。"""

        def on_ok(data: dict) -> None:
            log_info(f"后端服务连接成功: {runtime_config.api_base_url}")
            self._config_panel.log_message.emit("INFO", "后端服务连接成功")

        def on_err(msg: str) -> None:
            log_error(f"后端服务连接失败: {msg}")
            self._config_panel.log_message.emit("ERROR", f"后端连接失败: {msg}")
            QTimer.singleShot(RECONNECT_INTERVAL_MS, self._check_backend)

        api_service.health(on_ok, on_err)

    def _change_backend_url(self) -> None:
        """弹窗修改后端地址并重新检查连通性。"""
        from PySide6.QtWidgets import QInputDialog

        url, ok = QInputDialog.getText(
            self, "后端地址", "FastAPI 服务地址:", text=runtime_config.api_base_url
        )
        if ok and url.strip():
            runtime_config.api_base_url = url.strip()
            runtime_config.save_persisted()
            self._check_backend()

    # ==================== 任务流程 ====================
    def _on_task_selected(self, task: Optional[TaskItem]) -> None:
        """左侧任务选中变化：记录当前任务。

        Args:
            task: 选中任务对象。
        """
        self._current_task = task

    def _on_start_task(self, requirement: str) -> None:
        """开始任务：异步提交 /run_test 并启动状态监听线程。

        Args:
            requirement: 测试需求文本。
        """
        if self._monitor is not None:
            QMessageBox.warning(self, "任务执行中", "已有任务在执行，请等待完成或先终止")
            return
        if self._current_task is None:
            task = self._task_panel.auto_create_task(requirement)
            self._current_task = task
            log_info(f"未选中任务，已自动创建任务 #{task.task_id}: {task.name}")

        task = self._current_task
        if task.status == "执行中":
            QMessageBox.warning(self, "任务执行中", "当前任务已在执行")
            return
        task.status = "执行中"
        self._task_panel.update_task(task)
        self._config_panel.set_running(True)
        log_info(f"任务 #{task.task_id} {task.name} 提交执行")

        # 启动状态监听线程（轮询后端日志 → 进度条/状态文本）
        self._monitor = StatusMonitor(runtime_config.api_base_url)
        self._monitor.logs_received.connect(self._log_panel.append_lines)
        self._monitor.progress_received.connect(self._config_panel.set_status)
        self._monitor.start()

        # 异步提交任务（不阻塞 UI；reply 持有引用供终止）
        self._run_reply = api_service.run_test(
            requirement,
            lambda data: self._on_task_finished(task, data),
            lambda msg: self._on_task_failed(task, msg),
        )

    def _on_task_finished(self, task: TaskItem, data: dict) -> None:
        """任务成功返回：更新任务状态、进度条并询问是否预览报告。

        Args:
            task: 当前任务对象。
            data: /run_test 响应体（message/report_path/defects/error）。
        """
        self._cleanup_monitor()
        error = data.get("error", "")
        defects = data.get("defects", []) or []
        task.report_path = data.get("report_path", "")
        task.defect_count = data.get("defect_count", 0)

        if error:
            task.status = "失败"
            task.error = error
            self._config_panel.mark_finished(False)
            show_error_popup(self, error)
        else:
            task.status = "执行完成"
            self._config_panel.mark_finished(True, task.defect_count)
        self._task_panel.update_task(task)
        log_info(f"任务 #{task.task_id} 执行结束: {task.status}")

        # 任务完成自动弹出报告预览
        if task.report_path:
            self._open_report(task.report_path)

    def _on_task_failed(self, task: TaskItem, message: str) -> None:
        """任务请求失败（网络异常 / 服务不可达）：更新状态并弹窗。

        Args:
            task: 当前任务对象。
            message: 错误描述。
        """
        self._cleanup_monitor()
        task.status = "失败"
        task.error = message
        self._task_panel.update_task(task)
        self._config_panel.mark_finished(False)
        show_error_popup(self, message)

    def _on_stop_task(self) -> None:
        """终止任务：中止 HTTP 请求、停止监听线程、任务标记已终止。

        说明：后端 /run_test 为同步接口，无法优雅取消图执行；
        原型系统的终止语义为客户端主动断开（后端继续执行完本轮但结果不再展示）。
        """
        if self._run_reply is not None:
            self._run_reply.abort()
            self._run_reply = None
        self._cleanup_monitor()
        task = self._current_task
        if task and task.status == "执行中":
            task.status = "已终止"
            task.error = "用户手动终止"
            self._task_panel.update_task(task)
        self._config_panel.mark_finished(False)
        log_info("任务已被用户终止")

    def _cleanup_monitor(self) -> None:
        """停止并回收状态监听线程，复位执行按钮。"""
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor.wait(3000)
            self._monitor = None
        self._config_panel.set_running(False)

    # ==================== 报告 ====================
    def _open_report(self, report_path: str) -> None:
        """打开 Markdown 报告预览对话框。

        Args:
            report_path: 报告本地路径。
        """
        dialog = ReportPreviewDialog(report_path, self)
        dialog.exec()

    def _preview_last_report(self) -> None:
        """菜单入口：预览最近一次任务的报告（读取本地文件）。"""
        task = self._current_task
        if task and task.report_path:
            self._open_report(task.report_path)
            return
        QMessageBox.information(
            self, "提示", "请先执行完成一个测试任务，或在左侧选中已完成任务"
        )
