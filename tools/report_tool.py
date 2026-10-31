# -*- coding: utf-8 -*-
"""
测试报告工具模块
根据测试执行结果与缺陷列表，生成 Markdown 格式的安卓 App 缺陷测试报告。
报告默认按任务 run_id 命名并写入 outputs/runs/<run_id>/reports 目录，
未提供 run_id 时回退为时间戳 + 随机后缀；目录不存在自动创建。
报告自动写入全部执行尝试、异常诊断、adb 日志与截图路径。
"""

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import logger
from utils.redaction import redact_action_params


def _build_execute_table(execute_results: List[Dict[str, Any]]) -> str:
    """将动作执行结果列表拼接为 Markdown 表格。

    参数在写入报告前再次脱敏（幂等），防止调用方传入未脱敏的
    ``input_text`` 内容。

    Args:
        execute_results: 动作执行结果列表。

    Returns:
        str: Markdown 表格文本；结果为空时返回提示文案。
    """
    if not execute_results:
        return "本轮无已执行动作（动作规划失败或执行后端连接失败）。"
    lines = [
        "| 序号 | 动作 | 参数 | 执行结果 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, item in enumerate(execute_results, start=1):
        action = item.get("action", "-")
        params = str(redact_action_params(action, item.get("params", {})))
        success = "✅ 成功" if item.get("success") else "❌ 失败"
        message = str(item.get("message", "-")).replace("\n", " ")
        lines.append(f"| {idx} | {action} | {params} | {success} | {message} |")
    return "\n".join(lines)


def _build_attempts_block(execute_attempts: List[Dict[str, Any]]) -> str:
    """把多次执行尝试拼接为报告区块。

    首次执行失败后 LangGraph 会重新规划并再次执行，旧实现只用第二次
    的结果覆盖第一次，报告会丢失失败轨迹。这里按尝试顺序全部展示。

    Args:
        execute_attempts: execute_test 累积的执行尝试列表。

    Returns:
        str: Markdown 文本；单次尝试时只输出结果表。
    """
    if len(execute_attempts) <= 1:
        attempt = execute_attempts[0] if execute_attempts else {}
        error = str(attempt.get("error") or "").strip()
        prefix = f"> 本次尝试异常：{error}\n\n" if error else ""
        return prefix + _build_execute_table(attempt.get("results", []))

    lines: List[str] = []
    for item in execute_attempts:
        attempt_no = item.get("attempt", "?")
        mode = (
            "逐步 UI 驱动"
            if item.get("mode") == "stepwise"
            else "静态动作规划"
        )
        lines.append(f"### 第 {attempt_no} 次尝试（{mode}）")
        error = str(item.get("error") or "").strip()
        if error:
            lines.append(f"> 本次尝试异常：{error}")
            lines.append("")
        lines.append(_build_execute_table(item.get("results", [])))
        lines.append("")
    return "\n".join(lines).strip()


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
    device_backend: str = "",
    device_mode: str = "",
    device_name: str = "",
    device_platform: str = "",
    app_package: str = "",
    app_activity: str = "",
    stepwise_mode: bool = False,
    goal_achieved: Optional[bool] = None,
    goal_evidence: str = "",
    execute_attempts: Optional[List[Dict[str, Any]]] = None,
    error: str = "",
    output_dir: str = "",
    run_id: str = "",
) -> str:
    """生成 Markdown 测试缺陷报告并保存到本地文件。

    报告文件优先使用 run_id 命名并写入单次任务报告目录；未提供时回退到
    时间戳 + 随机后缀。截图与日志文件路径自动写入报告附件区。

    Args:
        requirement: 用户输入的 App 测试需求。
        test_cases: LLM 生成的测试用例文本。
        execute_results: 动作执行结果列表。
        defects: 缺陷分析节点输出的缺陷列表。
        log_path: adb logcat 日志文件路径。
        screenshot_path: adb 截图文件路径。
        device_backend: 本次执行后端（appium/mobile_mcp）。
        device_mode: 本次实际设备模式；为空时回退 .env 配置。
        device_name: 本次实际设备名称/序列号；为空时回退 .env 配置。
        device_platform: 本次实际安卓版本；真机可为空。
        app_package: 本次实际被测应用包名；为空时回退 .env 配置。
        app_activity: 本次实际启动 Activity；为空时回退 .env 配置。
        stepwise_mode: 本次是否采用逐步 UI 驱动模式。
        goal_achieved: 逐步模式下整体目标是否达成；None 表示未判定。
        goal_evidence: 目标达成/阻断的 UI 证据文本。
        execute_attempts: 全部执行尝试记录；用于展示重规划前后的完整轨迹。
        error: 流程级异常信息（设备连接失败等），写入报告诊断区。
        output_dir: 报告保存目录；为空时使用 settings.report_output_dir。
        run_id: 本次任务 ID；用于报告命名与基本信息展示。

    Returns:
        str: 生成的报告文件绝对路径；失败返回空字符串。
    """
    try:
        # 报告目录不存在时自动创建
        report_dir = Path(output_dir or settings.report_output_dir)
        report_dir.mkdir(parents=True, exist_ok=True)

        timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S")
        file_timestamp = time.strftime("%Y%m%d_%H%M%S")
        effective_mode = device_mode or (
            "USB 真机" if settings.IS_REAL_DEVICE else "安卓模拟器"
        )
        effective_name = device_name or settings.device_name
        effective_platform = device_platform or (
            "" if settings.IS_REAL_DEVICE else settings.android_platform_version
        )
        backend_label = (
            "mobile-mcp（MCP）" if device_backend == "mobile_mcp" else "Appium"
        )
        device_line = effective_name + (
            f" (Android {effective_platform})" if effective_platform else ""
        )
        effective_app_package = (app_package or "").strip() or settings.app_package
        effective_app_activity = (app_activity or "").strip() or settings.app_activity

        attempts = list(execute_attempts or [])
        if attempts:
            exec_section = _build_attempts_block(attempts)
        else:
            exec_section = _build_execute_table(execute_results)
        attempt_count = len(attempts) if attempts else 1
        defect_section = _build_defect_section(defects)

        # 整体目标结论：区分“没有 Crash”与“用户目标真正达成”
        mode_label = "逐步 UI 驱动" if stepwise_mode else "静态动作规划"
        if not stepwise_mode:
            verdict = "未判定（静态模式未做整体目标校验）"
            verdict_evidence = "—"
        elif goal_achieved is True:
            verdict = "✅ 达成"
            verdict_evidence = goal_evidence or "（Agent 未提供文字证据）"
        elif goal_achieved is False:
            verdict = "❌ 未达成"
            verdict_evidence = goal_evidence or "（未提供阻断证据）"
        else:
            verdict = "未判定"
            verdict_evidence = goal_evidence or "—"
        # Markdown 表格中的竖线与换行会破坏排版，先转义
        verdict_evidence = str(verdict_evidence).replace("|", "\\|").replace("\n", " ")

        # ========== 截图附件区：路径 + 内嵌图片预览 ==========
        if screenshot_path:
            screenshot_section = (
                f"- 测试截图文件: `{screenshot_path}`\n\n"
                f"![测试截图]({Path(screenshot_path).as_uri()})"
            )
        else:
            screenshot_section = "- 测试截图文件: `未生成`"
        log_section = f"- logcat 日志文件: `{log_path or '未生成'}`"

        run_id_row = f"| 任务 ID | {run_id} |\n" if run_id else ""
        error_text = str(error or "").strip() or "本轮流程未记录异常。"

        # ========== 组装完整 Markdown 报告 ==========
        report_content = f"""# 安卓 App 智能测试缺陷报告

## 一、测试基本信息

| 项目 | 内容 |
| --- | --- |
| 测试时间 | {timestamp_str} |
{run_id_row}| 设备模式 | {effective_mode} |
| 测试设备 | {device_line} |
| 执行后端 | {backend_label} |
| 被测应用包名 | {effective_app_package} |
| 被测应用 Activity | {effective_app_activity} |
| 测试执行方式 | LangGraph 智能测试 Agent（RAG + LLM + {backend_label} + ADB，{mode_label}） |

## 二、测试需求

{requirement}

## 三、生成的测试用例

{test_cases}

## 四、执行结论

| 项目 | 内容 |
| --- | --- |
| 执行模式 | {mode_label} |
| 执行尝试次数 | {attempt_count} 次 |
| 目标是否达成 | {verdict} |
| 达成/阻断证据 | {verdict_evidence} |

> 说明：本结论针对用户需求中的整体目标；“未发现 Crash/ANR”不等于目标已达成。

## 五、动作执行结果

{exec_section}

## 六、缺陷分析结果

{defect_section}

## 七、异常与诊断

{error_text}

## 八、测试附件

{log_section}
{screenshot_section}

---
> 本报告由【面向安卓 App 的智能测试 Agent】自动生成。
"""
        if run_id:
            report_file = report_dir / f"test_report_{run_id}.md"
        else:
            report_file = report_dir / f"test_report_{file_timestamp}_{uuid.uuid4().hex[:6]}.md"
        report_file.write_text(report_content, encoding="utf-8")
        logger.info(f"Markdown 测试报告已生成: {report_file}")
        return str(report_file)
    except Exception as e:
        logger.error(f"生成测试报告失败: {e}", exc_info=True)
        return ""
