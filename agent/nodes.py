# -*- coding: utf-8 -*-
"""
LangGraph 节点模块
定义智能测试 Agent 工作流的 6 个核心节点：
1. rag_retrieve            —— RAG 检索测试知识库
2. generate_test_case      —— LLM 结合检索上下文生成安卓 App 测试用例
3. plan_actions            —— LLM 将测试用例转为 JSON 格式的动作（解析失败自动重试 1 次）
4. execute_test            —— 解析 JSON 动作，按配置调用 Appium 或 mobile-mcp 执行，adb 抓取日志与截图
5. reflect_and_detect_defect —— LLM 分析 adb 日志，结合关键字扫描识别 Crash/ANR/UI 异常
6. build_report            —— 调用报告工具生成 Markdown 缺陷测试报告

LLM 通过工厂函数 get_llm() 按配置自动实例化：支持通义千问（qwen）/
DeepSeek（deepseek）一键切换（.env 的 LLM_PROVIDER），均为 OpenAI 兼容接口。

执行后端通过 tools/device_driver.py 的 DeviceDriver 协议切换：
- appium（默认）：需预先启动 Appium 服务（默认 http://127.0.0.1:4723）
- mobile_mcp：需 Node.js 20+ / npx，由程序自动拉起 mobile-mcp
两种方式均需预先启动安卓模拟器，或连接真机并开启 USB 调试。
"""

import json
import math
import re
import time
from typing import Any, Dict, List, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent.schemas import (
    STEPWISE_ACTION_NAMES,
    DefectItem,
    validate_static_action,
    validate_stepwise_params,
)
from agent.state import AgentState
from config.settings import settings
from rag.vector_store import similarity_search
from tools import adb_tool, report_tool
from tools.device_driver import (
    create_device_driver,
    resolve_device_backend,
    resolve_device_serial,
    supports_stepwise,
)
from utils import run_context, ui_tree
from utils.logger import logger
from utils.pet_broadcaster import pet_broadcaster
from utils.redaction import mask_text, redact_action_item, redact_action_params
from utils.ui_tree import UiNode, UiSnapshot

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

# sleep 动作允许的最大等待秒数，避免 LLM 规划出超长等待阻塞任务
MAX_SLEEP_SECONDS: int = 30

# 逐步 UI 驱动模式：单步动作白名单与安全上限
STEPWISE_ACTIONS = set(STEPWISE_ACTION_NAMES)
# 最近多少步操作会带入下一次 LLM prompt（避免历史无限增长）
STEPWISE_HISTORY_LIMIT: int = 12
# 单次输入文本的最大字符数（防止 LLM 输出超长内容）
STEPWISE_MAX_INPUT_CHARS: int = 300
# 逐步模式单步 sleep 的最大秒数
STEPWISE_MAX_SLEEP_SECONDS: float = 10.0
# 返回键最多使用次数，避免连续返回把被测 App 退回桌面
STEPWISE_MAX_BACK_ACTIONS: int = 3
# 过早调用 done success=false 时，先要求继续尝试的最少步数
STEPWISE_MIN_STEPS_BEFORE_FAILURE: int = 5
# 连续多少次 LLM 输出无法解析后中止（避免网络异常时空转）
STEPWISE_MAX_PARSE_FAILURES: int = 3
# 同一控件连续多少次“点击成功但页面无变化”后，禁止再次选择该控件
STEPWISE_MAX_NO_PROGRESS_REPEATS: int = 2
# 同一控件被阻止多少次后终止，避免在循环里耗尽全部步数
STEPWISE_MAX_BLOCKED_ATTEMPTS: int = 4
# 需要做无进展检测的动作类型（sleep 是主动等待，input_text 可能不改变 UI 文本）
STEPWISE_NO_PROGRESS_GUARDED_ACTIONS = {
    "tap_by_ref",
    "tap_by_text",
    "tap_by_coordinates",
    "scroll",
    "press_back",
}
# 通用导航按钮文字不能单独作为“目标达成”证据
STEPWISE_GENERIC_EVIDENCE = {
    "继续",
    "下一步",
    "确定",
    "好的",
    "完成",
    "提交",
    "检查",
    "跳过",
    "关闭",
    "重试",
    "开始",
    "继续学习",
    "领取",
    "continue",
    "next",
    "ok",
    "done",
    "submit",
    "check",
    "skip",
    "close",
    "retry",
}
# 当用户目标涉及课程/章节学习时，done 的证据必须包含完成相关字样，
# 防止把答题反馈页的“继续”误判为整节课完成。
STEPWISE_COURSE_GOAL_HINTS = (
    "课程",
    "章节",
    "一节课",
    "lesson",
    "unit",
    "course",
)
# 这些文案几乎只出现在课程/练习结算页，可作为自动判定完成的强证据
STEPWISE_AUTO_DONE_HINTS = (
    "课程完成",
    "完成课程",
    "完成本节",
    "完成本课",
    "本节完成",
    "练习完成",
    "练习结束",
    "单元完成",
    "连击进行中",
    "lesson complete",
    "unit complete",
    "lesson completed",
    "you completed",
)
STEPWISE_COURSE_EVIDENCE_HINTS = (
    "课程完成",
    "完成课程",
    "完成本节",
    "完成本课",
    "本节完成",
    "练习完成",
    "练习结束",
    "单元完成",
    "连击进行中",
    "领取经验",
    "学习时长",
    "正确率",
    "特别任务",
    "已完成",
    "完成了",
    "lesson complete",
    "unit complete",
    "lesson completed",
    "you completed",
    "claim xp",
    "xp earned",
)

# 测试用例标题行（支持 “## 用例 1：...”“### 测试用例 2” 等常见 Markdown 写法）
_CASE_HEADING_RE = re.compile(
    r"(?m)^\s*(?:#{1,4}\s*)?(?:测试)?用例\s*[0-9一二三四五六七八九十百]+\s*[:：]?"
)

# Android resource-id 的包名前缀（如 com.android.chrome:id/url_bar）
_RESOURCE_ID_PACKAGE_RE = re.compile(r"^([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+):id/")


def _resolve_adb_serial(user_is_real: Any, user_udid: str | None) -> str:
    """解析本次任务 adb 抓日志/截屏的目标设备序列号。

    与执行驱动的设备覆盖逻辑保持一致，确保自动化操作与 adb 采集
    始终作用于同一台设备（多设备在线时避免误操作 .env 默认设备）。

    Args:
        user_is_real: 前端设备模式覆盖值（True/False/None 表示使用 .env 配置）。
        user_udid: 前端真机 UDID 覆盖值。

    Returns:
        str: adb -s 目标设备序列号。
    """
    return resolve_device_serial(
        user_is_real if isinstance(user_is_real, bool) else None,
        user_udid,
    )


def _resolve_effective_device(
    user_is_real: Any, user_udid: str | None
) -> Dict[str, str]:
    """生成报告用的实际设备信息（兼容单次任务覆盖值）。

    Args:
        user_is_real: 前端设备模式覆盖值。
        user_udid: 前端真机 UDID 覆盖值。

    Returns:
        Dict[str, str]: device_mode / device_name / device_platform。
    """
    use_real = (
        user_is_real if isinstance(user_is_real, bool) else settings.IS_REAL_DEVICE
    )
    if use_real:
        name = (user_udid or "").strip() or settings.REAL_PHONE_UDID or "(未配置UDID)"
        platform = ""
        mode = "USB 真机"
    else:
        name = settings.EMULATOR_DEVICE_NAME
        platform = settings.android_platform_version
        mode = "安卓模拟器"
    return {
        "device_mode": mode,
        "device_name": name,
        "device_platform": platform,
    }


def _resolve_app_config(state: AgentState) -> Tuple[str, str]:
    """解析本次任务实际生效的被测应用包名与启动 Activity。

    优先级：单次任务覆盖值 > .env / settings 默认值。

    Args:
        state: 当前 Agent 状态。

    Returns:
        Tuple[str, str]: (app_package, app_activity)。
    """
    package = (state.get("user_app_package") or "").strip() or settings.app_package
    activity = (state.get("user_app_activity") or "").strip() or settings.app_activity
    return package, activity


def _stepwise_mode_enabled(state: AgentState) -> bool:
    """判断本次任务是否采用逐步 UI 驱动模式。

    优先级：单次任务覆盖值 > .env 的 STEPWISE_AGENT_ENABLED。
    逐步模式依赖 AppiumTool 的实时 UI 树/坐标点击扩展；mobile_mcp
    后端暂不支持，会自动回退到原静态动作规划。

    Args:
        state: 当前 Agent 状态。

    Returns:
        bool: 是否启用逐步 UI 驱动模式。
    """
    override = state.get("user_stepwise_agent")
    enabled = bool(override) if isinstance(override, bool) else bool(
        settings.STEPWISE_AGENT_ENABLED
    )
    if not enabled:
        return False
    backend = resolve_device_backend(state.get("user_device_backend"))
    if backend != "appium":
        logger.warning(
            f"逐步 UI 驱动模式暂只支持 Appium 后端（当前: {backend}），"
            "本次任务回退到静态动作规划"
        )
        return False
    return True


def _split_test_cases(test_cases: str) -> List[str]:
    """按 Markdown 用例标题把 LLM 输出拆成独立用例块。

    只有检测到 2 条及以上用例标题时才拆分；无法识别标题时原样返回，
    保持单用例/旧格式的现有行为。

    Args:
        test_cases: LLM 生成的测试用例全文。

    Returns:
        List[str]: 每个元素为一条用例的文本；空输入返回空列表。
    """
    text = (test_cases or "").strip()
    if not text:
        return []
    matches = list(_CASE_HEADING_RE.finditer(text))
    if len(matches) <= 1:
        return [text]

    blocks: List[str] = []
    for index, match in enumerate(matches):
        start = 0 if index == 0 else match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks or [text]


def _resource_id_allowed(resource_id: str, app_package: str) -> bool:
    """校验 resource-id 是否属于本次被测应用。

    仅当 resource-id 带有明确的包名前缀且与目标包名不一致时拒绝；
    没有包名前缀的非标准控件（Compose/Flutter 等）放行，避免误杀。

    Args:
        resource_id: 待执行的 resource-id。
        app_package: 本次任务被测应用包名。

    Returns:
        bool: 允许执行返回 True。
    """
    package = (app_package or "").strip()
    if not package:
        return True
    match = _RESOURCE_ID_PACKAGE_RE.match(resource_id or "")
    if not match:
        return True
    return match.group(1) == package


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
            timeout=max(1, int(settings.llm_request_timeout)),
            # 重试统一在 _invoke_llm 中完成，便于记录日志和区分可重试错误
            max_retries=0,
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
        timeout=max(1, int(settings.llm_request_timeout)),
        max_retries=0,
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


def _is_retryable_llm_error(exc: Exception) -> bool:
    """判断 LLM 调用异常是否值得重试。

    认证/参数错误重试也不会成功，直接失败；超时、限流、5xx 与网络
    异常则按指数退避重试。

    Args:
        exc: LLM 调用抛出的异常。

    Returns:
        bool: 可重试返回 True。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (400, 401, 403, 404, 422):
            return False
        if status in (408, 409, 425, 429, 500, 502, 503, 504):
            return True
    text = str(exc).lower()
    non_retryable = (
        "401",
        "403",
        "unauthorized",
        "invalid api key",
        "invalid_api_key",
        "authentication",
        "permission denied",
        "bad request",
        "invalid_request_error",
    )
    return not any(marker in text for marker in non_retryable)


def _invoke_llm(
    system_prompt: str,
    user_prompt: str,
    user_api_key: str | None = None,
    provider: str | None = None,
) -> str:
    """调用 LLM 的通用封装，带超时、重试与指数退避。

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

    max_retries = max(0, int(settings.llm_max_retries))
    total_attempts = max_retries + 1
    backoff = max(0.0, float(settings.llm_retry_backoff_seconds))
    last_error: Exception | None = None

    for attempt in range(total_attempts):
        try:
            response = get_llm(user_api_key, provider).invoke(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            )
            usage = getattr(response, "usage_metadata", None) or {}
            if usage:
                logger.debug(
                    "LLM 调用完成: input_tokens=%s, output_tokens=%s",
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )
            return str(response.content).strip()
        except Exception as e:
            last_error = e
            if attempt >= total_attempts - 1 or not _is_retryable_llm_error(e):
                break
            delay = backoff * (2 ** attempt)
            logger.warning(
                f"LLM 调用失败（第 {attempt + 1}/{total_attempts} 次），"
                f"{delay:.1f}s 后重试: {e}"
            )
            if delay > 0:
                time.sleep(delay)

    logger.error(f"LLM 调用失败: {last_error}", exc_info=True)
    return ""


def _extract_json_array(raw_output: str) -> Tuple[List[Any], str]:
    """从 LLM 原始输出中提取第一个合法 JSON 数组。

    使用 ``json.JSONDecoder.raw_decode`` 从每个 ``[`` 位置尝试解析，
    避免旧实现 ``\\[.*\\]`` 贪婪匹配把多个数组/后续解释文字拼在一起
    导致 ``json.loads`` 失败。

    Args:
        raw_output: LLM 原始输出文本。

    Returns:
        Tuple[List[Any], str]: (解析结果列表, 错误描述)。
        成功时错误描述为空字符串；失败时列表为空。
    """
    if not raw_output:
        return [], "LLM 输出为空"

    # 去除可能的 markdown 代码块包裹
    cleaned = re.sub(r"```(?:json)?|```", "", raw_output).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "[":
            continue
        try:
            parsed, _end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed, ""
    logger.warning(f"JSON 数组提取失败，输出片段: {cleaned[:300]}")
    return [], "输出中未找到合法 JSON 数组"


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
    app_package, _ = _resolve_app_config(state)
    # 把被测应用包名加入检索 query，优先召回该 App 的文档，降低跨 App 偏置
    rag_context = similarity_search(f"{app_package} {requirement}".strip())
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
    # 桌宠单向播报：默认关闭；异常在播报模块内部静默处理，不影响主流程。
    pet_broadcaster.planning_test_cases()
    requirement = state.get("requirement", "")
    app_package, app_activity = _resolve_app_config(state)
    rag_context = state.get("rag_context", [])
    context_text = "\n\n".join(
        f"[知识片段{i}]:\n{chunk}" for i, chunk in enumerate(rag_context, start=1)
    ) or "（知识库暂无相关内容）"

    system_prompt = (
        "你是一名资深安卓 App 测试工程师，服务于小米手机应用测试团队。"
        "请结合给定的测试知识库上下文与用户需求，编写结构清晰、可执行的安卓 App 测试用例。"
        "每个测试用例需包含：用例编号、用例标题、前置条件、测试步骤、预期结果。"
        "所有操作必须限定在指定的被测应用内；不要规划其他应用的启动、点击或输入。"
    )
    user_prompt = (
        f"【测试知识库上下文】\n{context_text}\n\n"
        f"【用户测试需求】\n{requirement}\n\n"
        f"【被测应用】包名: {app_package}, 启动Activity: {app_activity}\n"
        "请输出 1 条主用例；如果需求明确包含多个独立场景，再按场景补充，"
        "总数不超过 3 条。不要为了凑数重复同一动作序列。"
    )

    test_cases = _invoke_llm(system_prompt, user_prompt, _resolve_user_key(state), _resolve_provider(state))
    if not test_cases:
        test_cases = "LLM 生成测试用例失败，请检查大模型配置与网络。"
        return {"test_cases": test_cases, "error": test_cases}
    logger.info("测试用例生成完成")
    return {"test_cases": test_cases}


# ==================== 节点 3：规划执行动作 ====================
def _plan_actions_for_case(
    case_text: str,
    system_prompt: str,
    llm_key: str | None,
    llm_provider: str,
    failed_summary: str = "",
) -> Tuple[List[Dict[str, Any]], str, str]:
    """为单条测试用例规划动作，解析失败时自动重试。

    Args:
        case_text: 单条测试用例文本。
        system_prompt: 动作规划 system prompt。
        llm_key: 本次任务临时密钥（可为空）。
        llm_provider: 生效的 provider。
        failed_summary: 重规划时的失败动作摘要；首次规划为空。

    Returns:
        Tuple[List[Dict[str, Any]], str, str]: (动作列表, 错误描述, 最后一次原始输出)。
    """
    user_prompt = f"【测试用例】\n{case_text}\n\n请输出对应的动作 JSON 数组。"
    if failed_summary:
        user_prompt += (
            f"\n\n【重要提示】上一轮规划的动作全部执行失败，失败情况如下：\n{failed_summary}\n"
            "请调整 resource-id 或动作序列（例如增加 sleep 等待页面加载、"
            "改用更通用的 resource-id）后重新输出完整 JSON 数组。"
        )

    action_list: List[Dict[str, Any]] = []
    parse_error = ""
    raw_output = ""
    for attempt in range(PLAN_PARSE_MAX_RETRY + 1):
        raw_output = _invoke_llm(system_prompt, user_prompt, llm_key, llm_provider)
        action_list, parse_error = _parse_actions_json(raw_output)
        if action_list:
            return action_list, "", raw_output
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
    return [], parse_error, raw_output


def plan_actions(state: AgentState) -> AgentState:
    """节点3：LLM 将测试用例转换为 JSON 格式的执行动作列表。

    支持的动作类型：
    - click_by_id: 点击元素，params: {"resource_id": "xxx"}
    - input_text:  输入文本，params: {"resource_id": "xxx", "text": "xxx"}
    - clear_text:  清空输入框，params: {"resource_id": "xxx"}
    - sleep:       等待，params: {"seconds": 2}

    多条用例会逐条规划，并在用例之间自动插入 restart_app 动作复位 App，
    避免上一条用例的页面状态污染下一条用例。

    Args:
        state: LangGraph 共享状态（读取 test_cases、execute_results、plan_retry_count）。

    Returns:
        AgentState: 状态增量更新（写入 action_list、plan_retry_count；失败时写入 error）。
    """
    logger.info("===== 节点3: 规划执行动作 =====")
    if _stepwise_mode_enabled(state):
        # 逐步模式不再让 LLM 凭空猜 resource-id；动作由 execute_test 在
        # 每一步读取实时 UI 树后生成，因此这里只写入模式标记。
        logger.info(
            "逐步 UI 驱动模式已开启：跳过静态动作规划，"
            "改由执行节点逐步观察 UI 树并选择动作"
        )
        return {"action_list": [], "stepwise_mode": True}
    test_cases = state.get("test_cases", "")
    plan_retry_count = state.get("plan_retry_count", 0)
    app_package, app_activity = _resolve_app_config(state)
    # 存在上一轮执行结果说明当前是执行失败后的重规划分支
    is_replan = bool(state.get("execute_results"))

    system_prompt = (
        "你是一名安卓 UI 自动化测试专家。请将测试用例转换为自动化框架可执行的动作序列。"
        "严格输出 JSON 数组，不要输出任何其他文字或 markdown 代码块标记。"
        "每个动作对象包含 action 与 params 两个字段，仅支持以下四种动作：\n"
        '1. {"action": "click_by_id", "params": {"resource_id": "元素resource-id"}}\n'
        '2. {"action": "input_text", "params": {"resource_id": "输入框resource-id", "text": "输入内容"}}\n'
        '3. {"action": "clear_text", "params": {"resource_id": "输入框resource-id"}}\n'
        '4. {"action": "sleep", "params": {"seconds": 2}}\n'
        f"被测应用包名为 {app_package}，启动 Activity 为 {app_activity}，"
        f"resource-id 一般采用 \"包名:id/xxx\" 格式，且必须属于被测应用。\n"
        "清空输入框请使用 clear_text，不要用 text 为空字符串的 input_text。"
    )

    # 本次任务生效的 LLM 密钥与 provider（前端临时密钥优先，.env 兜底）
    llm_key = _resolve_user_key(state)
    llm_provider = _resolve_provider(state)
    failed_summary = (
        _summarize_failed_actions(state.get("execute_results", []))
        if is_replan
        else ""
    )

    case_blocks = _split_test_cases(test_cases)
    if not case_blocks:
        error_msg = "动作规划失败：测试用例为空"
        logger.error(error_msg)
        updates: AgentState = {"action_list": [], "error": error_msg}
        if is_replan:
            updates["plan_retry_count"] = plan_retry_count + 1
        return updates

    if len(case_blocks) > 1:
        logger.info(
            f"检测到 {len(case_blocks)} 条独立用例，将逐条规划并在用例间重启被测应用"
        )

    action_list: List[Dict[str, Any]] = []
    for index, case_text in enumerate(case_blocks):
        actions, parse_error, raw_output = _plan_actions_for_case(
            case_text,
            system_prompt,
            llm_key,
            llm_provider,
            failed_summary=failed_summary,
        )
        if not actions:
            error_msg = (
                f"动作规划失败（用例 {index + 1}/{len(case_blocks)}）："
                f"LLM 输出无法解析为有效动作 JSON（{parse_error}）"
            )
            logger.error(
                f"{error_msg}，最后一次原始输出: "
                f"{raw_output[:500] if raw_output else '（空）'}"
            )
            updates = {"action_list": [], "error": error_msg}
            if is_replan:
                updates["plan_retry_count"] = plan_retry_count + 1
            return updates

        # 第一条用例沿用当前 App 状态；后续用例前先重启 App 复位 UI
        if index > 0:
            action_list.append({"action": "restart_app", "params": {}})
        action_list.extend(actions)

    logger.info(
        f"动作规划完成，共 {len(action_list)} 个动作（{len(case_blocks)} 条用例）"
        + ("（重规划）" if is_replan else "")
    )
    updates = {"action_list": action_list}
    if is_replan:
        updates["plan_retry_count"] = plan_retry_count + 1
    return updates


def _parse_actions_json(raw_output: str) -> Tuple[List[Dict[str, Any]], str]:
    """解析并校验 LLM 输出中的动作 JSON 数组。

    逐项校验动作名、resource-id 与参数范围；非法条目丢弃并在日志中
    告警。若没有任何合法动作，则返回可直接拼入重试提示词的错误信息。

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
    errors: List[str] = []
    for item in parsed:
        action, item_error = validate_static_action(item, MAX_SLEEP_SECONDS)
        if action is None:
            errors.append(item_error)
        else:
            valid_actions.append(action)

    if not valid_actions:
        return [], errors[0] if errors else "动作数组中不含合法对象"
    if errors:
        logger.warning(
            f"动作解析丢弃 {len(errors)} 个非法条目: {errors[:3]}"
        )
    return valid_actions, ""


def _summarize_failed_actions(execute_results: List[Dict[str, Any]]) -> str:
    """将失败的动作执行结果汇总为多行文本（供重规划 prompt 使用）。

    参数再次脱敏，防止未脱敏的历史结果把输入内容带入下一次 LLM 调用。

    Args:
        execute_results: 动作执行结果列表。

    Returns:
        str: 失败动作摘要文本；无失败动作时返回提示文案。
    """
    failed = [r for r in execute_results if not r.get("success")]
    if not failed:
        return "（无失败动作）"
    return "\n".join(
        f"- 动作 {r.get('action')} 参数 "
        f"{redact_action_params(r.get('action', ''), r.get('params', {}))}: "
        f"{r.get('message', '')}"
        for r in failed
    )


# ==================== 节点 4：执行测试（逐步 UI 驱动） ====================
def _stepwise_system_prompt(app_package: str) -> str:
    """构建逐步 UI 驱动模式的 system prompt。

    Args:
        app_package: 本次被测应用包名。

    Returns:
        str: system prompt 文本。
    """
    prompt = """你是一个安卓 App 的逐步 UI 自动化测试 Agent。每一步你都会看到当前屏幕的实时 UI 树，只能选择一个动作。请根据总目标和当前 UI 状态推进任务，不要凭空猜测控件。

严格只输出一个 JSON 对象，不要输出 markdown 代码块、解释文字或多条动作：
{"action":"动作名","params":{...},"reason":"简短中文理由","expect":"执行后预期看到的变化"}

可用动作（params 必须是对象）：
1. tap_by_ref: {"ref": 12}
   优先使用；ref 必须来自“当前屏幕 UI 树”，点击该节点中心。
2. tap_by_text: {"text":"继续","index":0,"exact":true}
   文本必须来自当前 UI 树；index 用于同名控件（从 0 开始）。exact=false 表示包含匹配。
3. tap_by_coordinates: {"x":540,"y":1200}
   仅在 ref/文本都无法定位时使用；坐标必须在屏幕内，并尽量取自当前节点的 bounds。
4. input_text: {"ref": 8,"text":"hello"}
   先点击 ref 输入框使其获得焦点，再输入文本；ref 省略时表示向当前焦点输入框输入。
5. press_back: {}
   关闭弹窗、广告或返回上一页。
6. scroll: {"direction":"down"}
   direction 为 down 或 up。
7. sleep: {"seconds":2}
   等待页面加载，最多 10 秒。
8. done: {"success":true,"evidence":"当前 UI 树中可见的完成证据原文"}
   只有目标真正达成时才调用；evidence 必须是当前 UI 树中某个节点 text/content-desc 的原文片段，不能编造。若被登录、付费墙、验证码等硬阻断，可调用 {"success":false,"evidence":"当前可见的阻断文案"}。

规则：
- 只操作当前 UI 树中 tappable=true、enabled=true 的节点；绝不要点击 danger=true 的节点。
- 每一步 ref 都会重新编号，必须使用“当前屏幕 UI 树”的 ref，不能沿用历史步骤的 ref。
- 点击后先观察页面变化，再决定下一步；不要连续重复点击同一个无效控件超过 2 次。
- 课程地图：点击章节标题下方、路径中央的第一个绿色/高亮圆形课程节点（通常是无文字的 FrameLayout/View）；不要点左侧徽章、宝箱、书本、角色、底部导航或升级按钮。
- 不要点击包含“第 X 阶段，第 Y 部分 / 在商场购物”的章节标题栏，它会打开跳级/切换页面；应点击标题下方路径中央的圆形课程节点。
- 课程题目：词块通常是无 resource-id 的可点击 View，文字在子 TextView 上；可优先 tap_by_text 词块。选完答案后点击“检查/CHECK/提交”类按钮；出现反馈后点击“继续/CONTINUE”。
- 选词填空/组句：带 id=tapToken 的容器是词块，子节点 optionText 是它的文字；点击词库中的词块会把它移入句子，该词块随后从词库消失。按正确顺序把每个词各点一次；再点句子中的词块会把它移回词库。句中的“?”等标点是固定内容，不要点击。全部填满后“检查”才会变为 enabled=true。
- “找规律/新词形”题型：上方示例句只用于参考，不要点击示例句；根据示例句式从下方词库选择对应词，填满练习句后检查。
- 配对题：依次点击左右两列中语义配对的词块。
- 如果听力题没有声音/无法播放，优先选择官方“现在不做听力题”；口语题选择“现在不做口语题”，不要使用第三方跳过手段。
- 如果出现购买、订阅、免费试用、支付、充值、分享、发送消息等弹窗，使用 press_back 或“关闭/以后再说/算了吧”类按钮，绝不能购买、订阅、分享或发送消息。
- 如果上一步操作后页面没有变化，说明该控件无效；禁止再次选择同一个控件。应改用其他控件，或先 scroll / press_back 改变页面状态。
- 只有当前屏幕确实出现目标完成证据时才调用 done success=true；不要因为一次点击失败就 done success=false，只有连续尝试后仍被硬阻断才判定失败。
- 如果目标是完成课程/章节，evidence 必须来自课程结算页（如“连击进行中”“领取经验”“课程完成”），不能把答题反馈页的“继续”当作整节课完成证据。
- 每做完一道题并点击“继续”后，先检查是否进入课程结算页；一旦看到“课程完成/练习完成/连击进行中/领取经验”等结算文案，立即 done success=true，不要点击“领取经验/开箱/继续”等奖励按钮，也不需要领取奖励。
- 当前被测应用包名: __APP_PACKAGE__，所有操作必须限定在该应用内。"""
    return prompt.replace("__APP_PACKAGE__", app_package or "(未知)")


def _stepwise_user_prompt(
    goal: str,
    step_no: int,
    max_steps: int,
    remaining_seconds: int,
    snapshot: UiSnapshot,
    screen_size: Tuple[int, int],
    history: List[Dict[str, Any]],
    blocked_targets: List[str] | None = None,
    consecutive_no_progress: int = 0,
) -> str:
    """构建逐步 UI 驱动模式的 user prompt。

    Args:
        goal: 总目标（需求 + 测试用例摘要）。
        step_no: 当前步数（从 1 开始）。
        max_steps: 最大步数。
        remaining_seconds: 剩余秒数。
        snapshot: 当前 UI 快照。
        screen_size: 屏幕尺寸。
        history: 之前的动作执行记录。
        blocked_targets: 因连续点击无变化而被禁止的目标标签。
        consecutive_no_progress: 最近连续无变化动作数。

    Returns:
        str: user prompt 文本。
    """
    history_text = "（暂无，当前是第一步）"
    if history:
        lines: List[str] = []
        for item in history[-STEPWISE_HISTORY_LIMIT:]:
            status = "成功" if item.get("success") else "失败"
            action = item.get("action", "-")
            params = item.get("params", {}) or {}
            brief_parts: List[str] = []
            for key in ("ref", "text", "x", "y", "direction", "seconds", "evidence"):
                if key in params:
                    value = str(params.get(key, ""))[:80]
                    brief_parts.append(f"{key}={value}")
            brief = ", ".join(brief_parts)
            message = str(item.get("message", ""))[:120].replace("\n", " ")
            reason = str(item.get("reason", ""))[:60].replace("\n", " ")
            line = f"- 第 {item.get('step', '?')} 步 {action}({brief}) -> {status}；{message}"
            if item.get("ui_changed") is False:
                line += "；页面未变化"
            if reason:
                line += f"；意图：{reason}"
            lines.append(line)
        history_text = "\n".join(lines)
    ui_text = ui_tree.format_ui_tree(
        snapshot, screen_width=screen_size[0], screen_height=screen_size[1]
    )
    warning = ""
    if blocked_targets or consecutive_no_progress >= 2:
        warning_parts = ["【重复无效操作警告】"]
        if blocked_targets:
            warning_parts.append(
                "以下控件已连续点击 2 次但页面没有变化，禁止再次选择："
                + "、".join(blocked_targets[:6])
                + "。"
            )
        if consecutive_no_progress >= 2:
            warning_parts.append(
                f"最近连续 {consecutive_no_progress} 步页面没有变化，可能已陷入循环；"
                "请主动改变策略：选择其他控件，或 scroll / press_back 改变页面状态，"
                "不要继续点击同一区域。"
            )
        warning = "\n\n" + "\n".join(warning_parts)
    return (
        f"【总目标】\n{goal[:4000]}\n\n"
        f"【当前进度】第 {step_no}/{max_steps} 步，剩余约 {max(0, remaining_seconds)} 秒\n\n"
        f"【最近操作历史】\n{history_text}{warning}\n\n"
        f"【当前屏幕 UI 树】\n{ui_text}\n\n"
        "请只输出下一步动作 JSON 对象。"
    )


def _extract_json_object(raw_output: str) -> Tuple[Dict[str, Any] | None, str]:
    """从 LLM 输出中提取第一个合法 JSON 对象。

    容忍 markdown 代码块、前后解释文字和对象后的多余字符。

    Args:
        raw_output: LLM 原始输出。

    Returns:
        Tuple[Dict[str, Any] | None, str]: (JSON 对象, 错误描述)。
    """
    text = (raw_output or "").strip()
    if not text:
        return None, "LLM 输出为空"
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj, ""
        start = text.find("{", start + 1)
    return None, "未找到合法 JSON 对象"


def _parse_stepwise_action(
    raw_output: str,
) -> Tuple[Dict[str, Any] | None, str]:
    """解析并校验逐步模式下的单个动作 JSON。

    Args:
        raw_output: LLM 原始输出。

    Returns:
        Tuple[Dict[str, Any] | None, str]: (动作字典, 错误描述)。
        动作字典包含 action / params / reason / expect。
    """
    obj, error = _extract_json_object(raw_output)
    if obj is None:
        return None, error
    action = str(obj.get("action") or obj.get("name") or "").strip().lower()
    if action not in STEPWISE_ACTIONS:
        return None, f"不支持的动作: {action or '(空)'}"
    params = obj.get("params")
    if not isinstance(params, dict):
        params = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}
    params = dict(params)
    # 兼容部分模型把 done 的字段放在顶层
    if action == "done":
        for key in ("success", "evidence"):
            if key not in params and key in obj:
                params[key] = obj[key]
    normalized, params_error = validate_stepwise_params(
        action,
        params,
        max_sleep_seconds=STEPWISE_MAX_SLEEP_SECONDS,
        max_input_chars=STEPWISE_MAX_INPUT_CHARS,
    )
    if params_error:
        return None, params_error
    return {
        "action": action,
        "params": normalized,
        "reason": str(obj.get("reason", ""))[:120],
        "expect": str(obj.get("expect", ""))[:120],
    }, ""


def _trace_params(
    action: str, params: Dict[str, Any], node: UiNode | None = None
) -> Dict[str, Any]:
    """生成可写入报告的动作参数（输入内容做脱敏）。

    Args:
        action: 动作类型。
        params: 原始参数字典。
        node: 本次命中的节点（可选）。

    Returns:
        Dict[str, Any]: 可安全写入报告的参数。
    """
    trace: Dict[str, Any] = {}
    if action == "input_text":
        trace["text"] = mask_text(params.get("text", ""))
        if params.get("ref") is not None:
            trace["ref"] = params.get("ref")
        return trace
    for key in ("ref", "text", "index", "x", "y", "direction", "seconds", "success", "evidence"):
        if key in params:
            value = params.get(key)
            if isinstance(value, str):
                value = value[:80]
            trace[key] = value
    if node is not None and action == "tap_by_ref":
        if node.text:
            trace["node_text"] = node.text[:40]
        elif node.content_desc:
            trace["node_desc"] = node.content_desc[:40]
    return trace


def _snapshot_signature(snapshot: UiSnapshot) -> Tuple[str, ...]:
    """生成 UI 快照的稳定签名，用于判断一次动作是否改变了页面。

    Args:
        snapshot: 当前 UI 快照。

    Returns:
        Tuple[str, ...]: 按文档顺序拼接的节点特征；两次签名相同表示页面无可见变化。
    """
    return tuple(
        "|".join(
            (
                node.class_name,
                node.text,
                node.content_desc,
                node.resource_id,
                str(node.bounds),
                "1" if node.enabled else "0",
                "1" if node.tappable else "0",
                "1" if node.selected else "0",
                "1" if node.checked else "0",
            )
        )
        for node in snapshot.nodes
    )


def _node_target_key(node: UiNode) -> str:
    """生成节点的稳定目标标识（避免 ref 每步重编号导致误判）。"""
    return "|".join(
        (
            node.class_name,
            node.text,
            node.content_desc,
            node.resource_id,
            str(node.bounds),
        )
    )


def _node_readable_label(node: UiNode) -> str:
    """生成不依赖 ref 的节点可读标签（ref 每步都会重新编号）。"""
    parts: List[str] = []
    if node.text:
        parts.append(f'text="{node.text}"')
    elif node.content_desc:
        parts.append(f'desc="{node.content_desc}"')
    if node.resource_id:
        parts.append(f"id={node.resource_id.split('/')[-1]}")
    parts.append(f"bounds={node.bounds}")
    return " ".join(parts)


def _stepwise_target_key(
    action_item: Dict[str, Any],
    snapshot: UiSnapshot,
    screen_size: Tuple[int, int],
) -> Tuple[str, str]:
    """解析动作实际命中的控件，返回稳定目标标识与可读标签。

    Args:
        action_item: 解析后的动作。
        snapshot: 动作执行前的 UI 快照。
        screen_size: 屏幕尺寸。

    Returns:
        Tuple[str, str]: (稳定目标标识, 可读标签)；无法定位具体控件时标签为空。
    """
    action = str(action_item.get("action", "")).strip()
    params = action_item.get("params")
    if not isinstance(params, dict):
        params = {}

    if action == "tap_by_ref":
        node = ui_tree.find_node_by_ref(snapshot.nodes, params.get("ref"))
        if node is not None:
            return f"tap:{_node_target_key(node)}", _node_readable_label(node)
        return f"tap_ref:{params.get('ref')!r}", ""

    if action == "tap_by_text":
        text = str(params.get("text", "") or "").strip()
        try:
            index = int(params.get("index", 0) or 0)
        except (TypeError, ValueError):
            index = 0
        matches = ui_tree.find_nodes_by_text(snapshot.nodes, text, exact=True)
        if not matches and text:
            matches = ui_tree.find_nodes_by_text(snapshot.nodes, text, exact=False)
        tappable = [node for node in matches if node.tappable and node.enabled]
        if 0 <= index < len(tappable):
            node = tappable[index]
            return f"tap:{_node_target_key(node)}", _node_readable_label(node)
        return f"tap_text:{text}|{index}", f'text="{text}"'

    if action == "tap_by_coordinates":
        try:
            x = int(params.get("x"))
            y = int(params.get("y"))
        except (TypeError, ValueError):
            return f"tap_xy:{params.get('x')!r},{params.get('y')!r}", ""
        node = ui_tree.find_node_at(snapshot.nodes, x, y)
        if node is not None:
            return f"tap:{_node_target_key(node)}", _node_readable_label(node)
        return f"tap_xy:{x},{y}", f"坐标 ({x}, {y})"

    if action == "scroll":
        direction = str(params.get("direction", "down")).strip().lower()
        return f"scroll:{direction}", f"scroll {direction}"

    if action == "press_back":
        return "back", "返回键"

    if action == "input_text":
        ref = params.get("ref")
        if ref is not None:
            node = ui_tree.find_node_by_ref(snapshot.nodes, ref)
            if node is not None:
                return f"input:{_node_target_key(node)}", _node_readable_label(node)
        return "input:focused", "当前焦点输入框"

    return action, ""


def _capture_ui_snapshot(
    driver: Any, app_package: str
) -> Tuple[Tuple[UiSnapshot, int, int] | None, str]:
    """读取并解析当前屏幕 UI 树。

    Args:
        driver: 逐步模式驱动（需实现 get_page_source/get_window_size）。
        app_package: 被测应用包名，用于过滤其他应用节点。

    Returns:
        Tuple[Tuple[UiSnapshot, int, int] | None, str]:
        成功时为 ((快照, 宽, 高), "")；失败时为 (None, 错误描述)。
    """
    try:
        xml = driver.get_page_source()
    except Exception as e:
        return None, f"读取页面 UI 树异常: {e}"
    if not xml:
        return None, "读取页面 UI 树为空"
    snapshot = ui_tree.parse_ui_nodes(
        xml,
        app_package=app_package,
        max_nodes=max(1, int(settings.STEPWISE_UI_MAX_NODES)),
        max_text_length=max(8, int(settings.STEPWISE_UI_MAX_TEXT_LENGTH)),
    )
    if not snapshot.nodes:
        return None, "当前 UI 树没有可操作节点"
    try:
        width, height = driver.get_window_size()
    except Exception:
        width, height = 0, 0
    if width <= 0 or height <= 0:
        width = max((node.bounds[2] for node in snapshot.nodes), default=0)
        height = max((node.bounds[3] for node in snapshot.nodes), default=0)
    return (snapshot, int(width), int(height)), ""


def _stepwise_evidence_error(evidence: str, goal: str) -> str:
    """校验 done 证据是否有资格作为“目标达成”依据。

    Args:
        evidence: LLM 提供的当前页面可见证据。
        goal: 总目标文本。

    Returns:
        str: 不合格原因；合格时为空字符串。
    """
    normalized = (evidence or "").strip().strip("。.!！?？")
    if len(normalized) < 2:
        return "证据过短，无法证明目标达成"
    if normalized.casefold() in STEPWISE_GENERIC_EVIDENCE:
        return "证据只是通用按钮文字，不能证明目标达成"
    goal_lower = (goal or "").casefold()
    if any(hint in goal_lower for hint in STEPWISE_COURSE_GOAL_HINTS):
        evidence_lower = normalized.casefold()
        if not any(
            hint in evidence_lower for hint in STEPWISE_COURSE_EVIDENCE_HINTS
        ):
            return "课程类目标需要包含完成/结算相关字样（如“连击进行中”“领取经验”“课程完成”）"
    return ""


def _stepwise_auto_completion_evidence(snapshot: UiSnapshot, goal: str) -> str:
    """从当前 UI 树自动识别课程/练习结算证据。

    课程结束后多邻国等 App 会显示“课程完成”“连击进行中”等结算文案；
    此时应直接结束，而不是继续点击“领取经验/开箱”等奖励按钮。
    为避免把日常任务奖励误判为课程完成，这里只接受强结算文案。

    Args:
        snapshot: 当前 UI 快照。
        goal: 总目标文本。

    Returns:
        str: 命中的可见结算文案；未命中返回空字符串。
    """
    goal_lower = (goal or "").casefold()
    if not any(hint in goal_lower for hint in STEPWISE_COURSE_GOAL_HINTS):
        return ""
    # “连击进行中”也可能作为答题连击提示出现在题目页；
    # 若屏幕上仍有检查/提交按钮，说明练习尚未结束，不能据此判定完成。
    active_exercise = (
        ui_tree.snapshot_contains_text(snapshot, "检查")
        or ui_tree.snapshot_contains_text(snapshot, "提交")
        or ui_tree.snapshot_contains_text(snapshot, "check")
        or ui_tree.snapshot_contains_text(snapshot, "submit")
        or any(
            node.resource_id.lower().endswith("submitbutton")
            for node in snapshot.nodes
        )
    )
    for hint in STEPWISE_AUTO_DONE_HINTS:
        if not ui_tree.snapshot_contains_text(snapshot, hint):
            continue
        if hint == "连击进行中" and active_exercise:
            continue
        return hint
    return ""


def _execute_stepwise_action(
    driver: Any,
    action_item: Dict[str, Any],
    snapshot: UiSnapshot,
    screen_size: Tuple[int, int],
    step_no: int,
    back_count: int,
    goal: str = "",
) -> Dict[str, Any]:
    """执行逐步模式下的单个动作。

    Args:
        driver: 逐步模式驱动。
        action_item: 解析后的动作（含 action/params）。
        snapshot: 当前 UI 快照。
        screen_size: 屏幕尺寸 (width, height)。
        step_no: 当前步数。
        back_count: 已使用返回键次数。
        goal: 总目标文本，用于校验 done 证据是否满足课程完成语义。

    Returns:
        Dict[str, Any]: 执行结果，包含 success/message/terminal/
        goal_achieved/goal_evidence/trace_params/back_used。
    """
    action = str(action_item.get("action", "")).strip()
    params = action_item.get("params")
    if not isinstance(params, dict):
        params = {}

    def _result(
        success: bool,
        message: str,
        trace: Dict[str, Any] | None = None,
        terminal: bool = False,
        goal_achieved: bool = False,
        goal_evidence: str = "",
        back_used: bool = False,
    ) -> Dict[str, Any]:
        return {
            "success": success,
            "message": message,
            "terminal": terminal,
            "goal_achieved": goal_achieved,
            "goal_evidence": goal_evidence,
            "trace_params": trace or {},
            "back_used": back_used,
        }

    if action == "tap_by_ref":
        node = ui_tree.find_node_by_ref(snapshot.nodes, params.get("ref"))
        if node is None:
            return _result(False, f"ref 不存在或已过期: {params.get('ref')!r}", {"ref": params.get("ref")})
        trace = _trace_params(action, params, node)
        if not node.enabled:
            return _result(False, f"节点不可用: {node.short_label()}", trace)
        if node.dangerous:
            return _result(False, f"已拒绝敏感操作: {node.danger_reason}", trace)
        if not node.tappable:
            return _result(False, f"节点不可点击: {node.short_label()}", trace)
        tap_result = driver.tap_xy(*node.center)
        return _result(
            bool(tap_result.get("success")),
            f"点击 {node.short_label()} -> {tap_result.get('message', '')}",
            trace,
        )

    if action == "tap_by_text":
        text = str(params.get("text", "") or "").strip()
        if not text:
            return _result(False, "tap_by_text 缺少 text 参数")
        try:
            index = int(params.get("index", 0) or 0)
        except (TypeError, ValueError):
            index = 0
        exact_raw = params.get("exact", True)
        exact = not (
            exact_raw is False
            or (isinstance(exact_raw, str) and exact_raw.strip().lower() in ("false", "0", "no"))
        )
        matches = ui_tree.find_nodes_by_text(snapshot.nodes, text, exact=exact)
        fuzzy = False
        if not matches and exact:
            matches = ui_tree.find_nodes_by_text(snapshot.nodes, text, exact=False)
            fuzzy = bool(matches)
        if not matches:
            return _result(False, f"当前 UI 树未找到文本: {text!r}", {"text": text})
        tappable = [node for node in matches if node.tappable and node.enabled]
        if not tappable:
            return _result(
                False,
                f"文本 {text!r} 命中 {len(matches)} 个节点，但均不可点击",
                {"text": text},
            )
        if index < 0 or index >= len(tappable):
            return _result(
                False,
                f"index={index} 超出可点击文本数量（共 {len(tappable)} 个）",
                {"text": text, "index": index},
            )
        node = tappable[index]
        trace = _trace_params(action, params, node)
        trace["ref"] = node.ref
        if node.dangerous:
            return _result(False, f"已拒绝敏感操作: {node.danger_reason}", trace)
        tap_result = driver.tap_xy(*node.center)
        suffix = "（包含匹配）" if fuzzy else ""
        return _result(
            bool(tap_result.get("success")),
            f"点击文本 {text!r}{suffix}（ref={node.ref}）-> {tap_result.get('message', '')}",
            trace,
        )

    if action == "tap_by_coordinates":
        try:
            x = int(params.get("x"))
            y = int(params.get("y"))
        except (TypeError, ValueError):
            return _result(False, f"坐标参数非法: x={params.get('x')!r}, y={params.get('y')!r}")
        width, height = screen_size
        if width > 0 and height > 0 and not (0 <= x < width and 0 <= y < height):
            return _result(
                False,
                f"坐标超出屏幕范围: ({x}, {y})，屏幕 {width}x{height}",
                {"x": x, "y": y},
            )
        node = ui_tree.find_node_at(snapshot.nodes, x, y)
        trace = {"x": x, "y": y}
        if node is None and not settings.STEPWISE_ALLOW_UNTARGETED_TAP:
            # UI 树截断/解析失败时，坐标点击可能落在未识别的购买、支付或
            # 系统控件上；默认拒绝，只有显式配置才放宽。
            return _result(
                False,
                "坐标未命中当前 UI 树中已解析的可点击节点，已拒绝执行；"
                "如确认安全可设置 STEPWISE_ALLOW_UNTARGETED_TAP=true",
                trace,
            )
        if node is not None:
            trace["ref"] = node.ref
            if node.dangerous:
                return _result(False, f"坐标命中敏感控件: {node.danger_reason}", trace)
            if not node.enabled:
                return _result(False, f"坐标命中不可用控件: {node.short_label()}", trace)
        tap_result = driver.tap_xy(x, y)
        target = f"（命中 {node.short_label()}）" if node is not None else "（未命中已解析节点）"
        return _result(
            bool(tap_result.get("success")),
            f"点击坐标 ({x}, {y}){target} -> {tap_result.get('message', '')}",
            trace,
        )

    if action == "input_text":
        text = str(params.get("text", "") or "")
        if len(text) > STEPWISE_MAX_INPUT_CHARS:
            return _result(
                False,
                f"输入内容过长（{len(text)} 字符，上限 {STEPWISE_MAX_INPUT_CHARS}）",
            )
        trace = _trace_params(action, params)
        ref = params.get("ref")
        if ref is not None:
            node = ui_tree.find_node_by_ref(snapshot.nodes, ref)
            if node is None:
                return _result(False, f"输入框 ref 不存在或已过期: {ref!r}", trace)
            trace["ref"] = node.ref
            if node.password:
                return _result(False, "已拒绝向密码字段输入内容", trace)
            if node.dangerous:
                return _result(False, f"已拒绝敏感操作: {node.danger_reason}", trace)
            if not node.enabled:
                return _result(False, f"输入框不可用: {node.short_label()}", trace)
            tap_result = driver.tap_xy(*node.center)
            if not tap_result.get("success"):
                return _result(False, f"点击输入框失败: {tap_result.get('message', '')}", trace)
            time.sleep(0.3)
        input_result = driver.input_text_focused(text)
        action_label = "清空" if text == "" else f"输入 {len(text)} 个字符"
        return _result(
            bool(input_result.get("success")),
            f"{action_label} -> {input_result.get('message', '')}",
            trace,
        )

    if action == "press_back":
        if back_count >= STEPWISE_MAX_BACK_ACTIONS:
            return _result(
                False,
                f"返回键已使用 {back_count} 次，达到上限 {STEPWISE_MAX_BACK_ACTIONS} 次",
            )
        back_result = driver.press_back()
        return _result(
            bool(back_result.get("success")),
            f"返回键 -> {back_result.get('message', '')}",
            {"action": "press_back"},
            back_used=bool(back_result.get("success")),
        )

    if action == "scroll":
        direction = str(params.get("direction", "down") or "down").strip().lower()
        if direction not in ("down", "up"):
            return _result(False, f"scroll direction 非法: {direction!r}", {"direction": direction})
        width, height = screen_size
        if width <= 0 or height <= 0:
            return _result(False, "无法获取屏幕尺寸，不能滚动")
        x = width // 2
        if direction == "down":
            start_y, end_y = int(height * 0.78), int(height * 0.25)
        else:
            start_y, end_y = int(height * 0.25), int(height * 0.78)
        scroll_result = driver.swipe(x, start_y, x, end_y, 300)
        return _result(
            bool(scroll_result.get("success")),
            f"向{direction}滚动 -> {scroll_result.get('message', '')}",
            {"direction": direction},
        )

    if action == "sleep":
        raw_seconds = params.get("seconds", 1)
        try:
            seconds = float(raw_seconds)
        except (TypeError, ValueError):
            return _result(False, f"sleep 参数非法: {raw_seconds!r}")
        if not math.isfinite(seconds) or seconds < 0 or seconds > 10:
            return _result(False, f"sleep 秒数超出 0~10: {raw_seconds!r}")
        time.sleep(seconds)
        return _result(True, f"等待 {seconds:g} 秒完成", {"seconds": seconds})

    if action == "done":
        raw_success = params.get("success", True)
        if isinstance(raw_success, str):
            success = raw_success.strip().lower() not in ("false", "0", "no", "否")
        else:
            success = bool(raw_success)
        evidence = str(params.get("evidence", "") or "").strip()[:200]
        trace = {"success": success, "evidence": evidence}
        if not success:
            if step_no < STEPWISE_MIN_STEPS_BEFORE_FAILURE:
                return _result(
                    False,
                    f"第 {step_no} 步过早判定失败；请继续尝试，只有被硬阻断时才 done success=false",
                    trace,
                )
            return _result(
                True,
                f"Agent 判定目标无法达成：{evidence or '未提供证据'}",
                trace,
                terminal=True,
                goal_achieved=False,
                goal_evidence=evidence,
            )
        if settings.STEPWISE_DONE_REQUIRE_EVIDENCE:
            if not evidence:
                return _result(
                    False,
                    "done success=true 缺少 evidence；请填写当前 UI 树中可见的完成证据原文",
                    trace,
                )
            if not ui_tree.snapshot_contains_text(snapshot, evidence):
                return _result(
                    False,
                    f"evidence {evidence!r} 未出现在当前 UI 树中，不能判定成功",
                    trace,
                )
            evidence_error = _stepwise_evidence_error(evidence, goal)
            if evidence_error:
                return _result(False, f"evidence 不合格：{evidence_error}", trace)
        return _result(
            True,
            f"目标达成，证据：{evidence}" if evidence else "目标达成（未要求证据）",
            trace,
            terminal=True,
            goal_achieved=True,
            goal_evidence=evidence,
        )

    return _result(False, f"不支持的动作类型: {action}")


def _build_stepwise_goal(requirement: str, test_cases: str) -> str:
    """拼接逐步模式的总体目标（需求 + 测试用例摘要）。"""
    goal = (requirement or "").strip()
    cases = (test_cases or "").strip()
    if cases and cases not in goal:
        goal = f"{goal}\n\n【参考测试用例】\n{cases[:2000]}" if goal else cases[:4000]
    return goal or "根据当前页面完成用户要求的测试任务"


def _run_stepwise_agent(
    driver: Any,
    requirement: str,
    test_cases: str,
    app_package: str,
    llm_key: str | None,
    llm_provider: str,
) -> Tuple[List[Dict[str, Any]], bool, str, int]:
    """逐步观察 UI 树、选择动作并执行，直到目标达成或达到上限。

    Args:
        driver: 支持逐步模式的执行驱动。
        requirement: 用户测试需求。
        test_cases: LLM 生成的测试用例（作为参考放入目标）。
        app_package: 被测应用包名。
        llm_key: 本次任务临时密钥（仅内存使用）。
        llm_provider: 生效的 LLM provider。

    Returns:
        Tuple[List[Dict[str, Any]], bool, str, int]:
        (动作执行记录, 目标是否达成, 达成证据, 实际步数)。
    """
    max_steps = max(1, int(settings.STEPWISE_MAX_STEPS))
    max_seconds = max(30, int(settings.STEPWISE_MAX_SECONDS))
    goal = _build_stepwise_goal(requirement, test_cases)
    system_prompt = _stepwise_system_prompt(app_package)
    results: List[Dict[str, Any]] = []
    started_at = time.monotonic()
    achieved = False
    evidence = ""
    steps = 0
    back_count = 0
    parse_failures = 0
    snapshot_failures = 0
    terminal_reached = False
    stop_message = ""
    # 循环保护：记录每个控件的连续无变化次数，阻止 LLM 在同一控件上反复空点
    last_record: Dict[str, Any] | None = None
    last_signature: Tuple[str, ...] | None = None
    last_target_key = ""
    no_progress_counts: Dict[str, int] = {}
    blocked_counts: Dict[str, int] = {}
    target_labels: Dict[str, str] = {}
    consecutive_no_progress = 0

    logger.info(
        f"逐步 UI 驱动启动：最多 {max_steps} 步 / {max_seconds} 秒，"
        f"目标: {goal[:100]}"
    )

    for step_no in range(1, max_steps + 1):
        elapsed = time.monotonic() - started_at
        if elapsed >= max_seconds:
            logger.warning(f"逐步 UI 驱动达到时间上限 {max_seconds} 秒")
            break

        captured, capture_error = _capture_ui_snapshot(driver, app_package)
        if captured is None:
            snapshot_failures += 1
            steps = step_no
            message = capture_error or "读取 UI 树失败"
            logger.warning(f"第 {step_no} 步观察失败: {message}")
            results.append(
                {
                    "action": "observe_ui",
                    "params": {},
                    "success": False,
                    "message": message,
                    "step": step_no,
                    "reason": "读取当前屏幕 UI 树",
                    "expect": "获取可操作控件",
                }
            )
            if snapshot_failures >= 3:
                logger.error("连续 3 次读取 UI 树失败，终止逐步执行")
                break
            time.sleep(1.0)
            continue
        snapshot_failures = 0
        snapshot, width, height = captured
        signature = _snapshot_signature(snapshot)

        # 与上一步动作后的快照比较，判断动作是否真正推动了页面
        if last_record is not None and last_signature is not None:
            changed = signature != last_signature
            last_record["ui_changed"] = changed
            if changed:
                # 页面已变化，之前的“无进展”计数不再适用
                consecutive_no_progress = 0
                no_progress_counts.clear()
                blocked_counts.clear()
            elif last_record.get("action") != "sleep":
                consecutive_no_progress += 1
                if (
                    last_target_key
                    and last_record.get("action") in STEPWISE_NO_PROGRESS_GUARDED_ACTIONS
                ):
                    no_progress_counts[last_target_key] = (
                        no_progress_counts.get(last_target_key, 0) + 1
                    )
            last_record = None
            last_target_key = ""
        last_signature = signature

        # 课程结算页出现强完成文案时直接结束，避免 LLM 继续点击领取奖励而错过证据
        auto_evidence = _stepwise_auto_completion_evidence(snapshot, goal)
        if auto_evidence:
            steps = step_no
            achieved = True
            evidence = auto_evidence
            terminal_reached = True
            message = f"检测到课程完成结算文案：{auto_evidence}，自动判定目标达成"
            logger.info(f"第 {step_no} 步{message}")
            results.append(
                {
                    "action": "done",
                    "params": {"success": True, "evidence": auto_evidence},
                    "success": True,
                    "message": message,
                    "step": step_no,
                    "reason": "UI 树出现课程完成结算文案",
                    "expect": "结束逐步执行",
                }
            )
            pet_broadcaster.asserting()
            pet_broadcaster.step_succeeded("done")
            break

        blocked_targets = [
            target_labels.get(key, key)
            for key, count in no_progress_counts.items()
            if count >= STEPWISE_MAX_NO_PROGRESS_REPEATS
        ]
        user_prompt = _stepwise_user_prompt(
            goal=goal,
            step_no=step_no,
            max_steps=max_steps,
            remaining_seconds=int(max_seconds - elapsed),
            snapshot=snapshot,
            screen_size=(width, height),
            history=results,
            blocked_targets=blocked_targets,
            consecutive_no_progress=consecutive_no_progress,
        )
        raw_output = _invoke_llm(
            system_prompt, user_prompt, llm_key, llm_provider
        )
        action_item, parse_error = _parse_stepwise_action(raw_output)
        if action_item is None:
            # 格式错误时追加纠正提示重试一次，避免单次抖动浪费一步
            retry_prompt = (
                user_prompt
                + f"\n\n【格式错误，必须修正】上一次输出无法解析为动作 JSON：{parse_error}。"
                "请只输出一个 JSON 对象，必须包含字符串 action 与对象 params，不要输出其他文字。"
            )
            raw_output = _invoke_llm(
                system_prompt, retry_prompt, llm_key, llm_provider
            )
            action_item, parse_error = _parse_stepwise_action(raw_output)
        if action_item is None:
            parse_failures += 1
            steps = step_no
            logger.warning(f"第 {step_no} 步动作解析失败: {parse_error}")
            results.append(
                {
                    "action": "invalid_llm_output",
                    "params": {},
                    "success": False,
                    "message": f"动作 JSON 解析失败: {parse_error}",
                    "step": step_no,
                    "reason": "LLM 输出格式错误",
                    "expect": "输出合法动作 JSON",
                }
            )
            if parse_failures >= STEPWISE_MAX_PARSE_FAILURES:
                logger.error("连续多次动作解析失败，终止逐步执行")
                break
            continue
        parse_failures = 0

        action = action_item["action"]
        target_key, target_label = _stepwise_target_key(
            action_item, snapshot, (width, height)
        )
        if target_key and target_label:
            target_labels[target_key] = target_label
        if (
            target_key
            and action in STEPWISE_NO_PROGRESS_GUARDED_ACTIONS
            and no_progress_counts.get(target_key, 0) >= STEPWISE_MAX_NO_PROGRESS_REPEATS
        ):
            blocked_counts[target_key] = blocked_counts.get(target_key, 0) + 1
            message = (
                f"已阻止重复无效操作：{target_label or target_key} 已连续 "
                f"{no_progress_counts[target_key]} 次操作后页面未变化。"
                "请选择其他控件，或使用 scroll / press_back 改变页面状态。"
            )
            logger.warning(f"第 {step_no} 步{message}")
            steps = step_no
            results.append(
                {
                    "action": action,
                    "params": redact_action_params(
                        action, action_item.get("params", {}) or {}
                    ),
                    "success": False,
                    "message": message,
                    "step": step_no,
                    "reason": action_item.get("reason", ""),
                    "expect": action_item.get("expect", ""),
                }
            )
            pet_broadcaster.step_failed(action)
            if blocked_counts[target_key] >= STEPWISE_MAX_BLOCKED_ATTEMPTS:
                stop_message = (
                    f"同一控件连续 {STEPWISE_MAX_BLOCKED_ATTEMPTS} 次被阻止，"
                    "判定陷入重复无效操作，终止逐步执行"
                )
                logger.error(stop_message)
                break
            time.sleep(0.2)
            continue

        pet_broadcaster.executing_action(action)
        if action == "done":
            pet_broadcaster.asserting()
        result = _execute_stepwise_action(
            driver=driver,
            action_item=action_item,
            snapshot=snapshot,
            screen_size=(width, height),
            step_no=step_no,
            back_count=back_count,
            goal=goal,
        )
        steps = step_no
        record = {
            "action": action,
            "params": result.get("trace_params", {}),
            "success": bool(result.get("success")),
            "message": str(result.get("message", "")),
            "step": step_no,
            "reason": action_item.get("reason", ""),
            "expect": action_item.get("expect", ""),
        }
        results.append(record)
        last_record = record
        last_target_key = target_key
        if result.get("success"):
            pet_broadcaster.step_succeeded(action)
        else:
            pet_broadcaster.step_failed(action)
        if result.get("back_used"):
            back_count += 1
        if result.get("terminal"):
            terminal_reached = True
            achieved = bool(result.get("goal_achieved"))
            evidence = str(result.get("goal_evidence") or "")
            logger.info(
                f"逐步执行结束：目标{'达成' if achieved else '未达成'}，"
                f"共 {steps} 步，证据: {evidence[:120]}"
            )
            break
        time.sleep(
            max(0.0, float(settings.STEPWISE_ACTION_WAIT_SECONDS))
            if result.get("success")
            else 0.5
        )

    if not terminal_reached:
        elapsed = time.monotonic() - started_at
        reason = (
            f"达到时间上限 {max_seconds} 秒"
            if elapsed >= max_seconds
            else f"达到最大步数 {max_steps} 步"
        )
        message = stop_message or f"逐步执行停止：{reason}，目标未达成"
        logger.warning(message)
        results.append(
            {
                "action": "stepwise_stopped",
                "params": {},
                "success": False,
                "message": message,
                "step": steps + 1 if steps else 0,
                "reason": "执行上限",
                "expect": "在步数/时间上限内完成目标",
            }
        )
    return results, achieved, evidence, steps


# ==================== 节点 4：执行测试 ====================
def _execute_action(
    driver: Any, action_item: Dict[str, Any], app_package: str = ""
) -> ActionResult:
    """执行单个已规划动作，校验参数并把异常收敛为失败结果。

    对 click_by_id / input_text / clear_text 会校验 resource-id 的包名前缀，
    拒绝操作其他应用的控件（防止 RAG 污染或 LLM 幻觉导致跳错 App）。

    Args:
        driver: 当前执行后端（满足 DeviceDriver 协议）。
        action_item: 动作字典（含 action 与 params）。
        app_package: 本次任务被测应用包名，用于动作作用域校验。

    Returns:
        ActionResult: {success, message}。
    """
    action = str(action_item.get("action", "")).strip()
    params = action_item.get("params")
    if not isinstance(params, dict):
        params = {}

    if action == "restart_app":
        return driver.restart_app()

    if action in ("click_by_id", "input_text", "clear_text"):
        resource_id = str(params.get("resource_id", "")).strip()
        if not resource_id:
            return {"success": False, "message": f"{action} 缺少 resource_id 参数"}
        if not _resource_id_allowed(resource_id, app_package):
            return {
                "success": False,
                "message": (
                    f"已拒绝跨应用动作：resource-id {resource_id} 不属于被测应用 "
                    f"{app_package}"
                ),
            }

        if action == "click_by_id":
            return driver.click_by_id(resource_id)

        if action == "clear_text":
            return driver.clear_text(resource_id)

        text = params.get("text", "")
        text = "" if text is None else str(text)
        # 兼容 LLM 仍用空字符串 input_text 表示“清空”的旧写法
        if text == "":
            return driver.clear_text(resource_id)
        return driver.input_text(resource_id, text)

    if action == "sleep":
        raw_seconds = params.get("seconds", 1)
        try:
            seconds = float(raw_seconds)
        except (TypeError, ValueError):
            return {
                "success": False,
                "message": f"sleep 参数 seconds 非法: {raw_seconds!r}",
            }
        if not math.isfinite(seconds) or seconds < 0 or seconds > MAX_SLEEP_SECONDS:
            return {
                "success": False,
                "message": (
                    f"sleep 参数超出允许范围（0~{MAX_SLEEP_SECONDS} 秒）: {raw_seconds!r}"
                ),
            }
        time.sleep(seconds)
        return {"success": True, "message": f"等待 {seconds:g} 秒完成"}

    return {"success": False, "message": f"不支持的动作类型: {action}"}


def _append_execute_attempt(
    state: AgentState,
    results: List[Dict[str, Any]],
    error: str,
    mode: str,
) -> List[Dict[str, Any]]:
    """把本次执行尝试追加到历史尝试列表。

    ``execute_results`` 会被重规划后的新一轮结果覆盖，而
    ``execute_attempts`` 保留每一轮，供报告展示完整轨迹。

    Args:
        state: 当前状态（读取已有 execute_attempts）。
        results: 本次尝试的动作结果。
        error: 本次尝试的流程级错误。
        mode: "static" 或 "stepwise"。

    Returns:
        List[Dict[str, Any]]: 追加后的尝试列表。
    """
    attempts = list(state.get("execute_attempts") or [])
    attempts.append(
        {
            "attempt": len(attempts) + 1,
            "mode": mode,
            "results": list(results),
            "error": error or "",
        }
    )
    return attempts


def execute_test(state: AgentState) -> AgentState:
    """节点4：按配置选择执行后端，在设备（模拟器/真机）上执行动作。

    执行后端由 user_device_backend（单次任务覆盖）或 .env 的
    DEVICE_BACKEND 决定，默认 Appium；mobile_mcp 为实验后端。
    会话严格使用 try-finally 保证 close() 一定被调用，防止设备会话占用。
    动作执行前清空 logcat 标记起点，执行完成后抓取日志与截图作为缺陷分析依据。

    当 STEPWISE_AGENT_ENABLED=true（或前端单次覆盖）且后端为 Appium 时，
    改为逐步 UI 驱动：每一步读取实时 UI 树，由 LLM 选择 tap_by_ref /
    tap_by_text / 坐标点击等动作，执行后再次观察，直到 done 或达到上限。

    Args:
        state: LangGraph 共享状态（读取 action_list、设备与后端覆盖值）。

    Returns:
        AgentState: 状态增量更新（写入 execute_results、execute_attempts、
        run_id、adb_log_path、screenshot_path、adb_log_content、
        device_backend、实际设备信息，以及 stepwise_mode / goal_achieved /
        goal_evidence / stepwise_steps；连接失败时写入 error）。
    """
    logger.info("===== 节点4: 执行自动化测试 =====")
    action_list = state.get("action_list", [])
    execute_results: List[Dict[str, Any]] = []
    stepwise_mode = _stepwise_mode_enabled(state)
    goal_achieved = False
    goal_evidence = ""
    stepwise_steps = 0
    stepwise_info = {
        "stepwise_mode": stepwise_mode,
        "stepwise_steps": 0,
        "goal_achieved": False,
        "goal_evidence": "",
    }
    # 本次任务的独立产物目录；state 缺少 run_id 时自动补建（兼容直接调用节点）
    run_paths = run_context.ensure_run_paths(state.get("run_id"))
    run_id = run_paths.run_id
    attempt_mode = "stepwise" if stepwise_mode else "static"

    # 设备模式与执行后端：前端覆盖值优先（None/空字符串表示使用 .env 配置）
    user_is_real = state.get("user_is_real_device")
    user_udid = (state.get("user_udid") or "").strip() or None
    backend = resolve_device_backend(state.get("user_device_backend"))
    backend_label = "mobile-mcp" if backend == "mobile_mcp" else "Appium"
    app_package, app_activity = _resolve_app_config(state)
    device_info = _resolve_effective_device(user_is_real, user_udid)
    adb_serial = _resolve_adb_serial(user_is_real, user_udid)
    logger.info(
        f"执行后端: {backend_label}，设备: {device_info['device_mode']} / "
        f"{device_info['device_name']}，被测应用: {app_package}/{app_activity}，"
        f"执行模式: {'逐步 UI 驱动' if stepwise_mode else '静态动作规划'}"
    )

    # 桌宠单向播报：只发送“启动App”固定状态，不携带设备/应用敏感信息。
    pet_broadcaster.launching_app()

    # 设备预检：唤醒并检查锁屏，避免自动化动作落在锁屏界面；
    # 程序不会修改、保存或猜测锁屏密码，安全锁屏需用户手动解锁。
    device_ready, device_message = adb_tool.prepare_device(device_serial=adb_serial)
    if not device_ready:
        logger.error(f"设备预检失败: {device_message}")
        pet_broadcaster.step_failed("launch_app")
        return {
            "run_id": run_id,
            "execute_results": [],
            "execute_attempts": _append_execute_attempt(
                state, [], f"设备预检失败: {device_message}", attempt_mode
            ),
            "adb_log_path": "",
            "screenshot_path": "",
            "adb_log_content": "",
            "device_backend": backend,
            "app_package": app_package,
            "app_activity": app_activity,
            **device_info,
            **stepwise_info,
            "error": f"设备预检失败: {device_message}",
        }

    try:
        driver = create_device_driver(
            backend=backend,
            is_real_device=user_is_real if isinstance(user_is_real, bool) else None,
            udid=user_udid,
            app_package=app_package,
            app_activity=app_activity,
        )
    except Exception as e:
        error_msg = f"{backend_label} 执行驱动初始化失败: {e}"
        logger.error(error_msg, exc_info=True)
        pet_broadcaster.step_failed("launch_app")
        return {
            "run_id": run_id,
            "execute_results": [],
            "execute_attempts": _append_execute_attempt(
                state, [], error_msg, attempt_mode
            ),
            "adb_log_path": "",
            "screenshot_path": "",
            "adb_log_content": "",
            "device_backend": backend,
            "app_package": app_package,
            "app_activity": app_activity,
            **device_info,
            **stepwise_info,
            "error": error_msg,
        }

    if not driver.connect():
        if backend == "mobile_mcp":
            error_msg = (
                "mobile-mcp 连接失败，请确认 Node.js 20+/npx 可用、安卓设备已连接，"
                "且已安装 requirements.txt 中的 mcp 依赖"
            )
        else:
            error_msg = (
                "Appium 连接失败，请确认 Appium 服务与安卓设备（模拟器/真机）已启动"
            )
        # 连接失败时驱动内部可能已创建部分会话，同样需要释放
        try:
            driver.close()
        except Exception as close_error:
            logger.warning(f"连接失败后释放设备会话时出现异常: {close_error}")
        pet_broadcaster.step_failed("launch_app")
        return {
            "run_id": run_id,
            "execute_results": [],
            "execute_attempts": _append_execute_attempt(
                state, [], error_msg, attempt_mode
            ),
            "adb_log_path": "",
            "screenshot_path": "",
            "adb_log_content": "",
            "device_backend": backend,
            "app_package": app_package,
            "app_activity": app_activity,
            **device_info,
            **stepwise_info,
            "error": error_msg,
        }

    # 桌宠单向播报：启动会话成功（App 已被带到前台）。
    pet_broadcaster.step_succeeded("launch_app")

    if stepwise_mode and not supports_stepwise(driver):
        logger.warning(
            "当前执行驱动未实现逐步 UI 驱动扩展（get_page_source/tap_xy 等），"
            "本次回退到静态动作规划"
        )
        stepwise_mode = False
        stepwise_info["stepwise_mode"] = False
        attempt_mode = "static"

    # 在动作执行前清空 logcat，确保后续抓取的是本轮测试期间的日志
    adb_tool.clear_logcat(device_serial=adb_serial)
    # 记录执行前 PID；若 App 在测试中崩溃重启，执行后 PID 会变化，前后取并集
    app_pids_before = adb_tool.get_app_pids(app_package, device_serial=adb_serial)

    try:
        if stepwise_mode:
            # 逐步 UI 驱动：每一步读取实时 UI 树，由 LLM 选择一个动作执行。
            try:
                (
                    stepwise_results,
                    goal_achieved,
                    goal_evidence,
                    stepwise_steps,
                ) = _run_stepwise_agent(
                    driver=driver,
                    requirement=state.get("requirement", ""),
                    test_cases=state.get("test_cases", ""),
                    app_package=app_package,
                    llm_key=_resolve_user_key(state),
                    llm_provider=_resolve_provider(state),
                )
                execute_results.extend(stepwise_results)
            except Exception as e:
                # 逐步循环异常不应阻断日志/截图采集与报告生成。
                logger.error(f"逐步 UI 驱动执行异常: {e}", exc_info=True)
                execute_results.append(
                    {
                        "action": "stepwise_error",
                        "params": {},
                        "success": False,
                        "message": f"逐步 UI 驱动执行异常: {e}",
                        "step": stepwise_steps,
                        "reason": "异常",
                        "expect": "继续完成目标",
                    }
                )
        else:
            if not action_list:
                logger.warning("动作列表为空，跳过动作执行，仅采集设备日志与截图")
            for idx, action_item in enumerate(action_list, start=1):
                action = action_item.get("action", "")
                params = action_item.get("params", {})
                safe_params = redact_action_params(action, params)
                logger.info(
                    f"执行第 {idx}/{len(action_list)} 个动作: {action}, "
                    f"参数: {safe_params}"
                )
                # 只播报动作类型（映射为固定中文标签），不播报 params/resource-id。
                pet_broadcaster.executing_action(action)
                result = _execute_action(driver, action_item, app_package=app_package)
                if not result.get("success"):
                    logger.warning(
                        f"动作执行失败 [{action}]: {result.get('message', '')}"
                    )
                    pet_broadcaster.step_failed(action)
                else:
                    pet_broadcaster.step_succeeded(action)
                # 结果中只保留脱敏后的参数，避免输入内容进入状态/报告/重规划提示词
                execute_results.append({**redact_action_item(action_item), **result})
    finally:
        # 无论执行是否异常，都确保会话释放，防止设备会话占用。
        # close 失败不应阻断后续日志/截图采集与报告生成。
        try:
            driver.close()
        except Exception as close_error:
            logger.warning(f"释放设备会话时出现异常: {close_error}")

    # 测试动作执行完毕，抓取 adb 日志与截图作为缺陷分析依据
    # 目标设备与本次执行会话保持一致（真机覆盖参数优先）
    adb_log_path = adb_tool.capture_logcat(
        tag="agent_run",
        device_serial=adb_serial,
        clear_first=False,
        output_dir=str(run_paths.logs),
        buffers="main,system,crash",
    )
    screenshot_path = adb_tool.capture_screenshot(
        tag="agent_run", device_serial=adb_serial, output_dir=str(run_paths.screenshots)
    )
    app_pids_after = adb_tool.get_app_pids(app_package, device_serial=adb_serial)
    app_pids = sorted(set(app_pids_before) | set(app_pids_after))
    adb_log_content = (
        adb_tool.extract_relevant_log(
            adb_log_path,
            app_package=app_package,
            app_pids=app_pids,
            keywords=LOG_DEFECT_KEYWORDS,
        )
        if adb_log_path
        else ""
    )

    return {
        "run_id": run_id,
        "execute_results": execute_results,
        "execute_attempts": _append_execute_attempt(
            state, execute_results, "", attempt_mode
        ),
        "adb_log_path": adb_log_path,
        "screenshot_path": screenshot_path,
        "adb_log_content": adb_log_content,
        "device_backend": backend,
        "app_package": app_package,
        "app_activity": app_activity,
        "stepwise_mode": stepwise_mode,
        "stepwise_steps": stepwise_steps,
        "goal_achieved": goal_achieved,
        "goal_evidence": goal_evidence,
        **device_info,
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
    # 桌宠单向播报：将缺陷分析视为本轮“断言校验”阶段。
    pet_broadcaster.asserting()
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

    使用 Pydantic 模型统一类型/严重级别，并过滤缺少描述的无效条目；
    如果所有条目都无效，则返回错误以触发关键字证据兜底。

    Args:
        raw_output: LLM 原始输出文本。

    Returns:
        Tuple[List[Dict[str, Any]], str]: (缺陷列表, 错误描述)。
        成功时错误描述为空字符串；失败时缺陷列表为空。
    """
    parsed, error = _extract_json_array(raw_output)
    if error:
        return [], error
    if not parsed:
        # 模型明确返回 []，表示未发现缺陷；不应再用关键字证据覆盖
        return [], ""

    defects: List[Dict[str, Any]] = []
    errors: List[str] = []
    for item in parsed:
        if not isinstance(item, dict):
            errors.append("缺陷条目不是 JSON 对象")
            continue
        try:
            defects.append(DefectItem.model_validate(item).model_dump())
        except Exception as exc:
            errors.append(str(exc))

    if not defects:
        return [], errors[0] if errors else "没有合法的缺陷条目"
    if errors:
        logger.warning(f"缺陷解析丢弃 {len(errors)} 个非法条目: {errors[:3]}")
    return defects, ""


# ==================== 节点 6：生成测试报告 ====================
def build_report(state: AgentState) -> AgentState:
    """节点6：调用报告工具，将测试全流程数据生成为 Markdown 缺陷测试报告。

    Args:
        state: LangGraph 共享状态（读取 requirement、test_cases、
        execute_results、execute_attempts、defects、adb_log_path、
        screenshot_path、run_id、error）。

    Returns:
        AgentState: 状态增量更新（写入 report_path；失败时保留原 error
        或写入报告生成失败提示）。
    """
    logger.info("===== 节点6: 生成 Markdown 测试报告 =====")
    app_package, app_activity = _resolve_app_config(state)
    run_paths = run_context.ensure_run_paths(state.get("run_id"))
    report_path = report_tool.generate_markdown_report(
        requirement=state.get("requirement", ""),
        test_cases=state.get("test_cases", ""),
        execute_results=state.get("execute_results", []),
        defects=state.get("defects", []),
        log_path=state.get("adb_log_path", ""),
        screenshot_path=state.get("screenshot_path", ""),
        device_backend=state.get("device_backend", ""),
        device_mode=state.get("device_mode", ""),
        device_name=state.get("device_name", ""),
        device_platform=state.get("device_platform", ""),
        app_package=state.get("app_package", "") or app_package,
        app_activity=state.get("app_activity", "") or app_activity,
        stepwise_mode=bool(state.get("stepwise_mode")),
        goal_achieved=state.get("goal_achieved") if state.get("stepwise_mode") else None,
        goal_evidence=state.get("goal_evidence", ""),
        execute_attempts=state.get("execute_attempts", []),
        error=state.get("error", ""),
        output_dir=str(run_paths.reports),
        run_id=run_paths.run_id,
    )
    if not report_path:
        return {
            "report_path": "",
            "error": state.get("error") or "测试报告生成失败",
        }
    return {"report_path": report_path}
