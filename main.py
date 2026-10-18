# -*- coding: utf-8 -*-
"""
项目入口模块
通过 uvicorn 启动 FastAPI 服务。

启动方式：
    cd test_agent
    python main.py

注意：uvicorn.run 必须直接传入 app 实例（而非 "api.server:app" 字符串），
字符串导入方式依赖运行时 sys.path 导入机制，PyInstaller 打包后会启动失败。

前置条件（重要）：
1. 需预先启动 Appium 服务：appium --address 127.0.0.1 --port 4723
2. 需预先启动安卓模拟器（或连接真机并开启 USB 调试）
3. 已在 exe/项目同目录 .env 中配置大模型 API Key
"""

import uvicorn

from api.server import app
from config.settings import settings
from utils.logger import logger


def main() -> None:
    """启动 FastAPI 服务。

    直接传入 app 实例启动，保证开发环境与 PyInstaller 打包环境均可运行。
    """
    service_url = f"http://127.0.0.1:{settings.api_port}"
    logger.info(f"智能测试 Agent 服务启动中: {service_url} （浏览器打开即可操作）")
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
