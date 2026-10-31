# -*- coding: utf-8 -*-
"""
Appium 工具模块
封装 Appium WebDriver 连接与安卓 UI 自动化操作：
- 建立 / 关闭与 Appium Server 的会话（capability 由 settings.ANDROID_CAP 动态生成）
- 通过 resource-id 点击元素
- 通过 resource-id 输入文本

异常捕获细化区分：会话创建失败、Appium 服务连接失败、元素不存在、
等待超时、元素状态异常、协议错误等，日志统一携带 traceback 上下文。
所有会话调用方须在 try-finally 中调用 close()，确保会话一定释放，
防止设备会话被占用。

前置条件（重要）：
1. 需预先启动 Appium 服务：appium --address 127.0.0.1 --port 4723
2. 需预先启动安卓模拟器，或连接真机并开启 USB 调试
"""

import re
import time
from typing import Any, Dict, Optional, Tuple

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    InvalidElementStateException,
    InvalidSessionIdException,
    NoSuchElementException,
    SessionNotCreatedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config.settings import settings
from tools import adb_tool
from tools.device_driver import resolve_device_serial
from utils.logger import logger
from utils.redaction import (
    is_password_resource_id,
    mask_text,
    redact_text_in_message,
)
from utils.ui_tree import danger_reason_for_element

# 操作结果字典类型别名
ActionResult = Dict[str, Any]


class AppiumTool:
    """Appium 安卓自动化操作封装类（实现 DeviceDriver 协议）。

    Attributes:
        backend_name: 固定为 "appium"。
        driver: Appium Remote WebDriver 会话实例；未连接时为 None。
        is_real_device: 设备模式覆盖值（None 表示使用 settings 配置）。
        udid: 真机 UDID 覆盖值（None 表示使用 settings 配置）。
        app_package: 本次任务被测应用包名（空表示使用 settings 配置）。
        app_activity: 本次任务启动 Activity（空表示使用 settings 配置）。
    """

    backend_name = "appium"

    def __init__(
        self,
        is_real_device: Optional[bool] = None,
        udid: Optional[str] = None,
        app_package: Optional[str] = None,
        app_activity: Optional[str] = None,
    ) -> None:
        """初始化工具实例，driver 置空等待 connect() 建立会话。

        Args:
            is_real_device: 设备模式覆盖值；None 时读取 settings.IS_REAL_DEVICE。
            udid: 真机 UDID 覆盖值；None 时读取 settings 对应设备配置。
            app_package: 本次任务被测应用包名；空时读取 settings.app_package。
            app_activity: 本次任务启动 Activity；空时读取 settings.app_activity。
        """
        self.driver: Optional[webdriver.Remote] = None
        self.is_real_device: Optional[bool] = is_real_device
        self.udid: Optional[str] = udid
        self.app_package: str = (app_package or "").strip() or settings.app_package
        self.app_activity: str = (app_activity or "").strip() or settings.app_activity

    def _build_capabilities(self) -> Dict[str, Any]:
        """构建本次会话的 UiAutomator2 capability 参数。

        以 settings.ANDROID_CAP 为底，应用本次任务的设备与被测 App 覆盖值。

        Returns:
            Dict[str, Any]: capability 参数字典。
        """
        # 以 settings.ANDROID_CAP 为底，按本次任务的设备/应用覆盖值调整
        caps = dict(settings.ANDROID_CAP)
        # 单次任务可以指定被测应用，避免全局 APP_PACKAGE 残留导致跑错 App
        caps["appium:appPackage"] = self.app_package
        caps["appium:appActivity"] = self.app_activity
        use_real = self.is_real_device if self.is_real_device is not None else settings.IS_REAL_DEVICE
        if use_real:
            device_udid = self.udid or settings.REAL_PHONE_UDID
            caps["appium:udid"] = device_udid
            # 覆盖模式下 deviceName 也必须跟随本次目标设备，
            # 否则 .env 配置为模拟器时会残留 emulator-5554
            if device_udid:
                caps["appium:deviceName"] = device_udid
            caps.pop("appium:platformVersion", None)
        else:
            caps.pop("appium:udid", None)
            # 同理，.env 为真机模式时需把 deviceName 改回模拟器序列号
            caps["appium:deviceName"] = settings.EMULATOR_DEVICE_NAME
            caps["appium:platformVersion"] = settings.android_platform_version
        return caps

    def _device_label(self) -> str:
        """生成本次会话的设备描述（用于日志）。

        Returns:
            str: 设备模式与设备名描述文本。
        """
        use_real = self.is_real_device if self.is_real_device is not None else settings.IS_REAL_DEVICE
        mode = "真机" if use_real else "模拟器"
        if use_real:
            name = self.udid or settings.REAL_PHONE_UDID or "(未配置UDID)"
        else:
            name = settings.EMULATOR_DEVICE_NAME
        return f"{mode}, 设备: {name}"

    def describe(self) -> str:
        """返回本次会话的设备描述（供统一后端日志使用）。"""
        return f"appium, {self._device_label()}"

    def connect(self) -> bool:
        """建立与 Appium Server 的连接，启动被测 App 会话。

        capability 默认由 settings.ANDROID_CAP 动态生成（自动区分真机 / 模拟器），
        存在前端覆盖参数时按覆盖值构建。连接失败时自动清理残留 driver，
        防止半初始化会话占用设备。

        Returns:
            bool: 连接成功返回 True，失败返回 False。
        """
        try:
            # mobile-mcp 与 Appium 共用 UiAutomation；切回 Appium 前清理其残留
            # DeviceServer，避免新会话报 “UiAutomationService already registered”。
            serial = resolve_device_serial(self.is_real_device, self.udid)
            adb_tool.stop_mobile_mcp_device_server(device_serial=serial)

            options = UiAutomator2Options()
            options.load_capabilities(self._build_capabilities())

            logger.info(
                f"正在连接 Appium 服务: {settings.appium_server_url}, "
                f"设备模式: {self._device_label()}, "
                f"被测应用: {self.app_package}/{self.app_activity}"
            )
            self.driver = webdriver.Remote(settings.appium_server_url, options=options)
            self.driver.implicitly_wait(settings.implicit_wait)
            logger.info("Appium 会话建立成功")
            return True
        except SessionNotCreatedException as e:
            logger.error(
                f"Appium 会话创建失败（请检查被测应用包名/Activity、"
                f"UiAutomator2 驱动与设备是否就绪）: {e}",
                exc_info=True,
            )
            self.driver = None
            return False
        except (ConnectionError, OSError) as e:
            logger.error(
                f"无法连接 Appium 服务 {settings.appium_server_url}，"
                f"请确认服务已启动: {e}",
                exc_info=True,
            )
            self.driver = None
            return False
        except WebDriverException as e:
            logger.error(
                f"Appium 连接协议异常，请确认 Appium 服务与设备已启动: {e}",
                exc_info=True,
            )
            self.driver = None
            return False
        except Exception as e:
            logger.error(f"Appium 连接出现未知异常: {e}", exc_info=True)
            self.driver = None
            return False

    def _ensure_session(self) -> bool:
        """检查当前是否存在有效的 Appium 会话。

        Returns:
            bool: 会话可用返回 True；未连接返回 False（已记录警告日志）。
        """
        if self.driver is None:
            logger.warning("Appium 未连接，请先调用 connect() 建立会话")
            return False
        return True

    # 开屏广告跳过按钮的文本：兼容“跳过”“跳过 5”“跳过广告”“Skip 3”等
    _LAUNCH_AD_SKIP_PATTERN = re.compile(
        r"^(?:跳过(?:广告)?|skip(?:\s*ad)?)\s*\d*$", re.IGNORECASE
    )

    @classmethod
    def _find_skip_button(cls, source: str):
        """从页面源码中查找开屏广告的跳过按钮。

        Args:
            source: Appium 返回的当前页面 XML 源码。

        Returns:
            Optional[Tuple[str, int, int]]: (按钮文本, 中心 x, 中心 y)；
            未找到时返回 None。
        """
        for tag in re.findall(r"<[^>]+>", source):
            text_match = re.search(r'\btext="([^"]*)"', tag)
            if not text_match:
                continue
            text = text_match.group(1).strip()
            if not cls._LAUNCH_AD_SKIP_PATTERN.match(text):
                continue
            rect_match = re.search(
                r'\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag
            )
            if not rect_match:
                continue
            x1, y1, x2, y2 = (int(value) for value in rect_match.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            return text, (x1 + x2) // 2, (y1 + y2) // 2
        return None

    def _dismiss_launch_ad(self, timeout: float = 6.0) -> bool:
        """尝试跳过冷启动开屏广告，避免广告层拦截后续点击。

        部分第三方 App（如哔哩哔哩）冷启动时会展示全屏开屏广告：首页元素
        仍在无障碍树中，但触摸事件会被广告层消费，表现为“点击成功”却没有
        发生跳转。这里从页面源码中定位“跳过/Skip”按钮并点击，最多点击两次，
        然后等待按钮消失，避免广告残留时重复点击误触其他控件。

        Args:
            timeout: 等待广告消失的最大秒数。

        Returns:
            bool: 检测并点击过跳过按钮返回 True；未检测到广告返回 False。
        """
        if self.driver is None:
            return False
        serial = resolve_device_serial(self.is_real_device, self.udid)
        deadline = time.time() + timeout
        dismissed = False
        clicks = 0
        while time.time() < deadline:
            try:
                source = self.driver.page_source
            except WebDriverException as e:
                logger.debug(f"读取页面源码失败，跳过开屏广告检测: {e}")
                return dismissed
            target = self._find_skip_button(source)
            if target is None:
                return dismissed
            # 跳过按钮最多点两次；广告已消失但按钮文本尚未刷新时，
            # 继续按原坐标点击可能误触首页控件
            if clicks >= 2:
                return dismissed
            text, x, y = target
            if not adb_tool.tap(x, y, device_serial=serial):
                return dismissed
            clicks += 1
            dismissed = True
            logger.info(
                f"检测到启动广告，已点击跳过按钮: {text}（ADB 坐标 {x},{y}）"
            )
            # 广告退场动画期间触摸事件仍会被消费，等待页面稳定后再继续
            time.sleep(2.0)
        return dismissed

    def _element_safety_error(self, element: Any, action: str) -> Optional[str]:
        """检查元素是否命中密码字段或购买/订阅/支付/分享等敏感控件。

        静态模式没有逐步模式的 UI 树，只能在定位到元素后读取属性做二次
        校验。读取属性失败时放行（保持原有行为），由动作执行本身报错。

        Args:
            element: Appium WebElement。
            action: 动作类型（input_text 对密码字段更严格）。

        Returns:
            Optional[str]: 拒绝原因；安全时返回 None。
        """
        try:
            resource_id = str(element.get_attribute("resource-id") or "")
            text = str(element.get_attribute("text") or "")
            content_desc = str(element.get_attribute("content-desc") or "")
            class_name = str(element.get_attribute("class") or "")
            password = str(element.get_attribute("password") or "").strip().lower() == "true"
        except Exception as e:
            logger.debug(f"读取元素属性失败，跳过静态安全校验: {e}")
            return None

        if action == "input_text" and (
            password or is_password_resource_id(resource_id)
        ):
            return "已拒绝向密码字段输入内容"
        reason = danger_reason_for_element(
            text, content_desc, resource_id, class_name
        )
        if reason:
            return f"已拒绝敏感操作: {reason}"
        return None

    def click_by_id(self, resource_id: str, timeout: int = 10) -> ActionResult:
        """通过 resource-id 定位元素并点击。

        异常分类处理：元素不存在 / 等待超时 / 元素失效 / 不可交互 /
        协议错误分别记录不同级别与上下文的日志。

        Args:
            resource_id: 元素的 resource-id（如 com.miui.calculator:id/btn_5）。
            timeout: 显式等待元素出现的超时时间（秒）。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            logger.info(f"执行点击操作，resource-id: {resource_id}")
            # 冷启动广告会消费触摸事件，先跳过广告再定位目标元素
            self._dismiss_launch_ad()
            locate_started = time.monotonic()
            element = WebDriverWait(self.driver, timeout).until(
                EC.visibility_of_element_located((AppiumBy.ID, resource_id))
            )
            # 元素加载期间可能才弹出广告；广告关闭后页面会重绘，
            # 需要重新定位元素以获取最新坐标
            if time.monotonic() - locate_started > 1.0:
                if self._dismiss_launch_ad():
                    element = self.driver.find_element(AppiumBy.ID, resource_id)
            safety_error = self._element_safety_error(element, "click_by_id")
            if safety_error:
                logger.warning(f"{safety_error}: {resource_id}")
                return {"success": False, "message": safety_error}
            # 部分第三方 App（如 B 站）的自定义控件不响应 UiAutomator2
            # 无障碍点击，但系统级触摸注入有效，因此优先按元素中心坐标 ADB 点击。
            rect = element.rect or {}
            x = int(rect.get("x", 0) + rect.get("width", 0) / 2)
            y = int(rect.get("y", 0) + rect.get("height", 0) / 2)
            serial = resolve_device_serial(self.is_real_device, self.udid)
            if x > 0 and y > 0 and adb_tool.tap(x, y, device_serial=serial):
                logger.info(f"点击元素成功: {resource_id}（ADB 坐标 {x},{y}）")
                return {
                    "success": True,
                    "message": f"点击元素 {resource_id} 成功",
                }
            # ADB 不可用时回退到 Appium 原生点击
            element.click()
            logger.info(f"点击元素成功: {resource_id}（Appium click）")
            return {"success": True, "message": f"点击元素 {resource_id} 成功"}
        except NoSuchElementException as e:
            msg = f"元素不存在: {resource_id}（请检查 resource-id 是否正确）"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except TimeoutException as e:
            msg = f"等待元素超时（{timeout}s）: {resource_id}"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except StaleElementReferenceException as e:
            msg = f"元素引用已失效（页面刷新）: {resource_id}"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except InvalidElementStateException as e:
            msg = f"元素当前状态不可交互: {resource_id}"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except InvalidSessionIdException as e:
            msg = f"Appium 会话已失效，需重新 connect(): {resource_id}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}
        except WebDriverException as e:
            msg = f"点击元素协议异常: {resource_id}, 错误: {e}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = f"点击元素未知异常: {resource_id}, 错误: {e}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}

    def input_text(self, resource_id: str, text: str, timeout: int = 10) -> ActionResult:
        """通过 resource-id 定位输入框，清空后输入文本。

        异常分类处理逻辑与 click_by_id 一致。

        Args:
            resource_id: 输入框元素的 resource-id。
            text: 待输入的文本内容。
            timeout: 显式等待元素出现的超时时间（秒）。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            logger.info(
                f"执行输入操作，resource-id: {resource_id}, 内容: {mask_text(text) or '(空)'}"
            )
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((AppiumBy.ID, resource_id))
            )
            safety_error = self._element_safety_error(element, "input_text")
            if safety_error:
                logger.warning(f"{safety_error}: {resource_id}")
                return {"success": False, "message": safety_error}
            element.clear()
            element.send_keys(text)
            logger.info(f"输入文本成功: {resource_id}")
            return {
                "success": True,
                "message": f"向 {resource_id} 输入成功（{len(text)} 个字符）",
            }
        except NoSuchElementException as e:
            msg = f"输入框不存在: {resource_id}（请检查 resource-id 是否正确）"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except TimeoutException as e:
            msg = f"等待输入框超时（{timeout}s）: {resource_id}"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except StaleElementReferenceException as e:
            msg = f"输入框引用已失效（页面刷新）: {resource_id}"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except InvalidElementStateException as e:
            msg = f"输入框当前状态不可编辑: {resource_id}"
            logger.warning(msg, exc_info=True)
            return {"success": False, "message": msg}
        except InvalidSessionIdException as e:
            msg = f"Appium 会话已失效，需重新 connect(): {resource_id}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}
        except WebDriverException as e:
            msg = redact_text_in_message(
                f"输入文本协议异常: {resource_id}, 错误: {e}", text
            )
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = redact_text_in_message(
                f"输入文本未知异常: {resource_id}, 错误: {e}", text
            )
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}

    def clear_text(self, resource_id: str, timeout: int = 10) -> ActionResult:
        """通过 resource-id 定位输入框并清空文本。

        Args:
            resource_id: 输入框元素的 resource-id。
            timeout: 显式等待元素出现的超时时间（秒）。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            logger.info(f"执行清空操作，resource-id: {resource_id}")
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((AppiumBy.ID, resource_id))
            )
            safety_error = self._element_safety_error(element, "clear_text")
            if safety_error:
                logger.warning(f"{safety_error}: {resource_id}")
                return {"success": False, "message": safety_error}
            element.clear()
            logger.info(f"清空输入框成功: {resource_id}")
            return {"success": True, "message": f"已清空 {resource_id}"}
        except NoSuchElementException:
            msg = f"输入框不存在: {resource_id}（请检查 resource-id 是否正确）"
            logger.warning(msg)
            return {"success": False, "message": msg}
        except TimeoutException:
            msg = f"等待输入框超时（{timeout}s）: {resource_id}"
            logger.warning(msg)
            return {"success": False, "message": msg}
        except (StaleElementReferenceException, InvalidElementStateException) as e:
            msg = f"输入框当前不可清空: {resource_id}, 错误: {e}"
            logger.warning(msg)
            return {"success": False, "message": msg}
        except InvalidSessionIdException as e:
            msg = f"Appium 会话已失效，需重新 connect(): {resource_id}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}
        except WebDriverException as e:
            msg = f"清空输入框协议异常: {resource_id}, 错误: {e}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = f"清空输入框未知异常: {resource_id}, 错误: {e}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}

    def get_page_source(self) -> str:
        """读取当前屏幕的 UI 树 XML（逐步 UI 驱动模式使用）。

        Returns:
            str: Appium page_source；未连接或读取失败时返回空字符串。
        """
        if self.driver is None:
            return ""
        try:
            return self.driver.page_source or ""
        except WebDriverException as e:
            logger.warning(f"读取页面 UI 树失败: {e}")
            return ""
        except Exception as e:
            logger.warning(f"读取页面 UI 树出现未知异常: {e}")
            return ""

    def get_window_size(self) -> Tuple[int, int]:
        """读取当前屏幕尺寸（像素）。

        Returns:
            Tuple[int, int]: (width, height)；读取失败时返回 (0, 0)。
        """
        if self.driver is None:
            return 0, 0
        try:
            size = self.driver.get_window_size() or {}
            return int(size.get("width", 0) or 0), int(size.get("height", 0) or 0)
        except Exception as e:
            logger.warning(f"读取屏幕尺寸失败: {e}")
            return 0, 0

    def tap_xy(self, x: int, y: int) -> ActionResult:
        """按屏幕坐标点击（优先 ADB 触摸注入，失败回退 Appium 手势）。

        Args:
            x: 屏幕横坐标（像素）。
            y: 屏幕纵坐标（像素）。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            serial = resolve_device_serial(self.is_real_device, self.udid)
            if adb_tool.tap(int(x), int(y), device_serial=serial):
                return {"success": True, "message": f"已点击坐标 ({x}, {y})"}
            self.driver.execute_script("mobile: clickGesture", {"x": int(x), "y": int(y)})
            return {"success": True, "message": f"已点击坐标 ({x}, {y})（Appium 手势）"}
        except WebDriverException as e:
            msg = f"坐标点击失败 ({x}, {y}): {e}"
            logger.warning(msg)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = f"坐标点击出现未知异常 ({x}, {y}): {e}"
            logger.warning(msg)
            return {"success": False, "message": msg}

    def press_back(self) -> ActionResult:
        """发送系统返回键（用于关闭弹窗/广告）。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            serial = resolve_device_serial(self.is_real_device, self.udid)
            if adb_tool.keyevent("KEYCODE_BACK", device_serial=serial):
                return {"success": True, "message": "已发送返回键"}
            self.driver.back()
            return {"success": True, "message": "已发送返回键（Appium）"}
        except WebDriverException as e:
            msg = f"发送返回键失败: {e}"
            logger.warning(msg)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = f"发送返回键出现未知异常: {e}"
            logger.warning(msg)
            return {"success": False, "message": msg}

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 300,
    ) -> ActionResult:
        """执行一次滑动（用于滚动列表/页面）。

        Args:
            start_x: 起点横坐标。
            start_y: 起点纵坐标。
            end_x: 终点横坐标。
            end_y: 终点纵坐标。
            duration_ms: 滑动时长（毫秒）。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            serial = resolve_device_serial(self.is_real_device, self.udid)
            if adb_tool.swipe(
                int(start_x),
                int(start_y),
                int(end_x),
                int(end_y),
                duration_ms=int(duration_ms),
                device_serial=serial,
            ):
                return {"success": True, "message": "滑动完成"}
            self.driver.swipe(
                int(start_x), int(start_y), int(end_x), int(end_y), int(duration_ms)
            )
            return {"success": True, "message": "滑动完成（Appium 手势）"}
        except WebDriverException as e:
            msg = f"滑动失败: {e}"
            logger.warning(msg)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = f"滑动出现未知异常: {e}"
            logger.warning(msg)
            return {"success": False, "message": msg}

    def input_text_focused(self, text: str) -> ActionResult:
        """向当前焦点输入框输入文本（逐步模式用于无 resource-id 的输入框）。

        逐步模式会先点击目标输入控件使其获得焦点，再调用本方法。
        不记录输入内容，避免账号/密码等敏感文本进入日志。

        Args:
            text: 待输入文本。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            element = self.driver.switch_to.active_element
            if element is None:
                return {"success": False, "message": "当前没有获得焦点的输入框"}
            element.clear()
            element.send_keys(str(text))
            logger.info(f"已向焦点输入框输入 {len(str(text))} 个字符")
            return {"success": True, "message": f"已输入 {len(str(text))} 个字符"}
        except WebDriverException as e:
            msg = redact_text_in_message(f"向焦点输入框输入失败: {e}", text)
            logger.warning(msg)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = redact_text_in_message(f"向焦点输入框输入出现未知异常: {e}", text)
            logger.warning(msg)
            return {"success": False, "message": msg}

    def restart_app(self) -> ActionResult:
        """重启本次被测应用，用于多条测试用例之间复位 UI 状态。

        优先使用 Appium 的 terminate/activate；失败时回退到 ADB
        force-stop + am start，保证状态复位不中断后续用例。

        Returns:
            ActionResult: 执行结果字典 {success, message}。
        """
        if not self._ensure_session():
            return {"success": False, "message": "Appium 未连接，请先调用 connect()"}
        try:
            logger.info(f"重启被测应用: {self.app_package}")
            self.driver.terminate_app(self.app_package)
            time.sleep(0.5)
            self.driver.activate_app(self.app_package)
            time.sleep(1.0)
            return {
                "success": True,
                "message": f"已重启被测应用 {self.app_package}",
            }
        except Exception as e:
            logger.warning(f"Appium 重启应用失败，尝试 ADB 兜底: {e}")
            serial = resolve_device_serial(self.is_real_device, self.udid)
            stopped = adb_tool.force_stop_app(self.app_package, device_serial=serial)
            started = adb_tool.start_app(
                self.app_package,
                self.app_activity,
                device_serial=serial,
            )
            if stopped and started:
                time.sleep(1.0)
                return {
                    "success": True,
                    "message": f"已通过 ADB 重启被测应用 {self.app_package}",
                }
            return {
                "success": False,
                "message": f"重启被测应用失败: {e}",
            }

    def close(self) -> None:
        """关闭 Appium 会话并释放资源。

        无论 quit 是否抛出异常，均保证 self.driver 置空，
        调用方应在 try-finally 中调用本方法以确保会话释放。
        """
        if self.driver is None:
            return
        try:
            self.driver.quit()
            logger.info("Appium 会话已关闭")
        except Exception as e:
            logger.warning(f"关闭 Appium 会话时出现异常（仍会置空 driver）: {e}", exc_info=True)
        finally:
            self.driver = None
