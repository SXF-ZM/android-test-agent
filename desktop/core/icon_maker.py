# -*- coding: utf-8 -*-
"""
应用图标模块
使用 QPainter 纯代码绘制"安卓测试机器人"图标（PNG 用于窗口图标，
ICO 用于 PyInstaller 打包），无需外部图片资源；若资源目录已存在图标文件则直接加载。
"""

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap

# 图标资源目录（desktop/resources/generated）
RESOURCE_DIR: Path = Path(__file__).resolve().parent.parent / "resources" / "generated"
ICON_PNG_PATH: Path = RESOURCE_DIR / "app_icon.png"
ICON_ICO_PATH: Path = RESOURCE_DIR / "app_icon.ico"


def _draw_pixmap(size: int) -> QPixmap:
    """绘制指定尺寸的图标位图：绿色圆角机器人头像 + 手机轮廓。

    Args:
        size: 位图边长（像素）。

    Returns:
        QPixmap: 绘制完成的位图。
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 100.0  # 设计稿基准 100x100 缩放系数

    # 背景圆角矩形（深蓝渐变底色用纯色代替）
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#1e293b")))
    painter.drawRoundedRect(QRect(0, 0, size, size), int(18 * s), int(18 * s))

    # 机器人头部（安卓绿）
    android_green = QColor("#3ddc84")
    painter.setBrush(QBrush(android_green))
    painter.drawRoundedRect(QRect(int(22 * s), int(32 * s), int(56 * s), int(40 * s)), int(14 * s), int(14 * s))
    # 天线两根
    pen = QPen(android_green, max(2, int(4 * s)))
    painter.setPen(pen)
    painter.drawLine(QPoint(int(38 * s), int(34 * s)), QPoint(int(30 * s), int(20 * s)))
    painter.drawLine(QPoint(int(62 * s), int(34 * s)), QPoint(int(70 * s), int(20 * s)))
    # 眼睛（深色）
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#0f172a")))
    painter.drawEllipse(QRect(int(38 * s), int(46 * s), int(7 * s), int(7 * s)))
    painter.drawEllipse(QRect(int(55 * s), int(46 * s), int(7 * s), int(7 * s)))
    # 手机轮廓（白色描边圆角矩形，右下角）
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#e2e8f0"), max(2, int(5 * s))))
    painter.drawRoundedRect(QRect(int(58 * s), int(58 * s), int(30 * s), int(34 * s)), int(5 * s), int(5 * s))
    # 手机屏幕勾（测试通过标识）
    painter.setPen(QPen(android_green, max(2, int(5 * s))))
    painter.drawLine(QPoint(int(66 * s), int(76 * s)), QPoint(int(71 * s), int(82 * s)))
    painter.drawLine(QPoint(int(71 * s), int(82 * s)), QPoint(int(82 * s), int(66 * s)))

    painter.end()
    return pixmap


def ensure_icon_files() -> None:
    """生成图标文件到资源目录（幂等：已存在则跳过）。"""
    if ICON_PNG_PATH.exists() and ICON_ICO_PATH.exists():
        return
    try:
        RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
        _draw_pixmap(256).save(str(ICON_PNG_PATH), "PNG")
        # ICO 用于 exe 图标；Qt ICO 写入失败时保留 PNG 也能设置窗口图标
        _draw_pixmap(64).save(str(ICON_ICO_PATH), "ICO")
    except Exception:
        pass  # 图标生成失败不阻断启动


def get_app_icon() -> QIcon:
    """获取应用图标（优先资源文件，失败时内存绘制兜底）。

    Returns:
        QIcon: 应用窗口图标。
    """
    if ICON_PNG_PATH.exists():
        return QIcon(str(ICON_PNG_PATH))
    return QIcon(_draw_pixmap(128))


def get_ico_path() -> str:
    """获取 exe 打包用 ICO 路径（不存在时回退 PNG，PyInstaller 亦支持）。"""
    return str(ICON_ICO_PATH if ICON_ICO_PATH.exists() else ICON_PNG_PATH)
