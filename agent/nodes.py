# -*- coding: utf-8 -*-
"""
LangGraph 节点模块
定义智能测试 Agent 工作流的 6 个核心节点：
1. rag_retrieve            —— RAG 检索测试知识库
2. generate_test_case      —— LLM 结合检索上下文生成安卓 App 测试用例
3. plan_actions            —— LLM 将测试用例转为 JSON 格式的 Appium 动作（解析失败自动重试 1 次）
4. execute_test            —— 解析 JSON 动作，调用 Appium 执行，adb 抓取日志与截图
5. reflect_and_detect_defect —— LLM 分析 adb 日志，结合关键字扫描识别 Crash/ANR/UI 异常
6. build_report            —— 调用报告工具生成 Markdown 缺陷测试报告

LLM 通过工厂函数 get_llm() 按配置自动实例化：支持通义千问（qwen）/
DeepSeek（deepseek）一键切换（.env 的 LLM_PROVIDER），均为 OpenAI 兼容接口。

前置条件（重要）：
1. 需预先启动 Appium 服务（默认 http://127.0.0.1:4723）
2. 需预先启动安卓模拟器，或连接真机并开启 USB 调试
"""

import json
import re
import time
from typing import Any, Dict, List, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent.state import AgentState
from config.settings import settings
from rag.vector_store import similarity_search
from tools import adb_tool, report_tool
from tools.appium_tool import AppiumTool
from utils.logger import logger

# 动作规划 JSON 解析失败时的自动重试次数
PLAN_PARSE_MAX_RETRY: int = 1

# logcat 中疑似缺陷的关键字（Crash / ANR / 进程异常），用于辅助 LLM 判断、降低幻觉
LOG_DEFECT_KEYWORDS: List[str] = [
    "FATAL EXCEPTION",
    "ANR in",
    "Input dispatching timed out",
    "AndroidRuntime",
    "Force finishing activity",
    "beginning of crash",
    "has died",
    "Signal ",
]

# 宽松关键字集合：仅在 W/E/F 级别日志行中生效，
# 避免匹配到 INFO/DEBUG 级别的正常启动日志（如 "AndroidRuntime: Using default boot image"）造成误报
_LEVEL_FILTERED_KEYWORDS: set = {"AndroidRuntime", "has died", "Signal ", "Force finishing activity"}

# 动作结果字典类型别名
ActionResult = Dict[str, Any]


def _resolve_adb_serial(user_is_real: Any, user_udid: str | None) -> str:
    """解析本次任务 adb 抓日志/截屏的目标设备序列号。

    与 AppiumTool 的设备覆盖逻辑保持一致，确保 Appium 操作与 adb 采集
    始终作用于同一台设备（多设备在线时避免误操作 .env 默认设备）。

    Args:
        user_is_real: 前端设备模式覆盖值（True/False/None 表示使用 .env 配置）。
        user_udid: 前端真机 UDID 覆盖值。

    Returns:
        str: adb -s 目标设备序列号。
    """
    if user_is_real is True:
        return user_udid or settings.REAL_PHONE_UDID or settings.adb_device_serial
    if user_is_real is False:
        return settings.EMULATOR_DEVICE_NAME
    return settings.adb_device_serial


# ==================== LLM 客户端工厂（支持前端临时密钥与 provider 覆盖） ====================
# 缓存 key 规则：".env" 配置实例 → "settings:<provider>"；前端临时密钥实例不缓存，
# 任务结束即随引用释放，密钥不写入任何文件
_llm_cache: Dict[str, ChatOpenAI] = {}


def _mask_key(key: str) -> str:
    """密钥脱敏：仅保留前 4 位用于调试日志，禁止输出完整密钥。

    Args:
        key: 原始 API 密钥字符串。

    Returns:
        str: 脱敏后的密钥（如 "sk-1***"）；空密钥返回 "(未配置)"。
    """
    if not key:
        return "(未配置)"
    return key[:4] + "***"


def _provider_llm_params(provider: str) -> Tuple[str, str, str]:
    """获取指定 provider 的连接参数（地址、模型名、.env 配置密钥）。

    Args:
        provider: 规范化 provider 名称（"qwen" 或 "deepseek"）。

    Returns:
        Tuple[str, str, str]: (base_url, model_name, settings_api_key)。
    """
    if provider == "deepseek":
        return settings.deepseek_base_url, settings.deepseek_model_name, settings.deepseek_api_key
    return settings.llm_base_url, settings.llm_model_name, settings.llm_api_key


def _resolve_provider(state: "AgentState") -> str:
    """解析本次任务生效的 LLM Provider。

    优先级：前端 state 中的 user_provider 覆盖值 > .env 的 LLM_PROVIDER 配置。

    Args:
        state: 当前 Agent 状态。

    Returns:
        str: 规范化 provider 名称（"qwen" 或 "deepseek"）。
    """
    override = (state.get("user_provider") or "").strip().lower()
    if override in ("qwen", "deepseek"):
        return override
    return settings.validate_llm_provider()


def _resolve_user_key(state: "AgentState") -> str | None:
    """从 state 中提取当前 provider 对应的前端临时密钥。

    Args:
        state: 当前 Agent 状态。

    Returns:
        str | None: 前端传入的非空密钥；未提供时返回 None。
    """
    provider = _resolve_provider(state)
    raw = state.get("user_deepseek_key") if provider == "deepseek" else state.get("user_qwen_key")
    key = (raw or "").strip()
    return key or None


def get_llm(user_api_key: str | None = None, provider: str | None = None) -> ChatOpenAI:
    """LLM 工厂函数：按 provider 实例化对应模型客户端，支持前端临时密钥。

    优先级：传入的 user_api_key（前端临时密钥，新建实例不缓存、任务结束释放）
    > .env 配置密钥（按 provider 缓存单例复用）。

    Args:
        user_api_key: 前端传入的临时 API 密钥；为空则回退 .env 配置。
        provider: 生效 provider；为空时取 .env 的 LLM_PROVIDER。

    Returns:
        ChatOpenAI: LLM 客户端实例。
    """
    effective_provider = provider or settings.validate_llm_provider()
    base_url, model_name, settings_key = _provider_llm_params(effective_provider)

    # 前端临时密钥：每次新建实例，不缓存，任务执行完毕引用释放即销毁
    if user_api_key:
        logger.info(
            f"使用前端传入密钥实例化 LLM（provider={effective_provider}, model={model_name}, "
            f"key={_mask_key(user_api_key)}），仅本次任务内存使用"
        )
        return ChatOpenAI(
            model=model_name,
            api_key=user_api_key,
            base_url=base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )

    # .env 配置密钥：按 provider 缓存单例
    cache_key = f"settings:{effective_provider}"
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    logger.info(f"初始化 LLM 客户端: provider={effective_provider}, model={model_name} @ {base_url}")
    llm = ChatOpenAI(
        model=model_name,
        api_key=settings_key,
        base_url=base_url,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    _llm_cache[cache_key] = llm
    return llm


def _is_llm_configured(user_api_key: str | None = None, provider: str | None = None) -> bool:
    """检查本次任务可用的 LLM API Key 是否已正确配置。

    前端传入临时密钥视为已配置；否则检查 .env 中对应 provider 的密钥。

    Args:
        user_api_key: 前端传入的临时 API 密钥。
        provider: 生效 provider；为空时取 .env 的 LLM_PROVIDER。

    Returns:
        bool: 有可用密钥且不是占位符时返回 True。
    """
    if user_api_key:
        return True
    effective_provider = provider or settings.validate_llm_provider()
    env_var = "DEEPSEEK_API_KEY" if effective_provider == "deepseek" else "LLM_API_KEY"
    _, _, key = _provider_llm_params(effective_provider)
    key = key.strip()
    if not key:
        logger.error(
            f"LLM API Key 未配置（provider={effective_provider}），"
            f"请在前端页面填写密钥，或在 .env 中设置 {env_var}"
        )
        return False
    if key.startswith("sk-请替换") or key.startswith("sk-xxxx"):
        logger.error(
            f"LLM API Key 仍为占位符（provider={effective_provider}），"
            f"请在前端页面填写密钥，或在 .env 中配置真实 {env_var}"
        )
        return False
    return True


def _invoke_llm(
    system_prompt: str,
    user_prompt: str,
    user_api_key: str | None = None,
    provider: str | None = None,
) -> str:
    """调用 LLM 的通用封装，返回模型输出文本。

    Args:
        system_prompt: 系统提示词。
        user_prompt: 用户提示词。
        user_api_key: 前端传入的临时 API 密钥；为空则使用 .env 配置。
        provider: 生效 provider；为空时取 .env 的 LLM_PROVIDER。

    Returns:
        str: LLM 输出文本；未配置 Key 或调用异常时返回空字符串。
    """
    if not _is_llm_configured(user_api_key, provider):
        return ""
    try:
        response = get_llm(user_api_key, provider).invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        return str(response.content).strip()
    except Exception as e:
        logger.error(f"LLM 调用失败: {e}", exc_info=True)
        return ""

def _extract_json_array(raw_output: str) -> Tuple[List[Any], str]:
    """从 LLM 原始输出中提取并解析第一个 JSON 数组。

    兼容模型误加 markdown 代码块（```json ... ```）包裹的情况。

    Args:
        raw_output: LLM 原始输出文本。

    Returns:
        Tuple[List[Any], str]: (解析结果列表, 错误描述)。
        成功时错误描述为空字符串；失败时列表为空。
    """
    if not raw_output:
        return [], "LLM 输出为空"
    try:
        # 去除可能的 markdown 代码块包裹
        cleaned = re.sub(r"```(?:json)?|```", "", raw_output).strip()
        # 提取第一个 JSON 数组片段
        match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
        if not match:
            return [], "输出中未找到 JSON 数组结构"
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, list):
            return [], "JSON 解析结果不是数组"
        return parsed, ""
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"JSON 数组提取解析异常: {e}")
        return [], f"JSON 解析异常: {e}"


# ==================== 节点 1：RAG 检索 ====================
def rag_retrieve(state: AgentState) -> AgentState:
    """节点1：根据用户测试需求，从 Chroma 知识库检索 top3 相关测试知识片段。

    Args:
        state: LangGraph 共享状态（读取 requirement）。

    Returns:
        AgentState: 状态增量更新（写入 rag_context）。
    """
    logger.info("===== 节点1: RAG 检索测试知识库 =====")
    requirement = state.get("requirement", "")
    rag_context = similarity_search(requirement)
    if not rag_context:
        logger.warning("知识库未检索到相关内容，将以空上下文继续后续流程")
    return {"rag_context": rag_context}


# ==================== 节点 2：生成测试用例 ====================
def generate_test_case(state: AgentState) -> AgentState:
    """节点2：LLM 结合 RAG 检索上下文，生成安卓 App 测试用例。

    Args:
        state: LangGraph 共享状态（读取 requirement 与 rag_context）。

    Returns:
        AgentState: 状态增量更新（写入 test_cases；失败时写入 error）。
    """
    logger.info("===== 节点2: 生成安卓 App 测试用例 =====")
    requirement = state.get("requirement", "")
    rag_context = state.get("rag_context", [])
    context_text = "\n\n".join(
        f"[知识片段{i}]:\n{chunk}" for i, chunk in enumerate(rag_context, start=1)
    ) or "（知识库暂无相关内容）"

    system_prompt = (
        "你是一名资深安卓 App 测试工程师，服务于小米手机应用测试团队。"
        "请结合给定的测试知识库上下文与用户需求，编写结构清晰、可执行的安卓 App 测试用例。"
        "每个测试用例需包含：用例编号、用例标题、前置条件、测试步骤、预期结果。"
    )
    user_prompt = (
        f"【测试知识库上下文】\n{context_text}\n\n"
        f"【用户测试需求】\n{requirement}\n\n"
        f"【被测应用】包名: {settings.app_package}, 启动Activity: {settings.app_activity}\n"
        "请输出 3~5 条测试用例。"
    )

    test_cases = _invoke_llm(system_prompt, user_prompt, _resolve_user_key(state), _resolve_provider(state))
    if not test_cases:
        test_cases = "LLM 生成测试用例失败，请检查大模型配置与网络。"
        return {"test_cases": test_cases, "error": test_cases}
    logger.info("测试用例生成完成")
    return {"test_cases": test_cases}


# ==================== 节点 3：规划 Appium 动作 ====================
def plan_actions(state: AgentState) -> AgentState:
    """节点3：LLM 将测试用例转换为 JSON 格式的 Appium 动作列表。

    支持的动作类型：
    - click_by_id: 点击元素，params: {"resource_id": "xxx"}
    - input_text:  输入文本，params: {"resource_id": "xxx", "text": "xxx"}
    - sleep:       等待，params: {"seconds": 2}

    解析失败时自动重试 PLAN_PARSE_MAX_RETRY 次，重试 prompt 附带上次
    错误提示引导 LLM 修正输出格式；若为执行失败触发的重规划（重试分支），
    还会携带上一轮失败动作摘要帮助 LLM 调整动作。

    Args:
        state: LangGraph 共享状态（读取 test_cases、execute_results、plan_retry_count）。

    Returns:
        AgentState: 状态增量更新（写入 action_list、plan_retry_count；失败时写入 error）。
    """
    logger.info("===== 节点3: 规划 Appium 执行动作 =====")
    test_cases = state.get("test_cases", "")
    plan_retry_count = state.get("plan_retry_count", 0)
    # 存在上一轮执行结果说明当前是执行失败后的重规划分支
    is_replan = bool(state.get("execute_results"))

    system_prompt = (
        "你是一名安卓 UI 自动化测试专家。请将测试用例转换为 Appium 可执行的动作序列。"
        "严格输出 JSON 数组，不要输出任何其他文字或 markdown 代码块标记。"
        "每个动作对象包含 action 与 params 两个字段，仅支持以下三种动作：\n"
        '1. {"action": "click_by_id", "params": {"resource_id": "元素resource-id"}}\n'
        '2. {"action": "input_text", "params": {"resource_id": "输入框resource-id", "text": "输入内容"}}\n'
        '3. {"action": "sleep", "params": {"seconds": 2}}\n'
        f"被测应用包名为 {settings.app_package}，resource-id 一般采用 \"包名:id/xxx\" 格式。"
    )
    user_prompt = f"【测试用例】\n{test_cases}\n\n请输出对应的 Appium 动作 JSON 数组。"

    if is_replan:
        # 重试分支：附带上一轮失败上下文，引导 LLM 调整动作序列
        failed_summary = _summarize_failed_actions(state.get("execute_results", []))
        user_prompt += (
            f"\n\n【重要提示】上一轮规划的动作全部执行失败，失败情况如下：\n{failed_summary}\n"
            "请调整 resource-id 或动作序列（例如增加 sleep 等待页面加载、"
            "改用更通用的 resource-id）后重新输出完整 JSON 数组。"
        )

    # 本次任务生效的 LLM 密钥与 provider（前端临时密钥优先，.env 兜底）
    llm_key = _resolve_user_key(state)
    llm_provider = _resolve_provider(state)

    # 首次规划 + 解析失败自动重试（最多 PLAN_PARSE_MAX_RETRY 次）
    action_list: List[Dict[str, Any]] = []
    parse_error = ""
    for attempt in range(PLAN_PARSE_MAX_RETRY + 1):
        raw_output = _invoke_llm(system_prompt, user_prompt, llm_key, llm_provider)
        action_list, parse_error = _parse_actions_json(raw_output)
        if action_list:
            break
        if attempt < PLAN_PARSE_MAX_RETRY:
            logger.warning(
                f"动作 JSON 第 {attempt + 1} 次解析失败（错误: {parse_error}），"
                f"附带错误提示重试（剩余 {PLAN_PARSE_MAX_RETRY - attempt} 次）"
            )
            user_prompt += (
                f"\n\n【格式错误，必须修正】你上一次的输出无法解析为合法 JSON 动作数组，"
                f"错误原因: {parse_error}。"
                "请重新输出：一个纯 JSON 数组，不要包含任何解释文字、markdown 标记或注释，"
                "每个元素必须是包含 action（字符串）与 params（对象）两个字段的对象。"
            )

    if not action_list:
        error_msg = f"Appium 动作规划失败：LLM 输出无法解析为有效动作 JSON（{parse_error}）"
        logger.error(f"{error_msg}，最后一次原始输出: {raw_output[:500] if raw_output else '（空）'}")
        updates: AgentState = {"action_list": [], "error": error_msg}
        if is_replan:
            updates["plan_retry_count"] = plan_retry_count + 1
        return updates

    logger.info(f"动作规划完成，共 {len(action_list)} 个动作" + ("（重规划）" if is_replan else ""))
    updates = {"action_list": action_list}
    if is_replan:
        updates["plan_retry_count"] = plan_retry_count + 1
    return updates


def _parse_actions_json(raw_output: str) -> Tuple[List[Dict[str, Any]], str]:
    """解析并校验 LLM 输出中的动作 JSON 数组。

    校验规则：每个动作必须是 dict，且 action 为非空字符串、params 为 dict。

    Args:
        raw_output: LLM 原始输出文本。

    Returns:
        Tuple[List[Dict[str, Any]], str]: (合法动作列表, 错误描述)。
        成功时错误描述为空字符串；失败时动作列表为空。
    """
    parsed, error = _extract_json_array(raw_output)
    if error:
        return [], error
    valid_actions: List[Dict[str, Any]] = []
    for item in parsed:
        if (
            isinstance(item, dict)
            and isinstance(item.get("action"), str)
            and item.get("action")
            and isinstance(item.get("params"), dict)
        ):
            valid_actions.append(item)
    if not valid_actions:
        return [], "数组中不含合法动作对象（每个元素需含字符串 action 与对象 params 字段）"
    return valid_actions, ""


def _summarize_failed_actions(execute_results: List[Dict[str, Any]]) -> str:
    """将失败的动作执行结果汇总为多行文本（供重规划 prompt 使用）。

    Args:
        execute_results: 动作执行结果列表。

    Returns:
        str: 失败动作摘要文本；无失败动作时返回提示文案。
    """
    failed = [r for r in execute_results if not r.get("success")]
    if not failed:
        return "（无失败动作）"
    return "\n".join(
        f"- 动作 {r.get('action')} 参数 {r.get('params')}: {r.get('message', '')}"
        for r in failed
    )


# ==================== 节点 4：执行测试 ====================
def execute_test(state: AgentState) -> AgentState:
    """节点4：解析动作列表，调用 Appium 在设备（模拟器/真机）上执行操作。

    会话严格使用 try-finally 保证 close() 一定被调用，防止设备会话占用。
    执行完成后调用 adb 抓取 logcat 日志与屏幕截图，作为缺陷分析依据。
    动作列表为空时跳过执行，仅采集日志与截图。

    Args:
        state: LangGraph 共享状态（读取 action_list）。

    Returns:
        AgentState: 状态增量更新（写入 execute_results、adb_log_path、
        screenshot_path、adb_log_content；连接失败时写入 error）。
    """
    logger.info("===== 节点4: 执行 Appium 自动化测试 =====")
    action_list = state.get("action_list", [])
    execute_results: List[Dict[str, Any]] = []

    # 设备模式：前端覆盖值优先（None/空字符串表示使用 .env 配置）
    user_is_real = state.get("user_is_real_device")
    user_udid = (state.get("user_udid") or "").strip() or None
    appium = AppiumTool(
        is_real_device=user_is_real if isinstance(user_is_real, bool) else None,
        udid=user_udid,
    )
    if not appium.connect():
        error_msg = "Appium 连接失败，请确认 Appium 服务与安卓设备（模拟器/真机）已启动"
        return {
            "execute_results": [],
            "adb_log_path": "",
            "screenshot_path": "",
            "adb_log_content": "",
            "error": error_msg,
        }

    try:
        if not action_list:
            logger.warning("动作列表为空，跳过 Appium 动作执行，仅采集设备日志与截图")
        for idx, action_item in enumerate(action_list, start=1):
            action = action_item.get("action", "")
            params = action_item.get("params", {})
            logger.info(f"执行第 {idx}/{len(action_list)} 个动作: {action}, 参数: {params}")

            if action == "click_by_id":
                result = appium.click_by_id(params.get("resource_id", ""))
            elif action == "input_text":
                result = appium.input_text(
                    params.get("resource_id", ""), params.get("text", "")
                )
            elif action == "sleep":
                seconds = int(params.get("seconds", 1))
                time.sleep(seconds)
                result = {"success": True, "message": f"等待 {seconds} 秒完成"}
            else:
                result = {"success": False, "message": f"不支持的动作类型: {action}"}
                logger.warning(result["message"])

            execute_results.append({**action_item, **result})
    finally:
        # 无论执行是否异常，都确保 Appium 会话释放，防止设备会话占用
        appium.close()

    # 测试动作执行完毕，抓取 adb 日志与截图作为缺陷分析依据
    # 目标设备与本次 Appium 会话保持一致（真机覆盖参数优先）
    adb_serial = _resolve_adb_serial(user_is_real, user_udid)
    adb_log_path = adb_tool.capture_logcat(tag="agent_run", device_serial=adb_serial)
    screenshot_path = adb_tool.capture_screenshot(tag="agent_run", device_serial=adb_serial)
    adb_log_content = adb_tool.read_log_file(adb_log_path) if adb_log_path else ""

    return {
        "execute_results": execute_results,
        "adb_log_path": adb_log_path,
        "screenshot_path": screenshot_path,
        "adb_log_content": adb_log_content,
    }


# ==================== 节点 5：缺陷反思与识别 ====================
def _get_log_level(log_line: str) -> str:
    """从 threadtime 格式日志行中提取日志级别字母。

    Args:
        log_line: 单行 threadtime 格式日志（如 "... 6620 6620 I AndroidRuntime: ..."）。

    Returns:
        str: 日志级别字母（V/D/I/W/E/F）；无法解析时返回空字符串。
    """
    tokens = log_line.split()
    if len(tokens) >= 5 and len(tokens[4]) == 1:
        return tokens[4].upper()
    return ""


def _scan_log_keywords(log_content: str) -> List[Dict[str, str]]:
    """扫描 logcat 日志中的 Crash/ANR 等疑似缺陷关键字，提取证据行。

    用于在 LLM 分析前定位真实日志证据，辅助模型判断、降低纯 LLM 幻觉。
    同一关键字最多保留 3 条证据、总证据最多 10 条，避免日志刷屏；
    宽松关键字（_LEVEL_FILTERED_KEYWORDS）仅对 W/E/F 级别日志行生效以减少误报。

    Args:
        log_content: logcat 日志文本内容。

    Returns:
        List[Dict[str, str]]: 关键字证据列表，每项含 keyword 与 line 字段。
    """
    hits: List[Dict[str, str]] = []
    keyword_counter: Dict[str, int] = {}
    seen_lines: set = set()
    for line in log_content.splitlines():
        stripped = line.strip()
        if not stripped or stripped in seen_lines:
            continue
        for keyword in LOG_DEFECT_KEYWORDS:
            if keyword.lower() not in stripped.lower():
                continue
            # 宽松关键字需日志级别为 W/E/F 才视为有效证据
            if keyword in _LEVEL_FILTERED_KEYWORDS and _get_log_level(stripped) not in ("W", "E", "F"):
                continue
            if keyword_counter.get(keyword, 0) >= 3:
                continue
            seen_lines.add(stripped)
            keyword_counter[keyword] = keyword_counter.get(keyword, 0) + 1
            hits.append({"keyword": keyword, "line": stripped[:200]})
            break
        if len(hits) >= 10:
            break
    return hits


def _build_defects_from_keyword_hits(
    keyword_hits: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """当 LLM 输出无法解析时，基于关键字扫描证据构建兜底缺陷列表。

    证据来自真实日志行，不属于模型幻觉，可作为 LLM 失效时的降级方案。

    Args:
        keyword_hits: 关键字扫描证据列表。

    Returns:
        List[Dict[str, Any]]: 兜底缺陷列表（type/severity/description/evidence）。
    """
    defects: List[Dict[str, Any]] = []
    for hit in keyword_hits:
        keyword = hit["keyword"]
        if "FATAL EXCEPTION" in keyword or "AndroidRuntime" in keyword or "beginning of crash" in keyword:
            defect_type, severity = "Crash", "致命"
        elif "ANR" in keyword or "Input dispatching timed out" in keyword:
            defect_type, severity = "ANR", "严重"
        else:
            defect_type, severity = "其他", "一般"
        defects.append(
            {
                "type": defect_type,
                "severity": severity,
                "description": f"日志关键字自动扫描发现疑似 {defect_type}（关键字: {keyword}）",
                "evidence": hit["line"],
                "suggestion": "请结合完整日志与复现步骤人工确认该缺陷。",
            }
        )
    return defects


def reflect_and_detect_defect(state: AgentState) -> AgentState:
    """节点5：LLM 结合关键字扫描证据，分析 adb logcat 日志识别缺陷。

    先对日志做 Crash/ANR 关键字预扫描，将真实证据行作为上下文提供给 LLM，
    要求模型仅基于日志证据判断，降低纯 LLM 幻觉；LLM 输出解析失败时，
    降级使用关键字证据构建兜底缺陷列表。

    Args:
        state: LangGraph 共享状态（读取 adb_log_content、execute_results）。

    Returns:
        AgentState: 状态增量更新（写入 defects）。
    """
    logger.info("===== 节点5: 分析日志识别缺陷 =====")
    adb_log_content = state.get("adb_log_content", "")
    execute_results = state.get("execute_results", [])

    # 关键字预扫描：提取真实日志证据，辅助 LLM 判断
    keyword_hits = _scan_log_keywords(adb_log_content)
    if keyword_hits:
        logger.warning(f"日志关键字扫描发现 {len(keyword_hits)} 条疑似缺陷证据")
        evidence_text = "\n".join(
            f"- [{hit['keyword']}] {hit['line']}" for hit in keyword_hits
        )
    else:
        evidence_text = "（未扫描到 Crash/ANR 相关关键字）"
        logger.info("日志关键字扫描未发现疑似缺陷证据")

    # 汇总动作执行结果，作为缺陷分析的辅助上下文
    failed_actions = [r for r in execute_results if not r.get("success")]
    exec_summary = _summarize_failed_actions(failed_actions)

    system_prompt = (
        "你是一名安卓质量保障专家。请分析给定的 logcat 日志与动作执行情况，"
        "识别 App 是否存在 Crash（崩溃）、ANR（应用无响应）、UI 异常（元素缺失/点击失败）等缺陷。"
        "判断必须基于给出的日志内容与【关键字扫描证据】，禁止编造日志中不存在的证据。"
        "严格输出 JSON 数组，不要输出任何其他文字或 markdown 代码块标记。"
        "每个缺陷对象包含字段：type（Crash/ANR/UI异常/其他）、severity（致命/严重/一般/轻微）、"
        "description（缺陷描述）、evidence（日志关键证据摘录）、suggestion（修复建议）。"
        "若证据为空且日志中未发现异常，请输出空数组 []。"
    )
    user_prompt = (
        f"【动作执行失败情况】\n{exec_summary}\n\n"
        f"【关键字扫描证据（真实日志摘录，优先据此判断）】\n{evidence_text}\n\n"
        f"【logcat 日志】\n{adb_log_content or '（未抓取到日志）'}"
    )

    raw_output = _invoke_llm(system_prompt, user_prompt, _resolve_user_key(state), _resolve_provider(state))
    defects, parse_error = _parse_defects_json(raw_output)
    if not defects and parse_error:
        # LLM 输出失效：降级为关键字证据兜底，保证节点产出稳定
        logger.warning(
            f"缺陷 JSON 解析失败（{parse_error}），降级使用关键字扫描证据构建缺陷列表"
        )
        defects = _build_defects_from_keyword_hits(keyword_hits)
    logger.info(f"缺陷分析完成，识别到 {len(defects)} 个缺陷")
    return {"defects": defects}


def _parse_defects_json(raw_output: str) -> Tuple[List[Dict[str, Any]], str]:
    """解析并校验 LLM 输出中的缺陷 JSON 数组。

    Args:
        raw_output: LLM 原始输出文本。

    Returns:
        Tuple[List[Dict[str, Any]], str]: (缺陷列表, 错误描述)。
        成功时错误描述为空字符串；失败时缺陷列表为空。
    """
    parsed, error = _extract_json_array(raw_output)
    if error:
        return [], error
    defects = [item for item in parsed if isinstance(item, dict)]
    return defects, ""


# ==================== 节点 6：生成测试报告 ====================
def build_report(state: AgentState) -> AgentState:
    """节点6：调用报告工具，将测试全流程数据生成为 Markdown 缺陷测试报告。

    Args:
        state: LangGraph 共享状态（读取 requirement、test_cases、execute_results、
        defects、adb_log_path、screenshot_path）。

    Returns:
        AgentState: 状态增量更新（写入 report_path；失败时写入 error）。
    """
    logger.info("===== 节点6: 生成 Markdown 测试报告 =====")
    report_path = report_tool.generate_markdown_report(
        requirement=state.get("requirement", ""),
        test_cases=state.get("test_cases", ""),
        execute_results=state.get("execute_results", []),
        defects=state.get("defects", []),
        log_path=state.get("adb_log_path", ""),
        screenshot_path=state.get("screenshot_path", ""),
    )
    if not report_path:
        return {"report_path": "", "error": "测试报告生成失败"}
    return {"report_path": report_path}
