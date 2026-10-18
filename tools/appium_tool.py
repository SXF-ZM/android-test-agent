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

from typing import Any, Dict, Optional

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
from utils.logger import logger

# 操作结果字典类型别名
ActionResult = Dict[str, Any]


class AppiumTool:
    """Appium 安卓自动化操作封装类。

    Attributes:
        driver: Appium Remote WebDriver 会话实例；未连接时为 None。
        is_real_device: 设备模式覆盖值（None 表示使用 settings 配置）。
        udid: 真机 UDID 覆盖值（None 表示使用 settings 配置）。
    """

    def __init__(self, is_real_device: Optional[bool] = None, udid: Optional[str] = None) -> None:
        """初始化工具实例，driver 置空等待 connect() 建立会话。

        Args:
            is_real_device: 设备模式覆盖值；None 时读取 settings.IS_REAL_DEVICE。
            udid: 真机 UDID 覆盖值；None 时读取 settings 对应设备配置。
        """
        self.driver: Optional[webdriver.Remote] = None
        self.is_real_device: Optional[bool] = is_real_device
        self.udid: Optional[str] = udid

    def _build_capabilities(self) -> Dict[str, Any]:
        """构建本次会话的 UiAutomator2 capability 参数。

        存在前端覆盖参数时按覆盖值构建；否则直接复用 settings.ANDROID_CAP。

        Returns:
            Dict[str, Any]: capability 参数字典。
        """
        if self.is_real_device is None and not self.udid:
            return settings.ANDROID_CAP
        # 前端覆盖模式：以 settings.ANDROID_CAP 为底，按覆盖值调整设备参数
        caps = dict(settings.ANDROID_CAP)
        use_real = self.is_real_device if self.is_real_device is not None else settings.IS_REAL_DEVICE
        if use_real:
            caps["appium:udid"] = self.udid or settings.REAL_PHONE_UDID
            caps.pop("appium:platformVersion", None)
        else:
            caps.pop("appium:udid", None)
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
            name = settings.emulator_device_name
        return f"{mode}, 设备: {name}"

    def connect(self) -> bool:
        """建立与 Appium Server 的连接，启动被测 App 会话。

        capability 默认由 settings.ANDROID_CAP 动态生成（自动区分真机 / 模拟器），
        存在前端覆盖参数时按覆盖值构建。连接失败时自动清理残留 driver，
        防止半初始化会话占用设备。

        Returns:
            bool: 连接成功返回 True，失败返回 False。
        """
        try:
            options = UiAutomator2Options()
            options.load_capabilities(self._build_capabilities())

            logger.info(
                f"正在连接 Appium 服务: {settings.appium_server_url}, "
                f"设备模式: {self._device_label()}, "
                f"被测应用: {settings.app_package}/{settings.app_activity}"
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
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((AppiumBy.ID, resource_id))
            )
            element.click()
            logger.info(f"点击元素成功: {resource_id}")
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
            logger.info(f"执行输入操作，resource-id: {resource_id}, 内容: {text}")
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((AppiumBy.ID, resource_id))
            )
            element.clear()
            element.send_keys(text)
            logger.info(f"输入文本成功: {resource_id}")
            return {"success": True, "message": f"向 {resource_id} 输入 [{text}] 成功"}
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
            msg = f"输入文本协议异常: {resource_id}, 错误: {e}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}
        except Exception as e:
            msg = f"输入文本未知异常: {resource_id}, 错误: {e}"
            logger.error(msg, exc_info=True)
            return {"success": False, "message": msg}

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
