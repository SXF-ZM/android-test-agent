# -*- coding: utf-8 -*-
"""
测试报告工具模块
根据测试执行结果与缺陷列表，生成 Markdown 格式的安卓 App 缺陷测试报告。
报告文件使用时间戳命名，统一输出到 outputs/reports 目录（目录不存在自动创建），
并自动将 adb 截图文件路径写入报告附件区。
"""

import time
from pathlib import Path
from typing import Any, Dict, List

from config.settings import settings
from utils.logger import logger


def _build_execute_table(execute_results: List[Dict[str, Any]]) -> str:
    """将动作执行结果列表拼接为 Markdown 表格。

    Args:
        execute_results: Appium 动作执行结果列表。

    Returns:
        str: Markdown 表格文本；结果为空时返回提示文案。
    """
    if not execute_results:
        return "本轮无已执行动作（动作规划失败或 Appium 连接失败）。"
    lines = [
        "| 序号 | 动作 | 参数 | 执行结果 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, item in enumerate(execute_results, start=1):
        action = item.get("action", "-")
        params = str(item.get("params", {}))
        success = "✅ 成功" if item.get("success") else "❌ 失败"
        message = str(item.get("message", "-")).replace("\n", " ")
        lines.append(f"| {idx} | {action} | {params} | {success} | {message} |")
    return "\n".join(lines)


def _build_defect_section(defects: List[Dict[str, Any]]) -> str:
    """将缺陷列表拼接为 Markdown 缺陷详情区块。

    Args:
        defects: 缺陷分析节点输出的缺陷列表。

    Returns:
        str: Markdown 缺陷区块文本；缺陷为空时返回未发现提示。
    """
    if not defects:
        return "本轮测试未发现 Crash / ANR / UI 异常缺陷。"
    lines: List[str] = []
    for idx, defect in enumerate(defects, start=1):
        lines.append(
            f"### 缺陷 {idx}: {defect.get('type', '未知类型')}\n"
            f"- **严重级别**: {defect.get('severity', '未知')}\n"
            f"- **缺陷描述**: {defect.get('description', '无描述')}\n"
            f"- **日志证据**: `{defect.get('evidence', '无')}`\n"
            f"- **修复建议**: {defect.get('suggestion', '无')}\n"
        )
    return "\n".join(lines)


def generate_markdown_report(
    requirement: str,
    test_cases: str,
    execute_results: List[Dict[str, Any]],
    defects: List[Dict[str, Any]],
    log_path: str,
    screenshot_path: str,
) -> str:
    """生成 Markdown 测试缺陷报告并保存到本地文件。

    报告文件使用时间戳命名（test_report_YYYYmmdd_HHMMSS.md），统一输出到
    settings.report_output_dir 目录，目录不存在时自动创建；
    截图与日志文件路径自动写入报告附件区。

    Args:
        requirement: 用户输入的 App 测试需求。
        test_cases: LLM 生成的测试用例文本。
        execute_results: Appium 动作执行结果列表。
        defects: 缺陷分析节点输出的缺陷列表。
        log_path: adb logcat 日志文件路径。
        screenshot_path: adb 截图文件路径。

    Returns:
        str: 生成的报告文件绝对路径；失败返回空字符串。
    """
    try:
        # 报告目录不存在时自动创建
        report_dir = Path(settings.report_output_dir)
        report_dir.mkdir(parents=True, exist_ok=True)

        timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S")
        file_timestamp = time.strftime("%Y%m%d_%H%M%S")
        device_mode = "USB 真机" if settings.IS_REAL_DEVICE else "安卓模拟器"

        exec_table = _build_execute_table(execute_results)
        defect_section = _build_defect_section(defects)

        # ========== 截图附件区：路径 + 内嵌图片预览 ==========
        if screenshot_path:
            screenshot_section = (
                f"- 测试截图文件: `{screenshot_path}`\n\n"
                f"![测试截图]({Path(screenshot_path).as_uri()})"
            )
        else:
            screenshot_section = "- 测试截图文件: `未生成`"
        log_section = f"- logcat 日志文件: `{log_path or '未生成'}`"

        # ========== 组装完整 Markdown 报告 ==========
        report_content = f"""# 安卓 App 智能测试缺陷报告

## 一、测试基本信息

| 项目 | 内容 |
| --- | --- |
| 测试时间 | {timestamp_str} |
| 设备模式 | {device_mode} |
| 测试设备 | {settings.device_name} (Android {settings.android_platform_version}) |
| 被测应用包名 | {settings.app_package} |
| 被测应用 Activity | {settings.app_activity} |
| 测试执行方式 | LangGraph 智能测试 Agent（RAG + LLM + Appium + ADB） |

## 二、测试需求

{requirement}

## 三、生成的测试用例

{test_cases}

## 四、Appium 动作执行结果

{exec_table}

## 五、缺陷分析结果

{defect_section}

## 六、测试附件

{log_section}
{screenshot_section}

---
> 本报告由【面向安卓 App 的智能测试 Agent】自动生成。
"""
        report_file = report_dir / f"test_report_{file_timestamp}.md"
        report_file.write_text(report_content, encoding="utf-8")
        logger.info(f"Markdown 测试报告已生成: {report_file}")
        return str(report_file)
    except Exception as e:
        logger.error(f"生成测试报告失败: {e}", exc_info=True)
        return ""
