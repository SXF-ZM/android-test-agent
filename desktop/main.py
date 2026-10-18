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
import traceback
from datetime import datetime
from pathlib import Path


def _install_excepthook() -> None:
    """安装全局异常钩子：未捕获异常写入 exe 同级 crash.log。

    背景：windowed 打包模式无控制台，未捕获异常默认静默丢失，
    导致界面无响应时无法定位原因；落盘日志是最后防线。
    """
    def _hook(exc_type, exc_value, exc_tb) -> None:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        try:  # 打包环境下定位到 exe 同级目录
            base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
            log_dir = base.parent / "logs" if getattr(sys, "frozen", False) else log_dir
        except Exception:
            pass
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(log_dir / f"client_crash_{stamp}.log", "w", encoding="utf-8") as f:
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass
        sys.__stderr__ and traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def main() -> int:
    """创建 QApplication 与主窗口并进入事件循环。

    Returns:
        int: 应用退出码。
    """
    from PySide6.QtWidgets import QApplication

    from desktop.core.icon_maker import ensure_icon_files
    from desktop.ui.main_window import MainWindow

    _install_excepthook()

    app = QApplication(sys.argv)
    app.setApplicationName("安卓App智能测试Agent系统")
    app.setStyle("Fusion")

    ensure_icon_files()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
