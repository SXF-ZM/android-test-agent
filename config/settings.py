# -*- coding: utf-8 -*-
"""
全局配置模块
使用 pydantic BaseSettings 统一管理所有配置项：
- 大模型（通义千问，OpenAI 兼容接口）
- Embedding 本地模型
- Chroma 向量数据库
- Appium 服务与安卓设备参数（模拟器 / USB 真机一键切换）
- 设备执行后端（Appium / mobile-mcp）与 MCP 连接参数
- 逐步 UI 驱动 Agent 开关与步数/时长限制（默认关闭）
- dsh-pet 桌宠单向播报开关与地址（默认关闭）
- ADB 与测试产物目录

配置项均可通过环境变量或项目根目录 .env 文件覆盖，
密钥等敏感信息统一放入 .env（已被 .gitignore 忽略），禁止硬编码提交仓库。
"""

import os
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


def _default_mcp_command() -> str:
    """返回启动 mobile-mcp 的命令名。

    Windows 上 npx 实际为 npx.cmd，直接传 "npx" 时部分 Python
    子进程实现无法定位；非 Windows 环境使用 npx。

    Returns:
        str: npx 命令名（Windows 为 npx.cmd）。
    """
    return "npx.cmd" if os.name == "nt" else "npx"


class Settings(BaseSettings):
    """全局配置类。

    所有字段均支持通过环境变量或 .env 文件覆盖（环境变量名不区分大小写）。
    设备模式由 IS_REAL_DEVICE 开关控制：
    - False：安卓模拟器模式，使用 EMULATOR_DEVICE_NAME；
    - True：USB 真机模式，使用 REAL_PHONE_UDID，capability 自动注入 udid。
    执行后端由 DEVICE_BACKEND 控制：
    - appium（默认）：使用 Appium + UiAutomator2；
    - mobile_mcp：使用 mobile-next/mobile-mcp（MCP）。
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
    # 单次 LLM 请求超时（秒）
    llm_request_timeout: int = 60
    # 网络/限流类错误的最大重试次数（不含首次调用）
    llm_max_retries: int = 2
    # 首次重试等待秒数，后续按 2 倍指数退避
    llm_retry_backoff_seconds: float = 1.5

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

    # ==================== 设备执行后端配置 ====================
    # 默认执行后端：appium（稳定默认） / mobile_mcp（MCP 实验后端）
    # 可通过 .env 的 DEVICE_BACKEND 切换，也可由前端按单次任务覆盖
    device_backend: str = "appium"

    # ---- mobile-mcp 配置（DEVICE_BACKEND=mobile_mcp 时生效）----
    # 传输方式：stdio（默认，本地 npx 子进程） / http（已启动的 MCP HTTP 服务）
    mobile_mcp_transport: str = "stdio"
    # stdio 模式启动命令（Windows 自动使用 npx.cmd）
    mobile_mcp_command: str = _default_mcp_command()
    # stdio 模式命令参数（固定为本项目已验证的 1.0.15，便于论文实验复现；
    # 如需体验新版本可改为 @latest，但需重新验证工具 schema）
    mobile_mcp_args: str = "-y @mobilenext/mobile-mcp@1.0.15"
    # http 模式服务地址（mobile-mcp --listen 3000 的默认端点）
    mobile_mcp_url: str = "http://127.0.0.1:3000/mcp"
    # mobile-mcp 设备标识；留空时自动使用当前模拟器/真机序列号（adb devices）
    mobile_mcp_device: str = ""
    # 首次启动等待超时（秒）：npx 首次下载依赖可能较慢，默认 90 秒
    mobile_mcp_startup_timeout: int = 90
    # 单次工具调用超时（秒）
    mobile_mcp_call_timeout: int = 60
    # 关闭会话等待超时（秒）
    mobile_mcp_close_timeout: int = 10
    # 关闭 mobile-mcp 会话后是否停止宿主 mobilecli daemon，避免其长期驻留
    # 并占用设备/端口；默认开启，可通过 MOBILE_MCP_CLEANUP_DAEMON=false 关闭
    mobile_mcp_cleanup_daemon: bool = True

    # ==================== dsh-pet 桌宠单向播报（可选，默认关闭） ====================
    # 安全说明：该可视化模块可一键关闭，适配涉密在研设备测试场景。
    # PET_ENABLED=false 时 utils/pet_broadcaster.py 完全空转：
    # 不创建后台线程、不建立连接、不发起任何 HTTP 请求。
    # 启用后也只推送简短纯文本状态，不传输 UI 树、截图、控件文字或密钥。
    PET_ENABLED: bool = False
    # dsh-pet 本地 HTTP 服务地址；默认使用其 /say 单向播报接口。
    # 若桌宠实际端口不同（例如 47835），在此覆盖；建议仅指向本机回环地址。
    PET_BASE_URL: str = "http://127.0.0.1:47831"

    # ==================== 逐步 UI 驱动 Agent（可选，默认关闭） ====================
    # 静态动作规划只依据测试用例文本推断 resource-id，遇到多邻国这类
    # 可点击容器无 resource-id 的动态页面时会全部点空。开启本开关后，
    # execute_test 改为“读取实时 UI 树 → LLM 选择下一步 → 执行 → 再观察”
    # 的逐步闭环，并支持按 ref / 文本 / 坐标点击。
    # 仅 Appium 后端支持；mobile_mcp 会自动回退到原静态动作模式。
    STEPWISE_AGENT_ENABLED: bool = False
    # 单次任务最多执行多少步（含 tap / input / back / sleep）
    STEPWISE_MAX_STEPS: int = 80
    # 单次任务最长执行时间（秒），超时后停止并判定目标未达成
    STEPWISE_MAX_SECONDS: int = 900
    # 每一步提供给 LLM 的最大 UI 节点数（优先保留可点击控件）
    STEPWISE_UI_MAX_NODES: int = 80
    # 每个节点文本/content-desc 的最大长度（超出截断）
    STEPWISE_UI_MAX_TEXT_LENGTH: int = 80
    # 每个动作执行后等待页面稳定的秒数
    STEPWISE_ACTION_WAIT_SECONDS: float = 1.0
    # done 成功时是否要求 evidence 文本真实出现在当前 UI 树中
    STEPWISE_DONE_REQUIRE_EVIDENCE: bool = True
    # 坐标点击是否允许落在当前 UI 树未解析到的位置。
    # 默认 False：坐标未命中任何已解析节点时拒绝执行，避免误触未识别控件；
    # 仅在 UI 树截断导致误判的场景下才建议临时开启。
    STEPWISE_ALLOW_UNTARGETED_TAP: bool = False

    # ==================== ADB 配置 ====================
    # adb 可执行文件路径（自动探测项目内 SDK，或回退系统 PATH）
    adb_path: str = _default_adb_path()

    # ==================== 测试产物目录 ====================
    # adb 日志保存目录
    log_output_dir: str = str(data_path(Path("outputs") / "logs"))
    # 截图保存目录
    screenshot_output_dir: str = str(data_path(Path("outputs") / "screenshots"))
    # 测试报告保存目录（未指定 run_id 时使用）
    report_output_dir: str = str(data_path(Path("outputs") / "reports"))
    # 单次任务产物根目录：outputs/runs/<run_id>/{logs,screenshots,reports}
    run_output_dir: str = str(data_path(Path("outputs") / "runs"))
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

    def validate_device_backend(self) -> str:
        """校验并规范 DEVICE_BACKEND 配置。

        Returns:
            str: "appium" 或 "mobile_mcp"；非法值回退为 appium。
        """
        backend = self.device_backend.strip().lower().replace("-", "_")
        if backend in ("mobile_mcp", "mobilemcp", "mcp"):
            return "mobile_mcp"
        return "appium"

    def validate_mobile_mcp_transport(self) -> str:
        """校验 mobile-mcp 传输方式。

        Returns:
            str: "stdio" 或 "http"；非法值回退为 stdio。
        """
        transport = self.mobile_mcp_transport.strip().lower()
        return transport if transport in ("stdio", "http") else "stdio"

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
            # 关键修复：App 已在后台运行时 UIA2 默认跳过启动（仅挂到后台实例），
            # 会话会停留在桌面界面上导致全部元素查找超时、手机毫无动静；
            # 强制每次会话启动 App 确保其一定被带到前台
            "appium:forceAppLaunch": True,
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
            self.run_output_dir,
            self.upload_dir,
            self.chroma_persist_dir,
            self.embedding_cache_dir,
        ]:
            Path(dir_path).mkdir(parents=True, exist_ok=True)


# 全局单例配置对象
settings = Settings()
settings.ensure_dirs()
