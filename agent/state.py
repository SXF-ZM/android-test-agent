# -*- coding: utf-8 -*-
"""
LangGraph Agent 状态定义模块
使用 TypedDict 定义贯穿整个测试工作流的共享状态 AgentState，
各节点读取并增量更新该状态，实现测试数据的流转。
"""

from typing import Any, Dict, List, TypedDict


class AgentState(TypedDict, total=False):
    """
    智能测试 Agent 全局状态

    字段说明：
    - requirement: 用户输入的 App 测试需求文本
    - rag_context: RAG 检索到的知识库上下文片段列表
    - test_cases: LLM 生成的安卓 App 测试用例文本
    - action_list: LLM 规划出的 Appium 动作列表（JSON 格式，含 action 与 params）
    - execute_results: 每个动作的执行结果列表（success / message）
    - adb_log_path: adb 抓取的 logcat 日志本地路径
    - screenshot_path: adb 截图本地路径
    - adb_log_content: 日志文本内容（供 LLM 缺陷分析使用）
    - defects: 缺陷分析节点输出的缺陷列表
    - report_path: 最终生成的 Markdown 测试报告路径
    - error: 流程中的错误信息（用于排查与接口返回）
    - plan_retry_count: 动作重试分支已执行的重新规划次数（条件边据此控制最多重试 1 次）
    - user_qwen_key: 前端传入的通义千问临时密钥（仅本次图执行内存使用，任务结束销毁，不落盘）
    - user_deepseek_key: 前端传入的 DeepSeek 临时密钥（仅本次图执行内存使用，任务结束销毁，不落盘）
    - user_provider: 前端选择的 LLM Provider 覆盖值（qwen/deepseek，空表示用 .env 配置）
    - user_is_real_device: 前端选择的设备模式覆盖值（None 表示用 .env 配置）
    - user_udid: 前端填写的真机 UDID 覆盖值（真机模式下生效）
    """

    requirement: str
    rag_context: List[str]
    test_cases: str
    action_list: List[Dict[str, Any]]
    execute_results: List[Dict[str, Any]]
    adb_log_path: str
    screenshot_path: str
    adb_log_content: str
    defects: List[Dict[str, Any]]
    report_path: str
    error: str
    plan_retry_count: int
    user_qwen_key: str
    user_deepseek_key: str
    user_provider: str
    user_is_real_device: bool
    user_udid: str
