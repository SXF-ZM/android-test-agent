# -*- coding: utf-8 -*-
"""PySide6 桌面客户端打包脚本（PyInstaller 单目录模式，窗口程序无控制台）。"""

import shutil
from pathlib import Path

import PyInstaller.__main__

# 桌面客户端项目根目录（desktop/）
DESKTOP_ROOT: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = DESKTOP_ROOT.parent
DIST_DIR: Path = PROJECT_ROOT / "dist"
BUILD_DIR: Path = PROJECT_ROOT / "build"

ENTRY_SCRIPT: str = str(DESKTOP_ROOT / "main.py")
APP_NAME: str = "TestAgentDesktop"

# 桌面客户端隐式依赖（requests 由状态监听线程动态使用）
HIDDEN_IMPORTS: list = ["requests"]


def clean_cache() -> None:
    """清理旧打包缓存。"""
    for cache_dir in (BUILD_DIR / APP_NAME, DIST_DIR / APP_NAME):
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
            print(f"[clean] 已清理: {cache_dir}")


def build() -> None:
    """执行打包（单目录 + 无控制台 + 自定义图标）。"""
    from desktop.core.icon_maker import get_ico_path

    ensure_icon()
    args: list = [
        ENTRY_SCRIPT,
        f"--name={APP_NAME}",
        "--onedir",
        "--windowed",  # GUI 程序不显示控制台
        "--noconfirm",
        "--clean",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={DESKTOP_ROOT}",
        f"--paths={PROJECT_ROOT}",
        f"--icon={get_ico_path()}",
    ]
    for hidden in HIDDEN_IMPORTS:
        args.append(f"--hidden-import={hidden}")
    print("[build] 开始打包桌面客户端...")
    PyInstaller.__main__.run(args)
    print(f"[build] 打包完成: {DIST_DIR / APP_NAME / (APP_NAME + '.exe')}")
    print("提示：客户端 exe 需配合后端使用——目标机器需同时运行后端 test_agent.exe（或 python main.py）")


def ensure_icon() -> None:
    """确保图标文件已生成（在无 Qt 显示环境时跳过）。"""
    try:
        from desktop.core.icon_maker import ensure_icon_files

        ensure_icon_files()
    except Exception as e:
        print(f"[warn] 图标生成失败（跳过）: {e}")


if __name__ == "__main__":
    clean_cache()
    build()
