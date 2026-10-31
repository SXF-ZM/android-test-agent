# -*- coding: utf-8 -*-
"""LLM 结构化输出校验。

集中定义动作参数与缺陷字段的校验规则，避免模型输出中的拼写错误、
缺失字段或超范围数值直接流入设备执行与报告生成环节。

设计原则：
1. 校验失败只丢弃当前条目，不影响其他合法条目；
2. 不依赖具体 LLM Provider 的 function calling 能力，保持 qwen /
   DeepSeek 兼容性；
3. 校验函数返回中文错误信息，可直接拼入下一次重试提示词。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, field_validator

# 静态动作白名单（restart_app 一般由规划层注入，也允许模型显式返回）
STATIC_ACTION_NAMES = frozenset(
    {"click_by_id", "input_text", "clear_text", "sleep", "restart_app"}
)

# 逐步 UI 驱动动作白名单
STEPWISE_ACTION_NAMES = frozenset(
    {
        "tap_by_ref",
        "tap_by_text",
        "tap_by_coordinates",
        "input_text",
        "press_back",
        "scroll",
        "sleep",
        "done",
    }
)

def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """把值转换为 int；布尔值与非法值均视为失败。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """把值转换为 float；布尔值、NaN/Inf 与非法值均视为失败。"""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _as_bool(value: Any, default: bool = True) -> bool:
    """把常见布尔表示转换为 bool（兼容中文与字符串）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("false", "0", "no", "否"):
            return False
        if text in ("true", "1", "yes", "是"):
            return True
        return default
    if value is None:
        return default
    return bool(value)


def validate_static_action(
    item: Any, max_sleep_seconds: float = 30.0
) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验静态规划阶段的一个动作。

    Args:
        item: LLM 输出数组中的单个元素。
        max_sleep_seconds: sleep 动作允许的最大秒数。

    Returns:
        Tuple[Optional[Dict[str, Any]], str]: (规范化动作, 错误描述)。
        校验通过时错误描述为空字符串。
    """
    if not isinstance(item, dict):
        return None, "动作不是 JSON 对象"
    action = str(item.get("action") or "").strip()
    if action not in STATIC_ACTION_NAMES:
        return None, f"不支持的动作: {action or '(空)'}"
    params = item.get("params")
    if not isinstance(params, dict):
        return None, f"{action} 的 params 必须是 JSON 对象"
    params = dict(params)

    if action in ("click_by_id", "clear_text"):
        resource_id = str(params.get("resource_id") or "").strip()
        if not resource_id:
            return None, f"{action} 缺少 resource_id"
        params["resource_id"] = resource_id
    elif action == "input_text":
        resource_id = str(params.get("resource_id") or "").strip()
        if not resource_id:
            return None, "input_text 缺少 resource_id"
        params["resource_id"] = resource_id
        text = params.get("text", "")
        params["text"] = "" if text is None else str(text)
    elif action == "sleep":
        seconds = _as_float(params.get("seconds", 1), default=None)
        if seconds is None or seconds < 0 or seconds > max_sleep_seconds:
            return None, (
                f"sleep 的 seconds 必须是 0~{max_sleep_seconds:g} 之间的数字，"
                f"当前为 {params.get('seconds')!r}"
            )
        params = {"seconds": seconds}
    elif action == "restart_app":
        params = {}

    return {"action": action, "params": params}, ""


def validate_stepwise_params(
    action: str,
    params: Any,
    max_sleep_seconds: float = 10.0,
    max_input_chars: int = 300,
) -> Tuple[Dict[str, Any], str]:
    """校验并规范化逐步模式动作参数。

    Args:
        action: 动作名（必须已在 :data:`STEPWISE_ACTION_NAMES` 中）。
        params: LLM 输出的参数字典。
        max_sleep_seconds: sleep 动作允许的最大秒数。
        max_input_chars: 单次输入文本的最大字符数。

    Returns:
        Tuple[Dict[str, Any], str]: (规范化参数, 错误描述)。
    """
    if action not in STEPWISE_ACTION_NAMES:
        return {}, f"不支持的动作: {action or '(空)'}"
    if not isinstance(params, dict):
        return {}, f"{action} 的 params 必须是 JSON 对象"

    values = dict(params)
    if action == "tap_by_ref":
        ref = _as_int(values.get("ref"))
        if ref is None:
            return {}, "tap_by_ref 缺少合法的 ref"
        return {"ref": ref}, ""

    if action == "tap_by_text":
        text = str(values.get("text") or "").strip()
        if not text:
            return {}, "tap_by_text 缺少 text"
        index = _as_int(values.get("index", 0), 0) or 0
        return {
            "text": text,
            "index": max(0, index),
            "exact": _as_bool(values.get("exact", True), True),
        }, ""

    if action == "tap_by_coordinates":
        x = _as_int(values.get("x"))
        y = _as_int(values.get("y"))
        if x is None or y is None:
            return {}, "tap_by_coordinates 需要合法的 x/y 坐标"
        return {"x": x, "y": y}, ""

    if action == "input_text":
        text = values.get("text", "")
        text = "" if text is None else str(text)
        if len(text) > max_input_chars:
            return {}, (
                f"输入内容过长（{len(text)} 字符，上限 {max_input_chars}）"
            )
        normalized: Dict[str, Any] = {"text": text}
        if values.get("ref") is not None:
            ref = _as_int(values.get("ref"))
            if ref is None:
                return {}, "input_text 的 ref 非法"
            normalized["ref"] = ref
        return normalized, ""

    if action == "press_back":
        return {}, ""

    if action == "scroll":
        direction = str(values.get("direction") or "down").strip().lower()
        if direction not in ("down", "up"):
            return {}, "scroll 的 direction 必须是 down 或 up"
        return {"direction": direction}, ""

    if action == "sleep":
        seconds = _as_float(values.get("seconds", 1), default=None)
        if seconds is None or seconds < 0 or seconds > max_sleep_seconds:
            return {}, (
                f"sleep 的 seconds 必须是 0~{max_sleep_seconds:g} 之间的数字，"
                f"当前为 {values.get('seconds')!r}"
            )
        return {"seconds": seconds}, ""

    if action == "done":
        evidence = str(values.get("evidence") or "").strip()
        return {
            "success": _as_bool(values.get("success", True), True),
            "evidence": evidence[:200],
        }, ""

    return {}, f"暂未实现的逐步动作: {action}"


def _normalize_type(value: Any) -> str:
    """规范化缺陷类型；未知类型归入“其他”。"""
    text = str(value or "").strip()
    compact = text.replace(" ", "").casefold()
    aliases = {
        "crash": "Crash",
        "崩溃": "Crash",
        "anr": "ANR",
        "无响应": "ANR",
        "ui异常": "UI异常",
        "uiexception": "UI异常",
        "界面异常": "UI异常",
        "其他": "其他",
        "other": "其他",
    }
    return aliases.get(compact, "其他")


def _normalize_severity(value: Any) -> str:
    """规范化缺陷严重级别；未知级别归入“一般”。"""
    text = str(value or "").strip()
    compact = text.replace(" ", "").casefold()
    aliases = {
        "致命": "致命",
        "critical": "致命",
        "严重": "严重",
        "major": "严重",
        "高": "严重",
        "一般": "一般",
        "medium": "一般",
        "中": "一般",
        "轻微": "轻微",
        "minor": "轻微",
        "低": "轻微",
    }
    return aliases.get(compact, "一般")


class DefectItem(BaseModel):
    """单个缺陷的结构化模型。"""

    type: str = "其他"
    severity: str = "一般"
    description: str = "未提供描述"
    evidence: str = ""
    suggestion: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _validate_type(cls, value: Any) -> str:
        return _normalize_type(value)

    @field_validator("severity", mode="before")
    @classmethod
    def _validate_severity(cls, value: Any) -> str:
        return _normalize_severity(value)

    @field_validator("description", "evidence", "suggestion", mode="before")
    @classmethod
    def _to_text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("description")
    @classmethod
    def _ensure_description(cls, value: str) -> str:
        return value or "未提供描述"
