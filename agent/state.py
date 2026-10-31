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
    - action_list: LLM 规划出的设备动作列表（JSON 格式，含 action 与 params，
      由 Appium 或 mobile-mcp 后端执行）
    - execute_results: 最近一次执行尝试的动作结果列表（success / message），
      条件边据此判断是否需要重新规划
    - execute_attempts: 所有执行尝试的累积记录（含首次失败与重规划后的结果），
      供报告展示完整执行轨迹
    - run_id: 本次测试任务的唯一 ID，用于隔离 outputs/runs/<run_id> 产物目录
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
    - user_device_backend: 前端选择的执行后端覆盖值（appium/mobile_mcp，空表示用 .env 配置）
    - user_app_package: 前端填写的被测应用包名覆盖值（空表示用 .env 配置）
    - user_app_activity: 前端填写的启动 Activity 覆盖值（空表示用 .env 配置）
    - app_package: 本次任务实际生效的被测应用包名
    - app_activity: 本次任务实际生效的启动 Activity
    - device_backend: 本次任务实际生效的执行后端
    - device_mode: 本次任务实际使用的设备模式描述（安卓模拟器/USB 真机）
    - device_name: 本次任务实际使用的设备序列号/名称
    - device_platform: 本次任务实际使用的安卓平台版本（真机为空）
    - user_stepwise_agent: 前端/接口传入的逐步 UI 驱动开关覆盖值
      （None 表示使用 .env 的 STEPWISE_AGENT_ENABLED）
    - stepwise_mode: 本次任务实际是否采用逐步 UI 驱动模式
    - stepwise_steps: 逐步模式实际执行的动作步数
    - goal_achieved: 逐步模式下整体目标是否达成
    - goal_evidence: 目标达成的 UI 证据文本（仅来自当前页面 UI 树）
    """

    requirement: str
    rag_context: List[str]
    test_cases: str
    action_list: List[Dict[str, Any]]
    execute_results: List[Dict[str, Any]]
    execute_attempts: List[Dict[str, Any]]
    run_id: str
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
    user_device_backend: str
    user_app_package: str
    user_app_activity: str
    app_package: str
    app_activity: str
    device_backend: str
    device_mode: str
    device_name: str
    device_platform: str
    user_stepwise_agent: bool
    stepwise_mode: bool
    stepwise_steps: int
    goal_achieved: bool
    goal_evidence: str
