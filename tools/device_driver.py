# -*- coding: utf-8 -*-
"""
设备执行后端抽象模块
定义统一的 DeviceDriver 协议，屏蔽 Appium 与 mobile-mcp 的差异，
使 execute_test 节点只依赖抽象接口，不感知具体自动化框架。

设计目标：
- 默认后端保持 Appium，现有流程行为不变；
- 通过 DEVICE_BACKEND 或单次任务参数切换到 mobile_mcp；
- 新增后端需实现 connect / click_by_id / input_text / clear_text /
  restart_app / close 方法。
"""

from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

from config.settings import settings
from utils.logger import logger

# 动作执行结果字典类型别名
ActionResult = Dict[str, Any]

# 合法后端标识（内部统一使用下划线命名）
BACKEND_APPIUM = "appium"
BACKEND_MOBILE_MCP = "mobile_mcp"
SUPPORTED_BACKENDS = (BACKEND_APPIUM, BACKEND_MOBILE_MCP)


@runtime_checkable
class DeviceDriver(Protocol):
    """设备执行后端协议：AppiumTool 与 MobileMcpTool 均需满足。"""

    backend_name: str

    def connect(self) -> bool:
        """建立与执行框架/设备的会话，成功返回 True。"""
        ...

    def click_by_id(self, resource_id: str, timeout: int = 10) -> ActionResult:
        """按 resource-id 点击元素，返回 {success, message}。"""
        ...

    def input_text(self, resource_id: str, text: str, timeout: int = 10) -> ActionResult:
        """按 resource-id 向输入框写入文本，返回 {success, message}。"""
        ...

    def clear_text(self, resource_id: str, timeout: int = 10) -> ActionResult:
        """按 resource-id 清空输入框，返回 {success, message}。"""
        ...

    def restart_app(self) -> ActionResult:
        """重启本次被测应用，返回 {success, message}。"""
        ...

    def close(self) -> None:
        """释放会话与后台资源。"""
        ...


@runtime_checkable
class StepwiseDriver(Protocol):
    """逐步 UI 驱动模式所需的扩展协议（当前仅 AppiumTool 实现）。

    普通静态动作只需要 DeviceDriver；逐步模式额外读取实时 UI 树，
    并按坐标/文本/引用编号操作无 resource-id 的控件。
    """

    def get_page_source(self) -> str:
        """返回当前页面 XML。"""
        ...

    def get_window_size(self) -> Tuple[int, int]:
        """返回 (width, height) 屏幕尺寸。"""
        ...

    def tap_xy(self, x: int, y: int) -> ActionResult:
        """按屏幕坐标点击。"""
        ...

    def press_back(self) -> ActionResult:
        """发送系统返回键。"""
        ...

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
    ) -> ActionResult:
        """执行滑动。"""
        ...

    def input_text_focused(self, text: str) -> ActionResult:
        """向当前焦点输入框输入文本。"""
        ...


def supports_stepwise(driver: object) -> bool:
    """判断执行驱动是否支持逐步 UI 驱动模式。

    Args:
        driver: 已创建的执行驱动实例。

    Returns:
        bool: 实现了 StepwiseDriver 全部方法返回 True。
    """
    return isinstance(driver, StepwiseDriver)


def normalize_backend_name(value: Optional[str]) -> Optional[str]:
    """将用户输入的后端名称规范化为内部标识。

    Args:
        value: 原始名称（appium / mobile_mcp / mobile-mcp / mcp）。

    Returns:
        Optional[str]: 合法内部标识；无法识别时返回 None。
    """
    backend = (value or "").strip().lower().replace("-", "_")
    if backend in ("appium",):
        return BACKEND_APPIUM
    if backend in ("mobile_mcp", "mobilemcp", "mcp"):
        return BACKEND_MOBILE_MCP
    return None


def resolve_device_backend(override: Optional[str] = None) -> str:
    """解析本次任务生效的执行后端。

    优先级：单次任务覆盖值 > .env 的 DEVICE_BACKEND > appium 默认值。

    Args:
        override: 前端/接口传入的单次任务后端覆盖值。

    Returns:
        str: "appium" 或 "mobile_mcp"。
    """
    override_name = (override or "").strip()
    if override_name:
        normalized = normalize_backend_name(override_name)
        if normalized:
            return normalized
        logger.warning(
            f"未知执行后端: {override_name}，回退使用 .env 配置 DEVICE_BACKEND"
        )
    return settings.validate_device_backend()


def resolve_device_serial(
    is_real_device: Optional[bool] = None, udid: Optional[str] = None
) -> str:
    """解析本次任务的目标设备序列号（Appium 与 adb 共用）。

    Args:
        is_real_device: 设备模式覆盖值；None 表示使用 .env 配置。
        udid: 真机 UDID 覆盖值。

    Returns:
        str: adb -s 目标设备序列号，同时也是 mobile-mcp 默认设备标识。
    """
    if is_real_device is True:
        return (udid or "").strip() or settings.REAL_PHONE_UDID or settings.adb_device_serial
    if is_real_device is False:
        return settings.EMULATOR_DEVICE_NAME
    return settings.adb_device_serial


def create_device_driver(
    backend: Optional[str] = None,
    is_real_device: Optional[bool] = None,
    udid: Optional[str] = None,
    app_package: Optional[str] = None,
    app_activity: Optional[str] = None,
) -> DeviceDriver:
    """按后端标识创建执行驱动。

    Args:
        backend: 后端标识或用户输入（appium / mobile_mcp）。
        is_real_device: 设备模式覆盖值；None 表示使用 .env 配置。
        udid: 真机 UDID 覆盖值。
        app_package: 本次任务被测应用包名；None/空表示使用 .env 配置。
        app_activity: 本次任务启动 Activity；None/空表示使用 .env 配置。

    Returns:
        DeviceDriver: 满足统一协议的驱动实例。

    Raises:
        RuntimeError: 后端依赖未安装或创建失败。
    """
    effective_backend = resolve_device_backend(backend)
    if effective_backend == BACKEND_MOBILE_MCP:
        # 延迟导入：未安装 mcp 依赖时，Appium 模式仍可正常启动
        from tools.mobile_mcp_tool import MobileMcpTool

        return MobileMcpTool(
            is_real_device=is_real_device,
            udid=udid,
            app_package=app_package,
            app_activity=app_activity,
        )

    from tools.appium_tool import AppiumTool

    return AppiumTool(
        is_real_device=is_real_device,
        udid=udid,
        app_package=app_package,
        app_activity=app_activity,
    )
