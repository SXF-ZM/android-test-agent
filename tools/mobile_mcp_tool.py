# -*- coding: utf-8 -*-
"""
mobile-mcp 执行后端
通过 Model Context Protocol（MCP）调用 mobile-next/mobile-mcp 的安卓自动化工具，
实现与 AppiumTool 相同的动作接口：
- connect()     启动 npx @mobilenext/mobile-mcp 子进程（或连接 HTTP 端点）并完成 MCP 初始化
- click_by_id() 先用 mobile_list_elements_on_screen 获取无障碍元素树，再按坐标/ref 点击
- input_text()  点击目标输入框后调用 mobile_type_keys 输入文本
- clear_text()  聚焦输入框后通过 ADB 按键事件清空文本（mobile-mcp 1.0.15 无 clear 工具）
- restart_app() 通过 mobile_terminate_app + mobile_launch_app 复位被测应用
- close()       关闭 MCP 会话、子进程，并清理设备端 DeviceServer/宿主 daemon

实现要点：
1. MCP Python SDK 为 async API，而当前 LangGraph/FastAPI 链路是同步的；
   本模块用一条常驻事件循环线程承载 ClientSession，调用方通过
   run_coroutine_threadsafe 同步等待结果，不侵入现有同步架构。
2. 会话的进入与退出放在同一个协程任务中（AsyncExitStack），
   避免 anyio/stdio 子进程在跨任务关闭时报 cancel scope 错误。
3. mobile-mcp 的工具参数 schema 可能随版本变化，调用前按 inputSchema
   过滤参数，仅传递当前工具支持的字段，降低版本耦合。

前置条件：
- Node.js 20+ 与 npx 可用；
- 安卓设备已连接且 adb devices 可见；
- 已安装 requirements.txt 中的 mcp SDK。
"""

import asyncio
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config.settings import settings
from tools import adb_tool
from tools.device_driver import ActionResult, resolve_device_serial
from utils.logger import logger
from utils.redaction import (
    is_password_resource_id,
    mask_text,
    redact_text_in_message,
)
from utils.ui_tree import danger_reason_for_element

# MCP Python SDK 为可选导入：未安装时 Appium 后端仍可正常使用
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _MCP_IMPORT_ERROR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover - 仅在未安装依赖时触发
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    _MCP_IMPORT_ERROR = _exc

# 元素匹配时使用的标识字段（不同 mobile-mcp 版本/平台命名不一致）
_IDENTIFIER_KEYS: Tuple[str, ...] = (
    "identifier",
    "resource-id",
    "resourceId",
    "resource_id",
    # mobile-mcp 的 text 输出使用 id="com.pkg:id/xxx"
    "id",
    "accessibilityIdentifier",
    "accessibilityLabel",
    "content-desc",
    "contentDescription",
    "aria-label",
    "label",
    "text",
    "value",
    "hint",
    "name",
)

# 容器字段：元素树可能直接是数组，也可能包在对象里
_CONTAINER_KEYS: Tuple[str, ...] = (
    "elements",
    "items",
    "data",
    "result",
    "nodes",
    "children",
)

# 判断一个 dict 是否像 UI 元素（而非纯容器）
_ELEMENT_MARKER_KEYS = set(_IDENTIFIER_KEYS) | {
    "bounds",
    "rect",
    "frame",
    "ref",
    "elementRef",
    "element_ref",
    "children",
}


def _parse_json_payload(text: str) -> Any:
    """从工具返回文本中提取 JSON 结构。

    mobile-mcp 通常返回纯 JSON，但部分版本会在前后附加说明文字或
    markdown 代码块；这里先整体解析，再尝试从第一个 JSON 数组/对象处解析。

    Args:
        text: 工具返回的文本内容。

    Returns:
        Any: 解析后的 JSON 对象；无法解析时返回 None。
    """
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        pass
    decoder = json.JSONDecoder()
    for marker in ("[", "{"):
        index = cleaned.find(marker)
        if index < 0:
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
            return parsed
        except json.JSONDecodeError:
            continue
    return None


def _iter_element_dicts(obj: Any, seen: Optional[set] = None) -> Iterable[Dict[str, Any]]:
    """递归遍历元素树，产出疑似 UI 元素字典。

    Args:
        obj: 工具返回的 JSON 结构。
        seen: 已访问对象 id 集合，避免异常结构导致重复/递归。

    Yields:
        Dict[str, Any]: 疑似 UI 元素字典。
    """
    if seen is None:
        seen = set()
    if isinstance(obj, (dict, list)):
        obj_id = id(obj)
        if obj_id in seen:
            return
        seen.add(obj_id)

    if isinstance(obj, list):
        for item in obj:
            yield from _iter_element_dicts(item, seen)
        return

    if not isinstance(obj, dict):
        return

    markers = [key for key in obj if key in _ELEMENT_MARKER_KEYS]
    if markers and markers != ["children"]:
        yield obj

    for key in _CONTAINER_KEYS:
        child = obj.get(key)
        if isinstance(child, (dict, list)):
            yield from _iter_element_dicts(child, seen)

    # 兼容以 ref/id 为键的映射结构：{"1": {...}, "2": {...}}
    for key, value in obj.items():
        if key in _CONTAINER_KEYS:
            continue
        if isinstance(value, (dict, list)):
            yield from _iter_element_dicts(value, seen)


def _parse_text_elements(text: str) -> List[Dict[str, Any]]:
    """解析 mobile-mcp 的 text 格式元素列表。

    当工具版本不支持 format="json" 时，输出形如：
        @e5 Button text="登录" at=312,680 size=456x96
        @e9 EditText label="手机号" at=120,560 size=720x120

    Args:
        text: 工具返回的纯文本。

    Returns:
        List[Dict[str, Any]]: 转换后的元素字典列表。
    """
    elements: List[Dict[str, Any]] = []
    line_pattern = re.compile(
        r"^\s*@(?P<ref>[^\s]+)\s+(?P<type>\S+)\s*(?P<attrs>.*)$"
    )
    attr_pattern = re.compile(r'(\w+)=("([^"]*)"|[^\s]+)')
    for line in text.splitlines():
        match = line_pattern.match(line)
        if not match:
            continue
        element: Dict[str, Any] = {
            # mobile-mcp 的 ref 形如 @e5，text 行首的 @ 需要保留
            "ref": "@" + match.group("ref"),
            "type": match.group("type"),
        }
        for attr in attr_pattern.finditer(match.group("attrs")):
            key = attr.group(1)
            value = attr.group(3) if attr.group(3) is not None else attr.group(2)
            if key == "at":
                coords = value.split(",")
                if len(coords) == 2:
                    element["x"] = _to_int(coords[0])
                    element["y"] = _to_int(coords[1])
            elif key == "size":
                size = value.lower().split("x")
                if len(size) == 2:
                    element["width"] = _to_int(size[0])
                    element["height"] = _to_int(size[1])
            else:
                element[key] = value
        rect = _element_rect(element)
        if rect:
            element["bounds"] = f"[{rect[0]},{rect[1]}][{rect[2]},{rect[3]}]"
        elements.append(element)
    return elements


def _extract_elements(payload: Any, raw_text: str = "") -> List[Dict[str, Any]]:
    """从工具 JSON 或 text 输出中提取去重后的元素列表。

    Args:
        payload: 工具返回并解析后的 JSON 结构；解析失败时为 None。
        raw_text: 工具原始文本（用于 text 格式兜底解析）。

    Returns:
        List[Dict[str, Any]]: UI 元素列表。
    """
    candidates = (
        list(_iter_element_dicts(payload))
        if payload is not None
        else _parse_text_elements(raw_text)
    )
    elements: List[Dict[str, Any]] = []
    seen_keys = set()
    for element in candidates:
        rect = _element_rect(element)
        ref = _element_ref(element)
        text = _first_text(element)
        identity = ("ref", ref) if ref else ("rect", rect, text[:40])
        if identity in seen_keys:
            continue
        seen_keys.add(identity)
        elements.append(element)
    return elements


def _first_text(element: Dict[str, Any]) -> str:
    """读取元素的首个可读文本（用于日志与去重）。"""
    for key in ("text", "label", "content-desc", "contentDescription", "value", "hint", "name"):
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _to_int(value: Any) -> Optional[int]:
    """将坐标值转为 int，失败返回 None。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _rect_from_value(value: Any) -> Optional[Tuple[int, int, int, int]]:
    """将 bounds/rect/frame 字段解析为 (x1, y1, x2, y2)。

    支持格式：
    - "[x1,y1][x2,y2]"（Android UiAutomator 常见格式）
    - {"x":..,"y":..,"width":..,"height":..}
    - {"left":..,"top":..,"right":..,"bottom":..}
    - {"x1":..,"y1":..,"x2":..,"y2":..}
    - [x, y, width, height]

    Args:
        value: 原始坐标字段值。

    Returns:
        Optional[Tuple[int, int, int, int]]: 左上/右下坐标；无法解析返回 None。
    """
    if isinstance(value, str):
        match = re.search(
            r"\[?\s*(-?\d+)\s*,\s*(-?\d+)\s*\]?\s*[\[\(]?\s*(-?\d+)\s*,\s*(-?\d+)\s*\]?",
            value,
        )
        if match:
            return tuple(int(part) for part in match.groups())  # type: ignore[return-value]
        return None

    if isinstance(value, (list, tuple)) and len(value) >= 4:
        numbers = [_to_int(item) for item in value[:4]]
        if all(number is not None for number in numbers):
            x, y, third, fourth = numbers  # type: ignore[misc]
            # [x, y, width, height] 或 [x1, y1, x2, y2]
            if third > x and fourth > y and (third - x) > 2 and (fourth - y) > 2:
                return int(x), int(y), int(third), int(fourth)
            return int(x), int(y), int(x + third), int(y + fourth)
        return None

    if isinstance(value, dict):
        x = _to_int(value.get("x"))
        y = _to_int(value.get("y"))
        width = _to_int(value.get("width"))
        height = _to_int(value.get("height"))
        if x is not None and y is not None and width and height:
            return x, y, x + width, y + height

        left = _to_int(value.get("left"))
        top = _to_int(value.get("top"))
        right = _to_int(value.get("right"))
        bottom = _to_int(value.get("bottom"))
        if None not in (left, top, right, bottom):
            return int(left), int(top), int(right), int(bottom)  # type: ignore[arg-type]

        x1 = _to_int(value.get("x1"))
        y1 = _to_int(value.get("y1"))
        x2 = _to_int(value.get("x2"))
        y2 = _to_int(value.get("y2"))
        if None not in (x1, y1, x2, y2):
            return int(x1), int(y1), int(x2), int(y2)  # type: ignore[arg-type]
    return None


def _element_rect(element: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    """从元素字典中提取屏幕矩形。"""
    # mobile-mcp 的 JSON 输出把坐标放在 coordinates: {x, y, width, height} 中
    for key in ("bounds", "rect", "frame", "position", "coordinates"):
        rect = _rect_from_value(element.get(key))
        if rect:
            return rect

    x = _to_int(element.get("x"))
    y = _to_int(element.get("y"))
    width = _to_int(element.get("width"))
    height = _to_int(element.get("height"))
    if x is not None and y is not None and width and height:
        return x, y, x + width, y + height
    return None


def _element_center(element: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """计算元素中心点坐标。"""
    rect = _element_rect(element)
    if not rect:
        return None
    x1, y1, x2, y2 = rect
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def _element_ref(element: Dict[str, Any]) -> str:
    """读取 mobile-mcp 元素引用（ref），用于优先按 ref 点击。"""
    for key in ("ref", "elementRef", "element_ref"):
        value = element.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _identifier_values(element: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
    """产出元素的 (字段名, 字段值) 可读标识，用于匹配 resource-id。"""
    for key in _IDENTIFIER_KEYS:
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            yield key, value.strip()


def _match_score(element: Dict[str, Any], resource_id: str) -> int:
    """计算元素与目标 resource-id 的匹配分。

    Args:
        element: UI 元素字典。
        resource_id: LLM 规划出的 resource-id。

    Returns:
        int: 匹配分（0 表示不匹配），越高越可信。
    """
    target = resource_id.strip().lower()
    if not target:
        return 0
    # resource-id 常见形式 com.pkg:id/btn_5，同时尝试短名与分词（btn_5 -> btn / 5）
    short_name = re.split(r"[:/]", target)[-1].strip()
    tokens = {target, short_name}
    tokens.update(
        token for token in re.split(r"[_\-/.:]+", short_name) if token
    )
    best = 0
    for key, value in _identifier_values(element):
        candidate = value.lower()
        score = 0
        if candidate == target:
            score = 100
        elif short_name and candidate == short_name and len(short_name) >= 3:
            score = 95
        elif candidate.endswith(":" + short_name) or candidate.endswith("/" + short_name):
            score = 90
        elif target in candidate:
            score = 70
        elif candidate in target and len(candidate) >= 4:
            score = 60

        # 兜底：LLM 给出 btn_5 这类 resource-id，而元素只暴露 text="5"
        if key in ("text", "label", "value", "hint", "content-desc", "contentDescription", "accessibilityLabel", "name"):
            for token in tokens:
                if token and candidate == token and len(token) >= 1:
                    score = max(score, 95 if token == short_name else 85)
                elif token and len(token) >= 3 and token in candidate:
                    score = max(score, 65)
        if score > best:
            best = score
    return best


def _find_element(
    elements: List[Dict[str, Any]], resource_id: str
) -> Optional[Dict[str, Any]]:
    """在元素列表中查找与 resource-id 最匹配的元素。"""
    best_element: Optional[Dict[str, Any]] = None
    best_score = 0
    for element in elements:
        score = _match_score(element, resource_id)
        if score > best_score:
            best_element = element
            best_score = score
    if best_score < 50:
        return None
    return best_element


def _result_text(result: Any) -> str:
    """提取 MCP CallToolResult 的文本内容。

    MCP SDK 2.x 对字符串返回值会同时填充 content(TextContent) 与
    structuredContent({"result": "..."})；应优先读取可读文本，
    否则会把整体包成 JSON 对象，导致元素列表解析失败。
    """
    parts: List[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
            continue
        data = getattr(item, "data", None)
        if data:
            parts.append(str(data))
    if parts:
        return "\n".join(parts)

    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            return json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return ""


def _result_is_error(result: Any) -> bool:
    """读取 CallToolResult 的错误标记（兼容驼峰/下划线字段）。"""
    return bool(
        getattr(result, "isError", False) or getattr(result, "is_error", False)
    )


class MobileMcpTool:
    """mobile-mcp 执行驱动：实现 DeviceDriver 协议。

    Attributes:
        backend_name: 固定为 "mobile_mcp"。
        device_id: mobile-mcp 设备标识，默认与 adb 目标序列号一致。
        app_package: 本次任务被测应用包名（空表示使用 settings 配置）。
        app_activity: 本次任务启动 Activity（空表示使用 settings 配置）。
    """

    backend_name = "mobile_mcp"

    def __init__(
        self,
        is_real_device: Optional[bool] = None,
        udid: Optional[str] = None,
        device_id: Optional[str] = None,
        app_package: Optional[str] = None,
        app_activity: Optional[str] = None,
    ) -> None:
        """初始化驱动，延迟到 connect() 才启动 MCP 子进程。

        Args:
            is_real_device: 设备模式覆盖值；None 时读取 settings。
            udid: 真机 UDID 覆盖值。
            device_id: mobile-mcp 设备标识覆盖值；为空时按单次任务设备覆盖值、
                MOBILE_MCP_DEVICE、当前 adb 目标序列号的顺序解析。
            app_package: 本次任务被测应用包名；空时读取 settings.app_package。
            app_activity: 本次任务启动 Activity；空时读取 settings.app_activity。
        """
        self.is_real_device = is_real_device
        self.udid = udid
        self.app_package: str = (app_package or "").strip() or settings.app_package
        self.app_activity: str = (app_activity or "").strip() or settings.app_activity
        # 设备优先级：显式 device_id > 单次任务设备覆盖值 > MOBILE_MCP_DEVICE
        # > 当前 .env 模拟器/真机序列号。单次任务覆盖必须优先于全局 MCP 配置，
        # 否则动作设备与 adb 日志/截图设备可能不一致。
        explicit_device = (device_id or "").strip()
        has_task_override = is_real_device is not None or bool((udid or "").strip())
        if explicit_device:
            self.device_id = explicit_device
        elif has_task_override:
            self.device_id = resolve_device_serial(is_real_device, udid)
        else:
            self.device_id = (
                (settings.mobile_mcp_device or "").strip()
                or resolve_device_serial(is_real_device, udid)
            )
        self._transport = settings.validate_mobile_mcp_transport()
        self._startup_timeout = max(1, settings.mobile_mcp_startup_timeout)
        self._call_timeout = max(1, settings.mobile_mcp_call_timeout)
        self._close_timeout = max(1, settings.mobile_mcp_close_timeout)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[Any] = None
        self._tools: Dict[str, Any] = {}
        self._stop_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._connect_error: Optional[Exception] = None
        # close() 可能发生在会话初始化完成前（如启动超时），
        # 用该标记让后台协程在初始化后立即退出，避免遗留孤儿会话
        self._closing = False

    def describe(self) -> str:
        """返回本次会话的设备描述（用于日志）。"""
        use_real = (
            self.is_real_device
            if self.is_real_device is not None
            else settings.IS_REAL_DEVICE
        )
        mode = "真机" if use_real else "模拟器"
        return f"mobile-mcp({self._transport}), {mode}, 设备: {self.device_id}"

    # ==================== 会话生命周期 ====================
    def connect(self) -> bool:
        """启动 mobile-mcp 并完成 MCP 初始化。

        Returns:
            bool: 初始化成功返回 True；依赖缺失、启动超时或协议异常返回 False。
        """
        if _MCP_IMPORT_ERROR is not None:
            logger.error(
                "未安装或无法导入 MCP Python SDK，无法使用 mobile_mcp 后端。"
                f"请执行 pip install -r requirements.txt 后重试（导入错误: {_MCP_IMPORT_ERROR}）"
            )
            return False

        if self._session is not None:
            return True
        if self._thread is not None and self._thread.is_alive():
            # 上一次启动可能仍在进行或已失败，先清理再重试
            old_thread = self._thread
            self.close()
            if old_thread.is_alive():
                logger.error("上一次 mobile-mcp 会话尚未退出，请稍后重试")
                return False

        self._connect_error = None
        self._closing = False
        self._ready.clear()
        self._tools = {}
        logger.info(f"正在启动 mobile-mcp 执行后端: {self.describe()}")
        if self._transport == "stdio":
            logger.info(
                f"mobile-mcp 启动命令: {settings.mobile_mcp_command} "
                f"{settings.mobile_mcp_args}（首次运行需下载 npm 包）"
            )
        else:
            logger.info(f"mobile-mcp HTTP 端点: {settings.mobile_mcp_url}")

        self._thread = threading.Thread(
            target=self._thread_main,
            name="mobile-mcp-session",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(self._startup_timeout):
            logger.error(
                f"mobile-mcp 启动超时（{self._startup_timeout}s）。"
                "请确认已安装 Node.js 20+ 且 npx 可用；首次运行需联网下载 npm 包"
            )
            self.close()
            return False

        if self._connect_error is not None or self._session is None:
            logger.error(
                f"mobile-mcp 启动失败: {self._connect_error or '未建立 MCP 会话'}。"
                "请检查 Node.js/npx、设备连接与 MOBILE_MCP_* 配置"
            )
            self.close()
            return False

        logger.info(
            f"mobile-mcp 会话建立成功，可用工具 {len(self._tools)} 个，"
            f"设备: {self.device_id}"
        )
        # 与 Appium 的 forceAppLaunch 行为对齐：mobile-mcp 不会随会话自动启动 App，
        # 因此连接后主动拉起被测应用，避免动作在桌面/其他应用上执行。
        if self.app_package:
            launch = self.call_tool(
                "mobile_launch_app", {"packageName": self.app_package}
            )
            if launch.get("success"):
                logger.info(f"mobile-mcp 已启动被测应用: {self.app_package}")
                # 等待应用窗口稳定后再执行首个动作
                time.sleep(1.0)
            else:
                logger.warning(
                    f"mobile-mcp 启动被测应用失败（将继续执行）: "
                    f"{launch.get('message', '')}"
                )
        return True

    def _thread_main(self) -> None:
        """常驻事件循环线程：承载 MCP 会话，直到 close() 请求停止。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_session_main())
        except Exception as exc:  # pragma: no cover - 仅异常路径
            self._connect_error = exc
            logger.error(f"mobile-mcp 事件循环异常: {exc}", exc_info=True)
        finally:
            self._ready.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    async def _open_transport(self, stack: AsyncExitStack) -> Tuple[Any, Any]:
        """按配置打开 stdio 或 Streamable HTTP 传输。"""
        if self._transport == "http":
            # MCP SDK 1.x 使用 streamablehttp_client，2.x 更名为 streamable_http_client；
            # 两者都返回 (read_stream, write_stream, ...)，这里只取前两项。
            try:
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError:  # pragma: no cover - 取决于安装的 mcp 版本
                from mcp.client.streamable_http import (
                    streamable_http_client as streamablehttp_client,
                )

            streams = await stack.enter_async_context(
                streamablehttp_client(settings.mobile_mcp_url)
            )
            return streams[0], streams[1]

        if stdio_client is None or StdioServerParameters is None:
            raise RuntimeError("MCP stdio 传输不可用，请安装 mcp SDK")

        args = shlex.split(settings.mobile_mcp_args, posix=(os.name != "nt"))
        params = StdioServerParameters(
            command=settings.mobile_mcp_command,
            args=args,
            env=None,
        )
        return await stack.enter_async_context(stdio_client(params))

    async def _async_session_main(self) -> None:
        """会话主协程：进入/退出 MCP 上下文必须在同一个任务中完成。"""
        self._stop_event = asyncio.Event()
        try:
            if self._closing:
                # close() 在会话初始化前已被调用（如启动超时），直接退出
                return
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await self._open_transport(stack)
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()
                tools_result = await session.list_tools()
                tools = getattr(tools_result, "tools", []) or []
                self._tools = {
                    str(getattr(tool, "name", "")): tool
                    for tool in tools
                    if getattr(tool, "name", "")
                }
                self._session = session
                self._ready.set()
                await self._stop_event.wait()
        except asyncio.CancelledError:
            logger.info("mobile-mcp 会话已取消")
        except Exception as exc:
            self._connect_error = exc
            logger.error(f"mobile-mcp 会话初始化失败: {exc}", exc_info=True)
        finally:
            self._session = None
            self._stop_event = None
            self._ready.set()

    def close(self) -> None:
        """请求会话停止，并等待子进程/事件循环退出。"""
        self._closing = True
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and loop.is_running() and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass

        thread = self._thread
        thread_alive = False
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=self._close_timeout)
            thread_alive = thread.is_alive()
            if thread_alive:
                logger.warning(
                    f"mobile-mcp 会话在 {self._close_timeout}s 内未退出，"
                    "已放弃等待（后台线程为 daemon，不阻塞进程退出）"
                )

        # 线程仍存活时保留引用，避免下一次 connect() 在旧会话未退出时
        # 又启动第二个 MCP 子进程；旧线程退出后该引用不再 is_alive()。
        self._thread = thread if thread_alive else None
        self._loop = None
        self._session = None
        self._stop_event = None
        self._tools = {}

        # 跨后端清理：mobile-mcp 的 DeviceServer 会占用 Android UiAutomation，
        # 若不在关闭时清理，后续切回 Appium 会报 already registered。
        try:
            adb_tool.stop_mobile_mcp_device_server(device_serial=self.device_id)
        except Exception as exc:
            logger.debug(f"清理设备端 DeviceServer 失败（忽略）: {exc}")
        self._stop_host_daemon()

    def _stop_host_daemon(self) -> None:
        """停止 mobilecli 宿主 daemon，避免其长期驻留并占用设备。

        优先调用 `mobilecli daemon stop`（通过进程命令行定位二进制），
        失败时回退到 pkill/taskkill。仅在 mobile-mcp 会话关闭时调用，
        避免影响其他长期运行的 MCP 服务。
        """
        if not getattr(settings, "mobile_mcp_cleanup_daemon", True):
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/IM", "mobilecli.exe"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return

            found = subprocess.run(
                ["pgrep", "-af", "mobilecli.*daemon start"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in (found.stdout or "").splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) < 2:
                    continue
                binary = parts[1].split()[0]
                if "mobilecli" not in binary:
                    continue
                subprocess.run(
                    [binary, "daemon", "stop"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            # daemon stop 失败或进程命令行不含子命令时兜底
            subprocess.run(
                ["pkill", "-f", "mobilecli.*daemon start"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            logger.debug("未找到 pgrep/pkill，跳过 mobilecli daemon 清理")
        except subprocess.TimeoutExpired:
            logger.debug("停止 mobilecli daemon 超时（忽略）")
        except Exception as exc:
            logger.debug(f"停止 mobilecli daemon 失败（忽略）: {exc}")

    def __enter__(self) -> "MobileMcpTool":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    # ==================== 工具调用 ====================
    def _tool_properties(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """读取工具 inputSchema.properties；未知时返回 None。"""
        tool = self._tools.get(tool_name)
        if tool is None:
            return None
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "input_schema", None)
        if not isinstance(schema, dict):
            return None
        properties = schema.get("properties")
        return properties if isinstance(properties, dict) else None

    def _prepare_args(
        self, tool_name: str, arguments: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """按工具 schema 过滤参数，并自动补全 device 字段。"""
        prepared = dict(arguments or {})
        properties = self._tool_properties(tool_name)
        if properties is not None:
            prepared = {
                key: value for key, value in prepared.items() if key in properties
            }
        if self.device_id and (properties is None or "device" in properties):
            prepared.setdefault("device", self.device_id)
        return prepared

    def call_tool(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """同步调用 mobile-mcp 工具。

        Args:
            tool_name: MCP 工具名（如 mobile_list_elements_on_screen）。
            arguments: 工具参数（device 字段会自动补全）。

        Returns:
            Dict[str, Any]: {success, message, text}；失败时 success=False。
        """
        session = self._session
        loop = self._loop
        if session is None or loop is None or not loop.is_running():
            return {
                "success": False,
                "message": "mobile-mcp 未连接，请先调用 connect()",
                "text": "",
            }
        if self._tools and tool_name not in self._tools:
            return {
                "success": False,
                "message": f"mobile-mcp 未提供工具 {tool_name}（当前版本可能不支持）",
                "text": "",
            }

        prepared = self._prepare_args(tool_name, arguments)
        # 输入类工具的 text 参数可能包含账号/密码，调试日志同样脱敏
        log_args = dict(prepared)
        if tool_name == "mobile_type_keys" and "text" in log_args:
            log_args["text"] = mask_text(log_args.get("text"))
        logger.debug(f"调用 mobile-mcp 工具: {tool_name}, 参数: {log_args}")

        async def _invoke() -> Any:
            return await asyncio.wait_for(
                session.call_tool(tool_name, arguments=prepared),
                timeout=self._call_timeout,
            )

        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
            result = future.result(timeout=self._call_timeout + 5)
        except concurrent.futures.TimeoutError:
            if future is not None:
                future.cancel()
            message = f"mobile-mcp 工具 {tool_name} 调用超时（{self._call_timeout}s）"
            logger.error(message)
            return {"success": False, "message": message, "text": ""}
        except Exception as exc:
            message = f"mobile-mcp 工具 {tool_name} 调用异常: {exc}"
            logger.error(message, exc_info=True)
            return {"success": False, "message": message, "text": ""}

        text = _result_text(result)
        is_error = _result_is_error(result)
        if is_error:
            # 错误回显可能带出输入内容，返回前先脱敏
            if tool_name == "mobile_type_keys":
                text = redact_text_in_message(text, prepared.get("text"))
            logger.warning(f"mobile-mcp 工具 {tool_name} 返回错误: {text[:300]}")
            return {
                "success": False,
                "message": text or f"mobile-mcp 工具 {tool_name} 执行失败",
                "text": text,
            }
        logger.debug(f"mobile-mcp 工具 {tool_name} 调用完成，返回 {len(text)} 字符")
        return {"success": True, "message": text, "text": text}

    def list_elements(self) -> List[Dict[str, Any]]:
        """获取当前屏幕的无障碍元素列表（供调试与动作定位）。

        Returns:
            List[Dict[str, Any]]: 元素列表；工具不可用或解析失败时返回空列表。
        """
        result = self.call_tool(
            "mobile_list_elements_on_screen", {"format": "json"}
        )
        if not result.get("success"):
            # 兼容不支持 format 参数的旧版本：退回默认 text 格式
            result = self.call_tool("mobile_list_elements_on_screen", {})
        if not result.get("success"):
            return []
        raw_text = str(result.get("text", ""))
        payload = _parse_json_payload(raw_text)
        elements = _extract_elements(payload, raw_text)
        if not elements:
            logger.warning(
                f"mobile-mcp 元素列表为空或无法解析: {raw_text[:500]}"
            )
        logger.info(f"mobile-mcp 获取到 {len(elements)} 个屏幕元素")
        return elements

    # ==================== DeviceDriver 动作接口 ====================
    def _element_safety_error(self, element: Dict[str, Any], action: str) -> Optional[str]:
        """检查 mobile-mcp 元素是否命中密码字段或敏感控件。

        Args:
            element: mobile-mcp 返回的元素字典。
            action: 动作类型（input_text 对密码字段更严格）。

        Returns:
            Optional[str]: 拒绝原因；安全时返回 None。
        """
        resource_id = ""
        for key in (
            "resource-id",
            "resourceId",
            "resource_id",
            "identifier",
            "id",
            "accessibilityIdentifier",
        ):
            value = element.get(key)
            if value:
                resource_id = str(value)
                break

        text = _first_text(element)
        content_desc = ""
        for key in (
            "content-desc",
            "contentDescription",
            "accessibilityLabel",
            "aria-label",
            "label",
        ):
            value = element.get(key)
            if value:
                content_desc = str(value)
                break

        class_name = ""
        for key in ("class", "className", "type"):
            value = element.get(key)
            if value:
                class_name = str(value)
                break

        password = (
            str(element.get("password") or element.get("secure") or "")
            .strip()
            .lower()
            == "true"
        )
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

    def _click_element(self, element: Dict[str, Any]) -> ActionResult:
        """点击已定位的元素：优先使用 ref，失败后回退到中心坐标。"""
        ref = _element_ref(element)
        if ref and not ref.startswith("@"):
            # mobile-mcp 的 ref 约定带 @ 前缀（如 @e5）
            ref = f"@{ref}"
        if ref:
            result = self.call_tool(
                "mobile_click_on_screen_at_coordinates", {"ref": ref}
            )
            if result.get("success"):
                return {
                    "success": True,
                    "message": f"点击元素 ref={ref} 成功",
                }
            logger.warning(f"按 ref={ref} 点击失败，尝试坐标点击: {result.get('message')}")

        center = _element_center(element)
        if center is None:
            return {
                "success": False,
                "message": "元素缺少可用坐标，且 ref 点击失败",
            }
        x, y = center
        result = self.call_tool(
            "mobile_click_on_screen_at_coordinates", {"x": x, "y": y}
        )
        if result.get("success"):
            return {"success": True, "message": f"点击坐标 ({x}, {y}) 成功"}
        return {
            "success": False,
            "message": result.get("message") or f"点击坐标 ({x}, {y}) 失败",
        }

    def click_by_id(self, resource_id: str, timeout: int = 10) -> ActionResult:
        """通过 resource-id 定位元素并点击。

        与 Appium 的隐式等待不同，mobile-mcp 每次调用都是一次即时快照；
        这里在 timeout 时间内按 1 秒间隔重试元素查找，模拟显式等待。

        Args:
            resource_id: 目标元素 resource-id。
            timeout: 等待元素出现的超时时间（秒）。

        Returns:
            ActionResult: {success, message}。
        """
        resource_id = (resource_id or "").strip()
        if not resource_id:
            return {"success": False, "message": "click_by_id 缺少 resource_id"}

        logger.info(f"mobile-mcp 执行点击，resource-id: {resource_id}")
        deadline = time.monotonic() + max(1, timeout)
        element = None
        while time.monotonic() < deadline:
            elements = self.list_elements()
            element = _find_element(elements, resource_id)
            if element is not None:
                break
            time.sleep(1)

        if element is None:
            message = f"元素不存在或无法识别: {resource_id}"
            logger.warning(message)
            return {"success": False, "message": message}

        safety_error = self._element_safety_error(element, "click_by_id")
        if safety_error:
            logger.warning(f"{safety_error}: {resource_id}")
            return {"success": False, "message": safety_error}

        result = self._click_element(element)
        if result.get("success"):
            logger.info(f"mobile-mcp 点击成功: {resource_id}")
        else:
            logger.warning(f"mobile-mcp 点击失败: {resource_id}, {result.get('message')}")
        return result

    def input_text(self, resource_id: str, text: str, timeout: int = 10) -> ActionResult:
        """定位输入框，点击聚焦后通过 mobile_type_keys 输入文本。

        Args:
            resource_id: 输入框 resource-id。
            text: 待输入文本。
            timeout: 等待输入框出现的超时时间（秒）。

        Returns:
            ActionResult: {success, message}。
        """
        resource_id = (resource_id or "").strip()
        if not resource_id:
            return {"success": False, "message": "input_text 缺少 resource_id"}

        logger.info(
            f"mobile-mcp 执行输入，resource-id: {resource_id}, 内容: {mask_text(text) or '(空)'}"
        )
        deadline = time.monotonic() + max(1, timeout)
        element = None
        while time.monotonic() < deadline:
            elements = self.list_elements()
            element = _find_element(elements, resource_id)
            if element is not None:
                break
            time.sleep(1)

        if element is None:
            message = f"输入框不存在或无法识别: {resource_id}"
            logger.warning(message)
            return {"success": False, "message": message}

        safety_error = self._element_safety_error(element, "input_text")
        if safety_error:
            logger.warning(f"{safety_error}: {resource_id}")
            return {"success": False, "message": safety_error}

        click_result = self._click_element(element)
        if not click_result.get("success"):
            return {
                "success": False,
                "message": f"输入框点击聚焦失败: {click_result.get('message')}",
            }

        # 等待软键盘/输入焦点稳定后再输入
        time.sleep(0.6)
        type_args: Dict[str, Any] = {"text": str(text), "submit": False}
        # 不同版本对“覆盖已有文本”的字段命名不同，仅在 schema 支持时传入
        type_props = self._tool_properties("mobile_type_keys") or {}
        for replace_key in ("replace", "clear"):
            if replace_key in type_props:
                type_args[replace_key] = True
        result = self.call_tool("mobile_type_keys", type_args)
        if result.get("success"):
            logger.info(f"mobile-mcp 输入成功: {resource_id}")
            return {
                "success": True,
                "message": f"向 {resource_id} 输入成功（{len(text)} 个字符，mobile-mcp）",
            }
        message = redact_text_in_message(
            result.get("message") or f"向 {resource_id} 输入失败", text
        )
        logger.warning(message)
        return {"success": False, "message": message}

    def clear_text(self, resource_id: str, timeout: int = 10) -> ActionResult:
        """定位输入框并清空文本。

        mobile-mcp 1.0.15 的 mobile_type_keys 要求 text 非空，且没有独立
        clear 工具；因此聚焦输入框后使用 ADB 的 MOVE_END + DEL 按键清空。

        Args:
            resource_id: 输入框 resource-id。
            timeout: 等待输入框出现的超时时间（秒）。

        Returns:
            ActionResult: {success, message}。
        """
        resource_id = (resource_id or "").strip()
        if not resource_id:
            return {"success": False, "message": "clear_text 缺少 resource_id"}

        deadline = time.monotonic() + max(1, timeout)
        element = None
        while time.monotonic() < deadline:
            elements = self.list_elements()
            element = _find_element(elements, resource_id)
            if element is not None:
                break
            time.sleep(1)

        if element is None:
            message = f"输入框不存在或无法识别: {resource_id}"
            logger.warning(message)
            return {"success": False, "message": message}

        safety_error = self._element_safety_error(element, "clear_text")
        if safety_error:
            logger.warning(f"{safety_error}: {resource_id}")
            return {"success": False, "message": safety_error}

        click_result = self._click_element(element)
        if not click_result.get("success"):
            return {
                "success": False,
                "message": f"输入框点击聚焦失败: {click_result.get('message')}",
            }

        time.sleep(0.3)
        # 尽量按当前文本长度删除；无法读取时按 100 个字符兜底
        current_text = _first_text(element) or ""
        char_count = len(current_text) if current_text else 100
        cleared = adb_tool.clear_focused_text(
            device_serial=self.device_id, char_count=char_count
        )
        if not cleared:
            message = f"清空输入框失败（ADB 按键未成功）: {resource_id}"
            logger.warning(message)
            return {"success": False, "message": message}
        logger.info(f"mobile-mcp 清空成功: {resource_id}")
        return {"success": True, "message": f"已清空 {resource_id}（mobile-mcp）"}

    def restart_app(self) -> ActionResult:
        """重启本次被测应用，用于多条用例之间复位 UI 状态。

        Returns:
            ActionResult: {success, message}。
        """
        logger.info(f"mobile-mcp 重启被测应用: {self.app_package}")
        terminate = self.call_tool(
            "mobile_terminate_app", {"packageName": self.app_package}
        )
        # 应用原本未运行时 terminate 可能返回错误，继续尝试 launch
        if not terminate.get("success"):
            logger.debug(f"mobile_terminate_app 返回: {terminate.get('message')}")

        time.sleep(0.5)
        launch = self.call_tool(
            "mobile_launch_app", {"packageName": self.app_package}
        )
        if launch.get("success"):
            time.sleep(1.0)
            return {
                "success": True,
                "message": f"已重启被测应用 {self.app_package}（mobile-mcp）",
            }

        # mobile-mcp 工具失败时使用 ADB 兜底
        stopped = adb_tool.force_stop_app(
            self.app_package, device_serial=self.device_id
        )
        started = adb_tool.start_app(
            self.app_package,
            self.app_activity,
            device_serial=self.device_id,
        )
        if stopped and started:
            time.sleep(1.0)
            return {
                "success": True,
                "message": f"已通过 ADB 重启被测应用 {self.app_package}",
            }
        message = f"重启被测应用失败: {launch.get('message', 'mobile_launch_app 失败')}"
        logger.warning(message)
        return {"success": False, "message": message}
