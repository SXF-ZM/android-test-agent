# -*- coding: utf-8 -*-
"""
通用资源路径工具模块
解决 PyInstaller 打包后资源文件找不到的问题。

两种运行环境的路径规则：
- 开发环境（python main.py 直接运行）：
  * 只读资源根（templates 等）= 项目根目录
  * 可写数据根（.env / 模型缓存 / 向量库 / outputs）= 项目根目录
- 打包 exe 环境（PyInstaller 目录模式）：
  * 只读资源根 = sys._MEIPASS 临时解压目录（随 exe 分发的模板等）
  * 可写数据根 = exe 所在目录（.env、模型缓存、输出产物存放在 exe 同级，便于外置修改）

使用约定：
- 读取随包分发的只读资源（如 templates）→ resource_path()
- 读取/写入可变数据（如 .env、模型缓存、输出目录）→ data_path()
- 项目内禁止再使用硬编码相对路径定位上述资源
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """判断当前是否运行在 PyInstaller 打包的 exe 环境中。

    Returns:
        bool: PyInstaller 冻结环境返回 True；python 直接运行返回 False。
    """
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """获取可写数据根目录。

    打包环境返回 exe 所在目录（数据外置，改 .env 无需重新打包）；
    开发环境返回项目根目录（utils/ 的上一级）。

    Returns:
        Path: 可写数据根目录绝对路径。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(relative: str | Path) -> Path:
    """获取随包分发的只读资源路径（自动区分开发 / 打包环境）。

    打包环境基于 sys._MEIPASS 临时解压目录；开发环境基于项目根目录。

    Args:
        relative: 相对资源路径（如 "templates"、"templates/index.html"）。

    Returns:
        Path: 资源绝对路径（不保证存在，调用方按需校验）。
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", ""))
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative


def data_path(relative: str | Path) -> Path:
    """获取可写数据路径（.env / 模型缓存 / 向量库 / 输出目录等）。

    打包环境基于 exe 所在目录；开发环境基于项目根目录。

    Args:
        relative: 相对数据路径（如 ".env"、"models_cache"、"outputs/reports"）。

    Returns:
        Path: 数据绝对路径（不保证存在，调用方按需创建）。
    """
    return app_root() / relative


# 打包环境下初始化运行时环境变量：
# 1. HuggingFace 模型缓存指向 exe 同级目录，避免写入用户目录或临时目录
# 2. 关闭 chromadb 遥测上报，保证离线演示环境不产生网络依赖
if is_frozen():
    os.environ.setdefault("HF_HOME", str(data_path("models_cache") / "hf_home"))
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
