# -*- coding: utf-8 -*-
"""日志与报告脱敏工具。

静态动作规划直接使用测试用例中的输入文本，这些文本可能包含账号、密码
或业务敏感数据。本模块统一把输入内容替换为长度占位符，避免其进入日志、
动作结果、重规划提示词和 Markdown 报告。

脱敏函数保持幂等：已经脱敏的 ``[已输入 N 个字符]`` 再次处理时原样返回，
这样报告层与执行层可以重复调用而不会出现二次包装。
"""

from __future__ import annotations

import re
from typing import Any, Dict

# 已脱敏占位符，用于避免重复脱敏
_REDACTED_TEXT_RE = re.compile(r"^\[已输入 \d+ 个字符\]$")

# resource-id 中疑似密码/验证码字段的关键字
_PASSWORD_ID_KEYWORDS = ("password", "passwd", "otp", "verification_code")


def mask_text(value: Any) -> str:
    """把输入文本替换为长度占位符。

    Args:
        value: 原始输入内容。

    Returns:
        str: 空内容返回空字符串；已脱敏占位符原样返回；
        其余返回 ``[已输入 N 个字符]``。
    """
    text = "" if value is None else str(value)
    if not text:
        return ""
    if _REDACTED_TEXT_RE.fullmatch(text):
        return text
    return f"[已输入 {len(text)} 个字符]"


def redact_text_in_message(message: Any, text: Any) -> str:
    """把消息中可能回显的输入内容替换为脱敏占位符。

    部分驱动/协议异常会把 ``send_keys`` 的参数原样带回错误信息，
    在写日志和动作结果前需要再兜底一次。

    Args:
        message: 原始日志或错误信息。
        text: 本次输入内容。

    Returns:
        str: 脱敏后的消息。
    """
    raw_text = "" if text is None else str(text)
    raw_message = "" if message is None else str(message)
    if not raw_text:
        return raw_message
    return raw_message.replace(raw_text, mask_text(raw_text))


def is_password_resource_id(resource_id: Any) -> bool:
    """判断 resource-id 是否像密码/验证码输入框。

    Args:
        resource_id: 控件 resource-id。

    Returns:
        bool: 命中密码类关键字返回 True。
    """
    lower = str(resource_id or "").lower()
    return any(keyword in lower for keyword in _PASSWORD_ID_KEYWORDS)


def redact_action_params(action: Any, params: Any) -> Dict[str, Any]:
    """生成可安全写入日志/报告的动作参数副本。

    目前只有 ``input_text`` 携带用户输入内容，其余动作按原样保留。
    参数不是字典时返回空字典，避免报告层因异常类型崩溃。

    Args:
        action: 动作类型。
        params: 原始动作参数。

    Returns:
        Dict[str, Any]: 脱敏后的参数副本。
    """
    safe: Dict[str, Any] = dict(params) if isinstance(params, dict) else {}
    if str(action) == "input_text" and "text" in safe:
        safe["text"] = mask_text(safe.get("text"))
    return safe


def redact_action_item(item: Any) -> Dict[str, Any]:
    """生成可安全写入执行结果的动作副本（含脱敏参数）。

    Args:
        item: 原始动作字典。

    Returns:
        Dict[str, Any]: 保留原字段并替换 params 的副本；非法输入返回空字典。
    """
    if not isinstance(item, dict):
        return {}
    safe = dict(item)
    safe["params"] = redact_action_params(item.get("action", ""), item.get("params"))
    return safe
