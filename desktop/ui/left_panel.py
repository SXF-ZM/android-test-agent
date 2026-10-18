# -*- coding: utf-8 -*-
"""
任务管理面板（左侧栏）
任务列表 + 新建任务 / 删除任务 / 查看历史任务。
任务数据保存在内存（原型系统），包含任务名称、需求文本、状态与报告路径。
"""

from dataclasses import dataclass, field
from typing import List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


@dataclass
class TaskItem:
    """单条任务数据。

    Attributes:
        task_id: 任务唯一 ID（递增）。
        name: 任务名称。
        requirement: 测试需求文本。
        status: 任务状态（待执行/执行中/执行完成/失败/已终止）。
        report_path: 测试报告本地路径（完成后填充）。
        defect_count: 缺陷数量（完成后填充）。
        error: 任务失败原因。
    """

    task_id: int
    name: str
    requirement: str
    status: str = "待执行"
    report_path: str = ""
    defect_count: int = 0
    error: str = ""

    def display_text(self) -> str:
        """生成列表显示文本（名称 + 状态 + 缺陷数）。

        Returns:
            str: 显示文本。
        """
        extra = f"（缺陷 {self.defect_count}）" if self.status == "执行完成" else ""
        return f"#{self.task_id} {self.name}  [{self.status}]{extra}"


class TaskPanel(QWidget):
    """左侧任务管理面板。

    Signals:
        current_task_changed: (task: Optional[TaskItem]) 选中任务变化。
        task_list_changed: 任务增删改后触发（用于刷新显示）。
    """

    current_task_changed = Signal(object)
    task_list_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化任务列表与操作按钮。"""
        super().__init__(parent)
        self._tasks: List[TaskItem] = []
        self._next_id = 1

        layout = QVBoxLayout(self)
        title = QLabel("任务管理")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title)

        self._list_widget = QListWidget()
        self._list_widget.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list_widget)

        btn_new = QPushButton("新建任务")
        btn_new.clicked.connect(self.create_task)
        btn_delete = QPushButton("删除任务")
        btn_delete.clicked.connect(self.delete_task)
        btn_history = QPushButton("查看历史任务")
        btn_history.clicked.connect(self.show_history)
        for btn in (btn_new, btn_delete, btn_history):
            layout.addWidget(btn)
        layout.addStretch()

    # ==================== 数据操作 ====================
    def create_task(self) -> None:
        """弹窗新建任务（输入任务名称与测试需求）。"""
        name, ok = QInputDialog.getText(self, "新建任务", "任务名称:")
        if not ok or not name.strip():
            return
        requirement, ok = QInputDialog.getMultiLineText(
            self, "新建任务", "测试需求描述:", "测试小米计算器的加法功能"
        )
        if not ok or not requirement.strip():
            return
        task = TaskItem(task_id=self._next_id, name=name.strip(), requirement=requirement.strip())
        self._next_id += 1
        self._tasks.append(task)
        self._refresh()
        self._list_widget.setCurrentRow(len(self._tasks) - 1)

    def delete_task(self) -> None:
        """删除当前选中的任务（执行中的任务禁止删除）。"""
        task = self.current_task()
        if not task:
            QMessageBox.information(self, "提示", "请先选中要删除的任务")
            return
        if task.status == "执行中":
            QMessageBox.warning(self, "无法删除", "任务正在执行中，请先终止任务")
            return
        confirm = QMessageBox.question(
            self, "确认删除", f"确定删除任务 #{task.task_id} {task.name} 吗？"
        )
        if confirm is QMessageBox.StandardButton.Yes:
            self._tasks.remove(task)
            self._refresh()

    def show_history(self) -> None:
        """弹窗展示全部已完成/失败的历史任务（含报告路径与失败原因）。"""
        finished = [t for t in self._tasks if t.status in ("执行完成", "失败", "已终止")]
        if not finished:
            QMessageBox.information(self, "历史任务", "暂无已完成的任务")
            return
        lines = [
            f"#{t.task_id} {t.name}  [{t.status}]  缺陷:{t.defect_count}\n  报告: {t.report_path or '无'}"
            + (f"\n  原因: {t.error}" if t.error else "")
            for t in finished
        ]
        QMessageBox.information(self, "历史任务（共 %d 条）" % len(finished), "\n\n".join(lines))

    def update_task(self, task: TaskItem) -> None:
        """更新任务状态并刷新列表显示。

        Args:
            task: 待更新的任务对象（原地修改后传入）。
        """
        self._refresh()

    # ==================== 内部方法 ====================
    def current_task(self) -> Optional[TaskItem]:
        """获取当前选中任务。

        Returns:
            Optional[TaskItem]: 选中任务；无选中时返回 None。
        """
        row = self._list_widget.currentRow()
        return self._tasks[row] if 0 <= row < len(self._tasks) else None

    def _refresh(self) -> None:
        """按内存任务数据重建列表显示。"""
        self._list_widget.blockSignals(True)
        self._list_widget.clear()
        for task in self._tasks:
            item = QListWidgetItem(task.display_text())
            # 状态着色：执行中橙、完成绿、失败红
            if task.status == "执行中":
                item.setForeground(Qt.GlobalColor.darkYellow)
            elif task.status == "执行完成":
                item.setForeground(Qt.GlobalColor.darkGreen)
            elif task.status in ("失败", "已终止"):
                item.setForeground(Qt.GlobalColor.red)
            self._list_widget.addItem(item)
        self._list_widget.blockSignals(False)
        self.task_list_changed.emit()

    def _on_row_changed(self, row: int) -> None:
        """列表选中行变化时发出任务切换信号。

        Args:
            row: 选中行号。
        """
        task = self._tasks[row] if 0 <= row < len(self._tasks) else None
        self.current_task_changed.emit(task)
