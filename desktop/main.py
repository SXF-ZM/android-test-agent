# -*- coding: utf-8 -*-
"""
安卓App智能测试Agent系统 —— PySide6 桌面客户端入口
对接本地 FastAPI 后端（默认 http://127.0.0.1:8000）。

启动方式（项目虚拟环境）：
    cd test_agent
    .\\.venv\\Scripts\\python -m desktop.main

前置条件：后端服务已启动（python main.py），Appium 与模拟器/真机按需启动。
"""

import sys


def main() -> int:
    """创建 QApplication 与主窗口并进入事件循环。

    Returns:
        int: 应用退出码。
    """
    from PySide6.QtWidgets import QApplication

    from desktop.core.icon_maker import ensure_icon_files
    from desktop.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("安卓App智能测试Agent系统")
    app.setStyle("Fusion")

    ensure_icon_files()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
