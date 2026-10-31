# -*- coding: utf-8 -*-
"""UI 树解析、裁剪与安全筛选工具。

逐步 UI 驱动 Agent 会在每一步读取当前屏幕的 Appium page_source，
由本模块转换为紧凑、可引用的节点列表，再交给 LLM 选择下一步动作。

安全约束：
1. 只保留被测应用包内的节点，过滤系统 UI 与其他应用节点；
2. 密码字段的文本一律隐藏；
3. 对购买、订阅、支付、分享等敏感控件做标记，执行层默认拒绝点击；
4. 限制节点数量与单段文本长度，避免 prompt 过大或泄露无关内容。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# 控制字符、方向控制符与零宽字符
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁯]"
)
_WHITESPACE_RE = re.compile(r"\s+")
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 明确属于购买/订阅/支付/分享流程的完整文案（避免误伤课程里的单词题）
_DANGEROUS_PHRASES = (
    "立即购买",
    "去购买",
    "确认购买",
    "购买会员",
    "开通会员",
    "开通订阅",
    "订阅会员",
    "立即订阅",
    "免费试用",
    "开始试用",
    "开始免费试用",
    "确认支付",
    "立即支付",
    "去支付",
    "确认付款",
    "立即充值",
    "确认充值",
    "连续包月",
    "自动续费",
    "分享给",
    "发送消息",
    "发消息",
    "去暴涨",
    "领取会员",
    "升级会员",
)

# 英文短语可能出现在正常课程词块中（例如购物课会教 checkout / top up），
# 因此只在真正的按钮/图标或 content-desc 上做单词级英文拦截；
# 普通 TextView 答题词块交由 prompt 规则约束，避免误伤正确答案。
_DANGEROUS_CONTROL_PHRASES = (
    "buy now",
    "purchase now",
    "subscribe now",
    "start free trial",
    "free trial",
    "share with",
    "send message",
    "top up",
    "checkout",
    "confirm payment",
)

# 敏感控件的 resource-id / content-desc 关键字
_DANGEROUS_ID_KEYWORDS = (
    "subscription",
    "purchase",
    "checkout",
    "payment",
    "pay_",
    "buy",
    "share",
    "send",
    "topup",
    "top_up",
    "recharge",
)

# 仅当控件本身是按钮/图标，或 content-desc 明确为这些词时才拦截；
# 普通课程词块（TextView）即使文本是 "buy"/"订阅" 也允许点击。
_DANGEROUS_SINGLE_WORDS = {
    "购买",
    "订阅",
    "支付",
    "付款",
    "充值",
    "续费",
    "分享",
    "发送",
    "升级",
    "buy",
    "purchase",
    "subscribe",
    "share",
    "send",
    "pay",
    "payment",
    "upgrade",
    "trial",
}

# 注意：不包含 TextView。课程词块/选项常是 TextView，文本可能是
# "订阅"/"buy" 等词；单词级拦截只用于真正的按钮/图标或 content-desc，
# 避免把正常答题词块误判为敏感操作。
_BUTTON_CLASS_HINTS = ("button", "imageview", "imagebutton")

# 输入类控件即使没有文本也值得保留
_INPUT_CLASS_HINTS = ("edittext", "textfield", "autocomplete", "searchview")


def _clean_text(value: object, max_length: int = 80) -> str:
    """清洗单段文本：移除控制字符、合并空白并截断。

    Args:
        value: 原始文本或 content-desc。
        max_length: 最大字符数。

    Returns:
        str: 清洗后的文本；空值时返回空字符串。
    """
    text = _CONTROL_CHARS_RE.sub("", str(value or ""))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if max_length > 0 and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def _short_class_name(value: str) -> str:
    """把完整类名转换为短类名（如 android.widget.TextView → TextView）。"""
    name = str(value or "").strip()
    return name.rsplit(".", 1)[-1] if name else "View"


def _parse_bounds(value: str) -> Optional[Tuple[int, int, int, int]]:
    """解析 UiAutomator bounds 字符串。

    Args:
        value: 形如 "[56,1422][180,1557]" 的字符串。

    Returns:
        Optional[Tuple[int, int, int, int]]: (x1, y1, x2, y2)；非法或零面积返回 None。
    """
    match = _BOUNDS_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    x1, y1, x2, y2 = (int(item) for item in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _danger_reason(
    text: str,
    content_desc: str,
    resource_id: str,
    class_name: str,
    ancestor_texts: Sequence[str],
) -> str:
    """判断节点是否属于购买/订阅/支付/分享等敏感操作。

    Args:
        text: 节点自身文本。
        content_desc: 节点内容描述。
        resource_id: 节点 resource-id。
        class_name: 节点短类名。
        ancestor_texts: 祖先节点的文本/描述（用于识别包裹按钮的容器）。

    Returns:
        str: 命中原因；安全时为空字符串。
    """
    combined = _clean_text(f"{text} {content_desc}", max_length=200).lower()
    for phrase in _DANGEROUS_PHRASES:
        if phrase in combined:
            return f"敏感操作文案: {phrase}"

    rid_lower = str(resource_id or "").lower()
    for keyword in _DANGEROUS_ID_KEYWORDS:
        if keyword in rid_lower:
            return f"敏感控件 ID: {keyword}"

    for ancestor_text in ancestor_texts:
        ancestor_lower = _clean_text(ancestor_text, max_length=200).lower()
        for phrase in _DANGEROUS_PHRASES:
            if phrase in ancestor_lower:
                return f"敏感容器文案: {phrase}"

    class_lower = class_name.lower()
    # 按钮/图标上的英文短语与单词级敏感文案（如“分享”“订阅”“Buy now”）
    if any(hint in class_lower for hint in _BUTTON_CLASS_HINTS):
        for phrase in _DANGEROUS_CONTROL_PHRASES:
            if phrase in combined:
                return f"敏感按钮文案: {phrase}"
        if combined in _DANGEROUS_SINGLE_WORDS:
            return f"敏感按钮文案: {combined}"
    # content-desc 是无障碍标签，英文短语与单词级敏感词也拦截
    desc_lower = _clean_text(content_desc, max_length=80).lower()
    for phrase in _DANGEROUS_CONTROL_PHRASES:
        if phrase in desc_lower:
            return f"敏感图标描述: {phrase}"
    if desc_lower in _DANGEROUS_SINGLE_WORDS:
        return f"敏感图标描述: {desc_lower}"
    return ""


def danger_reason_for_element(
    text: str,
    content_desc: str,
    resource_id: str,
    class_name: str,
    ancestor_texts: Sequence[str] = (),
) -> str:
    """公开的敏感控件判定入口。

    Appium / mobile-mcp 的静态动作执行层在拿到元素属性后调用本函数，
    与逐步 UI 驱动模式共用同一套购买/订阅/支付/分享控件规则。

    Args:
        text: 元素可见文本。
        content_desc: 元素 content-desc。
        resource_id: 元素 resource-id。
        class_name: 元素类名（完整或短类名均可）。
        ancestor_texts: 祖先节点文本（静态后端通常拿不到，可为空）。

    Returns:
        str: 命中原因；安全时为空字符串。
    """
    return _danger_reason(
        text,
        content_desc,
        resource_id,
        _short_class_name(class_name),
        ancestor_texts,
    )


@dataclass(frozen=True)
class UiNode:
    """一个可引用的 UI 节点。"""

    ref: int
    class_name: str
    text: str
    content_desc: str
    resource_id: str
    clickable: bool
    enabled: bool
    tappable: bool
    bounds: Tuple[int, int, int, int]
    dangerous: bool
    danger_reason: str
    password: bool
    selected: bool = False
    checked: bool = False

    @property
    def center(self) -> Tuple[int, int]:
        """返回节点中心坐标。"""
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def area(self) -> int:
        """返回节点面积，用于坐标命中时选择最内层节点。"""
        x1, y1, x2, y2 = self.bounds
        return (x2 - x1) * (y2 - y1)

    def short_label(self) -> str:
        """返回用于日志/历史的简短节点标签。"""
        parts: List[str] = [f"ref={self.ref}"]
        if self.text:
            parts.append(f'text="{self.text}"')
        elif self.content_desc:
            parts.append(f'desc="{self.content_desc}"')
        if self.resource_id:
            parts.append(f"id={self.resource_id.split('/')[-1]}")
        return " ".join(parts)


@dataclass
class UiSnapshot:
    """一次 UI 树快照。"""

    nodes: List[UiNode]
    total_candidates: int
    truncated: bool


@dataclass
class _RawNode:
    """解析过程中的原始节点信息。"""

    elem: ET.Element
    order: int
    class_name: str
    text: str
    content_desc: str
    resource_id: str
    clickable: bool
    enabled: bool
    package: str
    bounds: Tuple[int, int, int, int]
    password: bool
    selected: bool
    checked: bool
    candidate: bool


def parse_ui_nodes(
    xml: str,
    app_package: str = "",
    max_nodes: int = 80,
    max_text_length: int = 80,
) -> UiSnapshot:
    """把 Appium page_source 解析为裁剪后的 UI 节点列表。

    Args:
        xml: Appium 返回的当前页面 XML。
        app_package: 被测应用包名；非空时过滤其他应用/系统 UI 节点。
        max_nodes: 最多保留的节点数。
        max_text_length: 单段文本/描述的最大长度。

    Returns:
        UiSnapshot: 节点列表、候选总数与是否截断。
    """
    if not xml or not str(xml).strip():
        return UiSnapshot(nodes=[], total_candidates=0, truncated=False)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return UiSnapshot(nodes=[], total_candidates=0, truncated=False)

    parent_map: Dict[ET.Element, ET.Element] = {
        child: parent for parent in root.iter() for child in parent
    }
    raw_by_elem: Dict[ET.Element, _RawNode] = {}
    ordered: List[_RawNode] = []
    package_filter = (app_package or "").strip()

    # 兼容两种 page_source 格式：
    # 1. ADB/uiautomator dump 与旧版 Appium：<node class="..." bounds="...">
    # 2. 新版 Appium/UiAutomator2：<android.widget.FrameLayout class="..." bounds="...">
    #    元素标签就是完整类名，没有统一的 node 标签。
    # 因此不按标签名过滤，只接受带合法 bounds 的真实节点元素。
    for order, elem in enumerate(root.iter()):
        if elem is root:
            continue
        attrs = elem.attrib
        bounds = _parse_bounds(attrs.get("bounds", ""))
        if bounds is None:
            continue
        class_name = _short_class_name(attrs.get("class") or elem.tag)
        resource_id = (attrs.get("resource-id") or "").strip()
        text = _clean_text(attrs.get("text", ""), max_text_length)
        content_desc = _clean_text(attrs.get("content-desc", ""), max_text_length)
        clickable = str(attrs.get("clickable", "")).lower() == "true"
        enabled = str(attrs.get("enabled", "true")).lower() != "false"
        selected = str(attrs.get("selected", "")).lower() == "true"
        checked = str(attrs.get("checked", "")).lower() == "true"
        package = (attrs.get("package") or "").strip()
        password = (
            str(attrs.get("password", "")).lower() == "true"
            or any(
                keyword in resource_id.lower()
                for keyword in ("password", "passwd", "otp", "verification_code")
            )
        )
        if password:
            # 密码字段只保留“存在输入框”的信息，绝不把内容送进 prompt。
            text = "[密码字段已隐藏]"
            content_desc = "[密码字段已隐藏]"

        class_lower = class_name.lower()
        is_input = any(hint in class_lower for hint in _INPUT_CLASS_HINTS)
        candidate = bool(text or content_desc or clickable or is_input)
        package_allowed = not package_filter or not package or package == package_filter
        raw = _RawNode(
            elem=elem,
            order=order,
            class_name=class_name,
            text=text,
            content_desc=content_desc,
            resource_id=resource_id,
            clickable=clickable,
            enabled=enabled,
            package=package,
            bounds=bounds,
            password=password,
            selected=selected,
            checked=checked,
            candidate=candidate,
        )
        raw_by_elem[elem] = raw
        if candidate and package_allowed:
            ordered.append(raw)

    # 先保留所有可点击/输入控件，再用带文字的节点填充剩余名额；
    # 这样即使页面文本很多，也不会把底部按钮裁掉。
    must_keep = [
        item
        for item in ordered
        if item.clickable or any(hint in item.class_name.lower() for hint in _INPUT_CLASS_HINTS)
    ]
    others = [item for item in ordered if item not in must_keep]
    limit = max(1, int(max_nodes))
    selected = must_keep[:limit]
    if len(selected) < limit:
        selected.extend(others[: limit - len(selected)])
    selected.sort(key=lambda item: item.order)
    truncated = len(ordered) > len(selected)

    nodes: List[UiNode] = []
    for ref, raw in enumerate(selected):
        ancestors: List[_RawNode] = []
        parent = parent_map.get(raw.elem)
        while parent is not None:
            ancestor_raw = raw_by_elem.get(parent)
            if ancestor_raw is not None:
                ancestors.append(ancestor_raw)
            parent = parent_map.get(parent)
        tappable = raw.clickable or any(item.clickable for item in ancestors)
        ancestor_texts = [
            f"{item.text} {item.content_desc}".strip()
            for item in ancestors
            if item.text or item.content_desc
        ]
        reason = _danger_reason(
            raw.text,
            raw.content_desc,
            raw.resource_id,
            raw.class_name,
            ancestor_texts,
        )
        nodes.append(
            UiNode(
                ref=ref,
                class_name=raw.class_name,
                text=raw.text,
                content_desc=raw.content_desc,
                resource_id=raw.resource_id,
                clickable=raw.clickable,
                enabled=raw.enabled,
                tappable=tappable,
                bounds=raw.bounds,
                dangerous=bool(reason),
                danger_reason=reason,
                password=raw.password,
                selected=raw.selected,
                checked=raw.checked,
            )
        )
    return UiSnapshot(
        nodes=nodes,
        total_candidates=len(ordered),
        truncated=truncated,
    )


def format_ui_tree(
    snapshot: UiSnapshot,
    screen_width: int = 0,
    screen_height: int = 0,
) -> str:
    """把 UI 快照格式化为紧凑的文本，供 LLM 阅读。

    Args:
        snapshot: UI 快照。
        screen_width: 屏幕宽度（像素）；0 表示不输出。
        screen_height: 屏幕高度（像素）；0 表示不输出。

    Returns:
        str: 每行一个节点的文本树。
    """
    lines: List[str] = []
    if screen_width > 0 and screen_height > 0:
        lines.append(f"屏幕尺寸: {screen_width}x{screen_height}")
    lines.append(
        "字段说明: ref=引用编号; tappable=可点击(含可点击父级); "
        "danger=true 表示敏感操作，禁止点击"
    )
    for node in snapshot.nodes:
        parts = [f"[ref={node.ref}]", f"class={node.class_name}"]
        if node.resource_id:
            parts.append(f"id={node.resource_id}")
        if node.text:
            parts.append(f'text="{node.text}"')
        if node.content_desc:
            parts.append(f'desc="{node.content_desc}"')
        parts.append(f"clickable={'true' if node.clickable else 'false'}")
        parts.append(f"tappable={'true' if node.tappable else 'false'}")
        parts.append(f"enabled={'true' if node.enabled else 'false'}")
        if node.selected:
            parts.append("selected=true")
        if node.checked:
            parts.append("checked=true")
        x1, y1, x2, y2 = node.bounds
        parts.append(f"bounds=[{x1},{y1}][{x2},{y2}]")
        if node.dangerous:
            parts.append("danger=true")
        lines.append(" ".join(parts))
    if snapshot.truncated:
        lines.append(
            f"[注意] 当前页面候选节点共 {snapshot.total_candidates} 个，"
            f"已截断为 {len(snapshot.nodes)} 个；如需操作未显示控件，请先滚动或关闭弹窗。"
        )
    return "\n".join(lines)


def find_node_by_ref(nodes: Sequence[UiNode], ref: object) -> Optional[UiNode]:
    """按引用编号查找节点。

    Args:
        nodes: 当前 UI 快照节点列表。
        ref: 引用编号（int 或可转换为 int 的字符串）。

    Returns:
        Optional[UiNode]: 找到的节点；越界或格式非法返回 None。
    """
    try:
        index = int(ref)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if 0 <= index < len(nodes):
        return nodes[index]
    return None


def find_nodes_by_text(
    nodes: Sequence[UiNode],
    text: str,
    exact: bool = True,
) -> List[UiNode]:
    """按可见文本/content-desc 查找节点。

    Args:
        nodes: 当前 UI 快照节点列表。
        text: 目标文本。
        exact: True 为完整匹配（忽略大小写）；False 为包含匹配。

    Returns:
        List[UiNode]: 按文档顺序排列的匹配节点。
    """
    target = _clean_text(text, max_length=200).casefold()
    if not target:
        return []
    matches: List[UiNode] = []
    for node in nodes:
        candidates = [node.text, node.content_desc]
        for candidate in candidates:
            normalized = _clean_text(candidate, max_length=200).casefold()
            if not normalized:
                continue
            if (exact and normalized == target) or (not exact and target in normalized):
                matches.append(node)
                break
    return matches


def find_node_at(nodes: Sequence[UiNode], x: int, y: int) -> Optional[UiNode]:
    """查找坐标所在的最内层可点击节点。

    Args:
        nodes: 当前 UI 快照节点列表。
        x: 屏幕横坐标。
        y: 屏幕纵坐标。

    Returns:
        Optional[UiNode]: 命中的最内层可点击节点；未命中返回 None。
    """
    matches: List[UiNode] = []
    for node in nodes:
        x1, y1, x2, y2 = node.bounds
        if x1 <= x <= x2 and y1 <= y <= y2 and node.tappable:
            matches.append(node)
    if not matches:
        return None
    return min(matches, key=lambda node: (node.area, -node.ref))


def snapshot_contains_text(snapshot: UiSnapshot, text: str) -> bool:
    """判断文本是否真实出现在当前 UI 树中（用于校验 done 证据）。

    Args:
        snapshot: UI 快照。
        text: 待校验文本。

    Returns:
        bool: 文本出现在任一节点的 text/content-desc 中返回 True。
    """
    target = _clean_text(text, max_length=200).casefold()
    if not target:
        return False
    for node in snapshot.nodes:
        combined = _clean_text(
            f"{node.text} {node.content_desc}", max_length=200
        ).casefold()
        if target in combined:
            return True
    return False
