# -*- coding: utf-8 -*-
"""
PyInstaller 打包脚本
将【面向安卓App的智能测试Agent】打包为 Windows 独立可执行程序（目录模式）。

使用方式（在项目根目录、项目虚拟环境下执行）：
    cd test_agent
    .\\.venv\\Scripts\\python.exe build_exe.py

打包策略说明：
- 单目录模式（onefile=False）：稳定性更高、启动快、降低杀毒误报，适合答辩演示；
- 保留控制台（console=True）：运行时可见日志，方便现场排查错误；
- templates 网页模板随包分发（--add-data），通过 utils.resource_path 定位；
- all-MiniLM-L6-v2 模型、chroma 向量库、outputs 输出目录【不打包进 exe】，
  首次运行 exe 时自动下载模型，数据均存放在 exe 同级目录；
- .env 配置文件不打包，打包完成后需手动复制到 dist/test_agent 目录。

打包完成后产物：
    dist/test_agent/test_agent.exe  （以及同目录运行时依赖文件）
"""

import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

# 项目根目录（build_exe.py 位于项目根）
PROJECT_ROOT: Path = Path(__file__).resolve().parent
# 打包产物输出目录
DIST_DIR: Path = PROJECT_ROOT / "dist"
# PyInstaller 工作缓存目录
BUILD_DIR: Path = PROJECT_ROOT / "build"
# 待打包的 Web 模板目录
TEMPLATES_DIR: Path = PROJECT_ROOT / "templates"

# 入口脚本与产物名称
ENTRY_SCRIPT: str = str(PROJECT_ROOT / "main.py")
APP_NAME: str = "test_agent"

# 隐式导入补齐：LangGraph/LangChain/Chroma/Embedding 等存在动态导入，
# PyInstaller 静态分析无法发现，需显式声明；uvicorn 子模块为运行时动态加载
HIDDEN_IMPORTS: list = [
    "langgraph",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_community",
    "langchain_huggingface",
    "langchain_chroma",
    "langchain_text_splitters",
    "sentence_transformers",
    "pydantic_settings",
    "uvicorn.logging",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
]

# 全量子模块收集：chromadb 存在大量动态导入（telemetry.posthog、migrations 等），
# PyInstaller 静态分析无法覆盖，缺失会在运行时报 No module named，需整体收集
COLLECT_SUBMODULES: list = ["chromadb"]

# 包元数据收集：appium/chromadb/langchain 等库在运行时通过 importlib.metadata
# 读取自身版本号，PyInstaller 默认不打包 dist-info 元数据，缺失会导致
# PackageNotFoundError 启动崩溃，需显式复制元数据
COPY_METADATA: list = [
    "Appium-Python-Client",
    "selenium",
    "chromadb",
    "langchain",
    "langchain-core",
    "langchain-openai",
    "langchain-community",
    "langchain-huggingface",
    "langchain-chroma",
    "langchain-text-splitters",
    "langgraph",
    "langgraph-checkpoint",
    "sentence-transformers",
    "transformers",
    "tokenizers",
    "huggingface-hub",
    "torch",
    "openai",
    "uvicorn",
    "fastapi",
    "pydantic",
    "pydantic-settings",
]


def clean_cache() -> None:
    """自动清理旧的 build、dist 缓存目录，保证产物干净。"""
    for cache_dir in (BUILD_DIR, DIST_DIR):
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
            print(f"[clean] 已清理缓存目录: {cache_dir}")


def build_exe() -> None:
    """执行 PyInstaller 打包（单目录模式 + 保留控制台）。"""
    if not TEMPLATES_DIR.exists():
        raise FileNotFoundError(f"模板目录不存在: {TEMPLATES_DIR}")

    args: list = [
        ENTRY_SCRIPT,
        f"--name={APP_NAME}",
        # 目录模式：产物为文件夹（含 exe），稳定性高、启动快
        "--onedir",
        # 保留控制台窗口，运行日志直接可见，方便答辩排查
        "--console",
        "--noconfirm",
        "--clean",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        f"--specpath={PROJECT_ROOT}",
        f"--paths={PROJECT_ROOT}",
        # Web 模板随包分发（Windows 分隔符为分号）
        f"--add-data={TEMPLATES_DIR}{os.pathsep}templates",
    ]
    for hidden_import in HIDDEN_IMPORTS:
        args.append(f"--hidden-import={hidden_import}")
    for sub_pkg in COLLECT_SUBMODULES:
        args.append(f"--collect-submodules={sub_pkg}")
    for metadata_pkg in COPY_METADATA:
        args.append(f"--copy-metadata={metadata_pkg}")

    print("[build] 开始 PyInstaller 打包（含 torch 等大依赖，预计需要较长时间）...")
    PyInstaller.__main__.run(args)
    print(f"[build] 打包完成，产物目录: {DIST_DIR / APP_NAME}")


def print_deploy_hint() -> None:
    """打包完成后输出部署提示：手动复制 .env 到产物目录。"""
    exe_path = DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
    env_target = DIST_DIR / APP_NAME / ".env"
    print("=" * 60)
    print("打包完成！部署步骤：")
    print(f"1. 产物位置: {exe_path}")
    print(f"2. 【必须手动复制】将项目根目录 .env 复制到: {env_target}")
    print("   （.env 存放大模型密钥与设备配置，不打包进 exe）")
    print("3. 双击 test_agent.exe 启动服务（需保留同目录其他依赖文件）")
    print("4. 浏览器打开 http://127.0.0.1:8000 即可操作")
    print("5. 首次运行会自动下载 all-MiniLM-L6-v2 模型（约 90MB，需联网一次）")
    print("6. 模型缓存/向量库/测试报告均存放在 exe 同级目录，不污染系统")
    print("=" * 60)


def main() -> None:
    """打包脚本入口：清理缓存 → 执行打包 → 输出部署提示。"""
    clean_cache()
    build_exe()
    print_deploy_hint()


if __name__ == "__main__":
    main()
