# -*- coding: utf-8 -*-
"""
运行时配置管理模块（桌面客户端）
管理客户端与后端通信参数及本次会话的临时配置。

安全约束：
- LLM API 密钥仅保存在本进程内存中（RuntimeConfig 实例属性），
  绝不写入任何文件 / 注册表 / 环境变量，程序退出即销毁；
- 仅非敏感项（后端地址）允许持久化到用户配置目录，方便下次启动。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 非敏感配置持久化路径（不含任何密钥）
_CONFIG_FILE: Path = Path.home() / ".android_test_agent_desktop.json"


@dataclass
class RuntimeConfig:
    """客户端运行时配置（进程内存单例）。

    Attributes:
        api_base_url: 后端 FastAPI 服务地址。
        llm_provider: 本次任务选择的 LLM Provider（qwen / deepseek）。
        qwen_key: 通义千问临时密钥（仅内存，退出即销毁）。
        deepseek_key: DeepSeek 临时密钥（仅内存，退出即销毁）。
        is_real_device: 设备模式（False=模拟器 / True=USB 真机）。
        device_udid: 真机 UDID 或模拟器设备号。
    """

    api_base_url: str = "http://127.0.0.1:8000"
    llm_provider: str = "qwen"
    qwen_key: str = field(default="", repr=False)
    deepseek_key: str = field(default="", repr=False)
    is_real_device: bool = False
    device_udid: str = ""

    def load_persisted(self) -> None:
        """从用户目录加载非敏感持久化配置（不含密钥）。"""
        try:
            if _CONFIG_FILE.exists():
                data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
                self.api_base_url = data.get("api_base_url", self.api_base_url)
                self.llm_provider = data.get("llm_provider", self.llm_provider)
        except (json.JSONDecodeError, OSError):
            pass  # 配置损坏时使用默认值

    def save_persisted(self) -> None:
        """持久化非敏感配置到用户目录（仅 api_base_url 与 llm_provider）。"""
        try:
            _CONFIG_FILE.write_text(
                json.dumps(
                    {"api_base_url": self.api_base_url, "llm_provider": self.llm_provider},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # 写入失败不影响运行

    def active_key(self) -> str:
        """按当前 provider 获取生效密钥（仅内存读取）。

        Returns:
            str: 当前 provider 对应的密钥；未填写返回空字符串。
        """
        return self.deepseek_key if self.llm_provider == "deepseek" else self.qwen_key


# 模块级单例
runtime_config = RuntimeConfig()
