# -*- coding: utf-8 -*-
"""
LangGraph 工作流组装模块
将 6 个测试节点组装为状态图（StateGraph），形成完整的智能测试链路：

START
  → rag_retrieve（RAG 检索知识库）
  → generate_test_case（生成测试用例）
  → plan_actions（规划 Appium 动作）
  → execute_test（执行测试 + adb 抓取日志/截图）
      ↓ 条件分支：全部动作执行失败且重试额度未用完 → 回到 plan_actions 重新规划（最多 1 次）
  → reflect_and_detect_defect（缺陷分析）
  → build_report（生成 Markdown 报告）
END
"""

from langgraph.graph import END, START, StateGraph

from agent.nodes import (
    build_report,
    execute_test,
    generate_test_case,
    plan_actions,
    rag_retrieve,
    reflect_and_detect_defect,
)
from agent.state import AgentState
from utils.logger import logger

# 动作执行失败后允许重新规划的最大次数
MAX_PLAN_RETRY: int = 1


def route_after_execute(state: AgentState) -> str:
    """execute_test 节点后的条件路由函数。

    当已规划动作且全部执行失败、且重试额度未用完时，路由回 plan_actions
    重新调用 LLM 规划动作；否则进入缺陷分析节点。

    Args:
        state: LangGraph 共享状态（读取 action_list、execute_results、plan_retry_count）。

    Returns:
        str: 下一节点名称（"plan_actions" 或 "reflect_and_detect_defect"）。
    """
    action_list = state.get("action_list", [])
    results = state.get("execute_results", [])
    plan_retry_count = state.get("plan_retry_count", 0)

    all_failed = bool(action_list) and bool(results) and all(
        not r.get("success") for r in results
    )
    if all_failed and plan_retry_count < MAX_PLAN_RETRY:
        logger.warning(
            f"全部动作执行失败（{len(results)} 个），触发重试分支：重新规划动作"
            f"（已重试 {plan_retry_count}/{MAX_PLAN_RETRY} 次）"
        )
        return "plan_actions"
    return "reflect_and_detect_defect"


def build_test_agent_graph():
    """构建并编译智能测试 Agent 的 LangGraph 状态图。

    6 个节点按原有链路顺序串联，execute_test 后增加条件分支：
    全部动作执行失败时自动回退到 plan_actions 重规划（最多 MAX_PLAN_RETRY 次）。

    Returns:
        CompiledStateGraph: 编译完成的 LangGraph 可执行图对象。
    """
    # 以 AgentState 为共享状态构建状态图
    graph_builder = StateGraph(AgentState)

    # 注册 6 个节点
    graph_builder.add_node("rag_retrieve", rag_retrieve)
    graph_builder.add_node("generate_test_case", generate_test_case)
    graph_builder.add_node("plan_actions", plan_actions)
    graph_builder.add_node("execute_test", execute_test)
    graph_builder.add_node("reflect_and_detect_defect", reflect_and_detect_defect)
    graph_builder.add_node("build_report", build_report)

    # 串联主干链路：START 到 plan_actions
    graph_builder.add_edge(START, "rag_retrieve")
    graph_builder.add_edge("rag_retrieve", "generate_test_case")
    graph_builder.add_edge("generate_test_case", "plan_actions")
    graph_builder.add_edge("plan_actions", "execute_test")

    # execute_test 后条件分支：执行失败走重规划重试分支，否则进入缺陷分析
    graph_builder.add_conditional_edges(
        "execute_test",
        route_after_execute,
        {
            "plan_actions": "plan_actions",
            "reflect_and_detect_defect": "reflect_and_detect_defect",
        },
    )

    # 收尾链路：缺陷分析 → 报告生成 → END
    graph_builder.add_edge("reflect_and_detect_defect", "build_report")
    graph_builder.add_edge("build_report", END)

    graph = graph_builder.compile()
    logger.info(
        "LangGraph 智能测试工作流构建完成（含执行失败重规划条件分支，最多 "
        f"{MAX_PLAN_RETRY} 次）"
    )
    return graph


# 模块级编译一次，供 API 层直接复用
test_agent_graph = build_test_agent_graph()


def run_test_agent(
    requirement: str,
    qwen_api_key: str | None = None,
    deepseek_api_key: str | None = None,
    llm_provider: str | None = None,
    is_real_device: bool | None = None,
    real_udid: str | None = None,
) -> AgentState:
    """执行完整测试链路的入口函数。

    前端传入的密钥与配置覆盖值仅注入本次图执行的初始状态（内存使用），
    任务结束后随状态对象释放，不写入任何文件。

    Args:
        requirement: 用户输入的 App 测试需求文本。
        qwen_api_key: 前端传入的通义千问临时密钥（可选）。
        deepseek_api_key: 前端传入的 DeepSeek 临时密钥（可选）。
        llm_provider: 前端选择的模型覆盖值（"qwen"/"deepseek"，可选）。
        is_real_device: 前端选择的设备模式覆盖值（可选）。
        real_udid: 前端填写的真机 UDID 覆盖值（可选）。

    Returns:
        AgentState: 最终 AgentState（含测试用例、执行结果、缺陷列表、报告路径等）；
        图执行抛出异常时返回携带 error 字段的状态，保证调用方不收到未处理堆栈。
    """
    logger.info(f"启动智能测试 Agent，需求: {requirement[:80]}...")
    # 密钥不打印日志；状态字段仅在本次图执行周期内存活
    initial_state: AgentState = {
        "requirement": requirement,
        "user_qwen_key": (qwen_api_key or "").strip(),
        "user_deepseek_key": (deepseek_api_key or "").strip(),
        "user_provider": (llm_provider or "").strip().lower(),
        "user_is_real_device": is_real_device if isinstance(is_real_device, bool) else None,
        "user_udid": (real_udid or "").strip(),
    }
    try:
        final_state = test_agent_graph.invoke(initial_state)
        logger.info("智能测试 Agent 执行完成")
        return final_state
    except Exception as e:
        logger.error(f"智能测试 Agent 执行异常: {e}", exc_info=True)
        return {"requirement": requirement, "error": f"Agent 执行异常: {e}"}
