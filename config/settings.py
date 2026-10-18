# -*- coding: utf-8 -*-
"""
全局配置模块
使用 pydantic BaseSettings 统一管理所有配置项：
- 大模型（通义千问，OpenAI 兼容接口）
- Embedding 本地模型
- Chroma 向量数据库
- Appium 服务与安卓设备参数（模拟器 / USB 真机一键切换）
- ADB 与测试产物目录

配置项均可通过环境变量或项目根目录 .env 文件覆盖，
密钥等敏感信息统一放入 .env（已被 .gitignore 忽略），禁止硬编码提交仓库。
"""

from pathlib import Path
from typing import Any, Dict

from pydantic_settings import BaseSettings, SettingsConfigDict

from utils.resource_path import app_root, data_path

# 项目根目录（开发环境 = test_agent/；打包环境 = exe 所在目录，可写数据根）
BASE_DIR = app_root()


def _default_adb_path() -> str:
    """探测 adb 可执行文件默认路径。

    优先使用项目同级 android-sdk 目录中的 adb（本地便携化安装），
    不存在时回退为系统 PATH 中的 adb 命令。

    Returns:
        str: adb 可执行文件的绝对路径或命令名。
    """
    candidate = BASE_DIR.parent / "android-sdk" / "platform-tools" / "adb.exe"
    if candidate.exists():
        return str(candidate)
    return "adb"


class Settings(BaseSettings):
    """全局配置类。

    所有字段均支持通过环境变量或 .env 文件覆盖（环境变量名不区分大小写）。
    设备模式由 IS_REAL_DEVICE 开关控制：
    - False：安卓模拟器模式，使用 EMULATOR_DEVICE_NAME；
    - True：USB 真机模式，使用 REAL_PHONE_UDID，capability 自动注入 udid。
    """

    model_config = SettingsConfigDict(
        # .env 位于可写数据根目录（打包后为 exe 同级目录，外置便于改配置）
        env_file=str(data_path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ==================== 设备模式开关 ====================
    # False = 安卓模拟器模式（默认）；True = USB 真机模式
    IS_REAL_DEVICE: bool = False
    # 模拟器设备名（adb devices 中的序列号，如 emulator-5554）
    EMULATOR_DEVICE_NAME: str = "emulator-5554"
    # 真机 UDID（通过 adb devices 查看；真机模式下必填）
    REAL_PHONE_UDID: str = ""

    # ==================== 大模型配置（LLM Provider 一键切换，均为 OpenAI 兼容接口） ====================
    # 大模型提供方：qwen（通义千问）/ deepseek（DeepSeek），通过 .env 的 LLM_PROVIDER 切换
    llm_provider: str = "qwen"

    # ---- 通义千问（DashScope OpenAI 兼容模式）----
    # 通义千问 DashScope 提供的 OpenAI 兼容模式地址
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # API Key 从 .env 读取，不在代码中硬编码
    llm_api_key: str = ""
    llm_model_name: str = "qwen-turbo"

    # ---- DeepSeek（OpenAI 兼容模式）----
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    deepseek_model_name: str = "deepseek-chat"

    # 采样参数（两种 Provider 通用）
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096

    # ==================== Embedding 模型配置（本地 sentence-transformers） ====================
    embedding_model_name: str = "all-MiniLM-L6-v2"
    # 本地模型缓存目录（可写数据目录，打包后位于 exe 同级，首次运行自动下载）
    embedding_cache_dir: str = str(data_path("models_cache"))

    # ==================== Chroma 向量数据库配置 ====================
    chroma_persist_dir: str = str(data_path("chroma_db"))
    chroma_collection_name: str = "app_test_knowledge"
    # RAG 检索返回的相关文档片段数量
    rag_top_k: int = 3
    # 文档切分参数
    chunk_size: int = 500
    chunk_overlap: int = 50

    # ==================== Appium 配置 ====================
    # 注意：运行前需预先启动 Appium 服务（默认 http://127.0.0.1:4723）
    appium_server_url: str = "http://127.0.0.1:4723"
    # 安卓平台名称与版本（platformVersion 仅模拟器模式使用，真机由 udid 定位）
    android_platform_name: str = "Android"
    android_platform_version: str = "11.0"
    # 被测 App 包名与启动 Activity（默认使用模拟器自带的系统设置作为演示被测应用）
    app_package: str = "com.android.settings"
    app_activity: str = ".Settings"
    # 等待启动超时时间（毫秒）
    new_command_timeout: int = 300
    # 隐式等待时间（秒）
    implicit_wait: int = 10

    # ==================== ADB 配置 ====================
    # adb 可执行文件路径（自动探测项目内 SDK，或回退系统 PATH）
    adb_path: str = _default_adb_path()

    # ==================== 测试产物目录 ====================
    # adb 日志保存目录
    log_output_dir: str = str(data_path(Path("outputs") / "logs"))
    # 截图保存目录
    screenshot_output_dir: str = str(data_path(Path("outputs") / "screenshots"))
    # 测试报告保存目录
    report_output_dir: str = str(data_path(Path("outputs") / "reports"))
    # 上传的需求文档保存目录
    upload_dir: str = str(data_path("uploads"))

    # ==================== FastAPI 服务配置 ====================
    # 演示环境绑定本机回环地址，避免触发防火墙授权弹窗
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # ==================== LLM Provider 动态属性 ====================
    @property
    def active_llm_base_url(self) -> str:
        """根据 LLM_PROVIDER 返回当前生效的 LLM 服务地址。

        Returns:
            str: qwen 返回 DashScope 兼容地址；deepseek 返回 DeepSeek 地址；
            未知 provider 回退为 qwen 地址。
        """
        if self.llm_provider == "deepseek":
            return self.deepseek_base_url
        return self.llm_base_url

    @property
    def active_llm_api_key(self) -> str:
        """根据 LLM_PROVIDER 返回当前生效的 API Key。

        Returns:
            str: 当前 provider 对应的 API Key（从 .env 读取，不落日志）。
        """
        if self.llm_provider == "deepseek":
            return self.deepseek_api_key
        return self.llm_api_key

    @property
    def active_llm_model_name(self) -> str:
        """根据 LLM_PROVIDER 返回当前生效的模型名称。

        Returns:
            str: qwen 返回 llm_model_name；deepseek 返回 deepseek_model_name。
        """
        if self.llm_provider == "deepseek":
            return self.deepseek_model_name
        return self.llm_model_name

    def validate_llm_provider(self) -> str:
        """校验并规范 LLM_PROVIDER 配置。

        Returns:
            str: 规范化后的小写 provider（"qwen" 或 "deepseek"）；
            非法取值时回退为 "qwen" 并记录说明。
        """
        provider = self.llm_provider.strip().lower()
        if provider not in ("qwen", "deepseek"):
            # 非法配置回退为 qwen，保证系统可用（此处不引入 logger，避免循环导入）
            return "qwen"
        return provider

    # ==================== 设备相关动态属性 ====================
    @property
    def device_name(self) -> str:
        """根据设备模式返回当前目标设备名称。

        Returns:
            str: 真机模式返回 UDID；模拟器模式返回模拟器设备名；
            真机模式未配置 UDID 时返回提示占位符。
        """
        if self.IS_REAL_DEVICE:
            return self.REAL_PHONE_UDID or "real-device-udid-not-configured"
        return self.EMULATOR_DEVICE_NAME

    @property
    def adb_device_serial(self) -> str:
        """根据设备模式返回 adb -s 目标设备序列号。

        Returns:
            str: adb 设备序列号（真机为 UDID，模拟器为模拟器名）。
        """
        return self.device_name

    @property
    def ANDROID_CAP(self) -> Dict[str, Any]:
        """动态生成 Appium Desired Capabilities（UIAutomator2）。

        真机模式自动注入 udid 参数定位设备；模拟器模式注入 platformVersion。
        供 AppiumTool.connect() 通过 UiAutomator2Options.load_capabilities 加载。

        Returns:
            Dict[str, Any]: Appium capability 键值对（平台参数带 appium: 前缀）。
        """
        caps: Dict[str, Any] = {
            "platformName": self.android_platform_name,
            "appium:automationName": "UiAutomator2",
            "appium:deviceName": self.device_name,
            "appium:appPackage": self.app_package,
            "appium:appActivity": self.app_activity,
            "appium:newCommandTimeout": self.new_command_timeout,
            # 不重置 App 数据，加快会话启动速度
            "appium:noReset": True,
        }
        if self.IS_REAL_DEVICE:
            # 真机模式：udid 为必填定位参数
            caps["appium:udid"] = self.REAL_PHONE_UDID
        else:
            # 模拟器模式：按系统镜像版本等待启动
            caps["appium:platformVersion"] = self.android_platform_version
        return caps

    def ensure_dirs(self) -> None:
        """确保所有输出目录存在，不存在则自动创建。"""
        for dir_path in [
            self.log_output_dir,
            self.screenshot_output_dir,
            self.report_output_dir,
            self.upload_dir,
            self.chroma_persist_dir,
            self.embedding_cache_dir,
        ]:
            Path(dir_path).mkdir(parents=True, exist_ok=True)


# 全局单例配置对象
settings = Settings()
settings.ensure_dirs()
