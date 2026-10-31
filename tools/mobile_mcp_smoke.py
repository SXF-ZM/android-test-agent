# -*- coding: utf-8 -*-
"""
mobile-mcp 后端冒烟脚本（不依赖 LLM / Appium）

用途：
在接入完整 LangGraph 流程前，先单独验证 Node.js/npx、设备连接、
MCP 握手、元素树解析与点击/输入映射是否正常。

使用方式（项目根目录）：
    python -m tools.mobile_mcp_smoke --device emulator-5554
    python -m tools.mobile_mcp_smoke --click-id com.android.settings:id/search
    python -m tools.mobile_mcp_smoke --input-id com.android.settings:id/search --text wifi

退出码：0 表示连接与元素读取成功；1 表示失败。
"""

import argparse
import json
import sys
import time
from typing import Any, Dict, List

from tools.mobile_mcp_tool import MobileMcpTool


def _print_elements(elements: List[Dict[str, Any]], as_json: bool) -> None:
    """打印元素列表摘要或完整 JSON。"""
    print(f"发现 {len(elements)} 个元素")
    if as_json:
        print(json.dumps(elements, ensure_ascii=False, indent=2, default=str))
        return
    for index, element in enumerate(elements[:50], start=1):
        text = (
            element.get("text")
            or element.get("label")
            or element.get("content-desc")
            or element.get("contentDescription")
            or element.get("value")
            or ""
        )
        identifier = (
            element.get("identifier")
            or element.get("resource-id")
            or element.get("resourceId")
            or ""
        )
        bounds = element.get("bounds") or element.get("rect") or ""
        ref = element.get("ref", "")
        print(f"{index:>3}. ref={ref!s:<6} id={identifier!s:<45} text={text!s:<30} bounds={bounds}")
    if len(elements) > 50:
        print(f"... 其余 {len(elements) - 50} 个元素已省略（使用 --json 查看全部）")


def main() -> int:
    """解析命令行参数并执行连接/元素读取/可选动作。"""
    parser = argparse.ArgumentParser(description="mobile-mcp 执行后端冒烟测试")
    parser.add_argument("--device", default="", help="mobile-mcp 设备标识（默认取 .env 设备）")
    parser.add_argument("--click-id", default="", help="测试点击的 resource-id")
    parser.add_argument("--input-id", default="", help="测试输入的 resource-id")
    parser.add_argument("--text", default="hello", help="测试输入文本")
    parser.add_argument("--timeout", type=int, default=10, help="元素等待超时（秒）")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印完整元素列表")
    args = parser.parse_args()

    tool = MobileMcpTool(device_id=args.device or None)
    print(f"启动 mobile-mcp: {tool.describe()}")
    if not tool.connect():
        print("mobile-mcp 连接失败，请查看上方日志", file=sys.stderr)
        return 1

    try:
        devices = tool.call_tool("mobile_list_available_devices", {})
        if devices.get("success"):
            print(f"可用设备:\n{devices.get('text', '')}")

        elements = tool.list_elements()
        _print_elements(elements, args.json)

        if args.click_id:
            print(f"\n[测试点击] {args.click_id}")
            result = tool.click_by_id(args.click_id, timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=False, default=str))
            time.sleep(1)
            if not result.get("success"):
                return 1

        if args.input_id:
            print(f"\n[测试输入] {args.input_id} <- {args.text!r}")
            result = tool.input_text(args.input_id, args.text, timeout=args.timeout)
            print(json.dumps(result, ensure_ascii=False, default=str))
            time.sleep(1)
            _print_elements(tool.list_elements(), args.json)
            if not result.get("success"):
                return 1

        return 0
    finally:
        tool.close()


if __name__ == "__main__":
    raise SystemExit(main())
