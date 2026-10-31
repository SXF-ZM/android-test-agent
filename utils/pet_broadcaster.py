# -*- coding: utf-8 -*-
"""dsh-pet（鲸鱼桌宠）单向状态播报模块。

安全说明（重要）：
1. 本模块只做“测试 Agent → 桌宠”的单向 HTTP 推送，不提供右键菜单、
   不轮询桌宠状态、不接收桌宠返回内容，也不做任何双向交互。
2. 只允许推送固定词表中的简短纯文本状态：不上传 adb UI 树、截图、
   控件文字、resource-id、输入内容、日志、报告路径或密钥。
3. 总开关由环境变量 PET_ENABLED 控制（config/settings.py）。
   涉密在研设备测试必须设置 PET_ENABLED=false；此时所有播报方法在入口
   直接返回，不创建线程、不建立连接、不发起任何 HTTP 请求。
4. 所有网络请求均设置超时并捕获全部异常；桌宠未启动、端口不通、服务
   异常时静默跳过。播报在后台 daemon 线程中执行，绝不阻塞或中断
   uiautomator2 / Appium 的安卓测试主流程。

接口约定：
    仅调用 dsh-pet 的 GET /say?text=<文本> 接口，忽略响应体。
    默认地址为 http://127.0.0.1:47831，可通过 PET_BASE_URL 覆盖。
"""

from __future__ import annotations

import queue
import re
import threading
from typing import Optional
from urllib.parse import urlsplit

from config.settings import settings

# requests 为可选依赖：即使运行环境缺失该库，也不允许影响测试主流程。
try:  # pragma: no cover - 依赖缺失时走降级分支
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]


# ==================== 固定状态词表 ====================
# 只使用固定词表，禁止将调用方传入的自由文本直接拼入播报内容。
STATUS_PLANNING = "规划用例"
STATUS_LAUNCH_APP = "启动App"
STATUS_EXECUTING = "执行操作"
STATUS_ASSERTION = "断言校验"
STATUS_STEP_SUCCESS = "步骤成功"
STATUS_STEP_FAILURE = "步骤失败"
STATUS_FINISHED = "整套测试结束"

# Agent 动作类型到安全中文标签的映射；未识别动作统一显示“测试动作”，
# 从而避免把 params、resource-id 或输入内容泄露给桌宠。
_ACTION_LABELS = {
    "click_by_id": "点击元素",
    "input_text": "输入文本",
    "clear_text": "清空文本",
    "sleep": "等待",
    "restart_app": "重启应用",
    "launch_app": "启动应用",
    "test_run": "测试执行",
    # 逐步 UI 驱动模式的动作同样只映射为固定中文标签，不传控件内容
    "tap_by_ref": "点击元素",
    "tap_by_text": "点击文字",
    "tap_by_coordinates": "点击坐标",
    "press_back": "返回",
    "scroll": "滚动页面",
    "done": "校验目标",
    "stepwise_agent": "逐步执行",
}

# ==================== 安全清洗规则 ====================
# 单条播报最大字符数，保证桌宠只显示一行简短状态。
_MAX_TEXT_LENGTH = 80
# 请求超时（连接, 读取），单位秒；本地桌宠异常时最多等待约 1 秒。
_REQUEST_TIMEOUT = (0.5, 1.0)
# 后台队列上限；超出时直接丢弃最新状态，绝不阻塞测试线程。
_QUEUE_MAXSIZE = 256
# dsh-pet 单向播报端点。
_SAY_PATH = "/say"

# 控制字符、方向控制符与零宽字符：全部移除，避免日志/终端注入。
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f]"
)
# HTML/XML 标签：移除，确保只发送纯文本。
_HTML_TAG_RE = re.compile(r"<[^>]*>")
# 敏感内容特征：UI 树、控件属性、截图/base64 等一旦出现，整段 detail 丢弃。
_FORBIDDEN_MARKERS_RE = re.compile(
    r"(?:<hierarchy|resource-id|bounds\s*=|content-desc|text\s*=|"
    r"data:image|base64,|screenshot|uiautomator\s+dump|"
    r"截图|UI\s*树|界面树|控件文字|控件树|图像数据)",
    re.IGNORECASE,
)
# URL 与常见绝对路径：即使误传也做遮蔽处理（防御性清洗）。
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s，。；、]*")
_UNIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[\w.-]+/)+[\w.-]*")
_WHITESPACE_RE = re.compile(r"\s+")


def _safe_int(value: object, upper: int = 99999) -> int:
    """把计数转换为 0~upper 的安全整数，异常值按 0 处理。

    Args:
        value: 调用方传入的计数。
        upper: 允许的最大值。

    Returns:
        int: 清洗后的非负整数。
    """
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(number, upper))


class PetBroadcaster:
    """dsh-pet 单向播报器（线程安全、失败静默、默认关闭）。

    安全说明：
        该可视化模块可一键关闭（PET_ENABLED=false），关闭时不启动后台线程、
        不创建 HTTP 会话、不发起任何请求，适配涉密在研设备测试场景。
        启用后也只推送固定词表状态，绝不传递 UI 树、截图或控件文字。

    工作方式：
        首次启用播报时懒加载一个 daemon 工作线程，通过有界队列串行发送；
        HTTP 请求带超时且忽略响应，任何异常都在线程内吞掉，主测试流程
        不会感知桌宠是否存在。
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        base_url: Optional[str] = None,
        timeout: tuple[float, float] = _REQUEST_TIMEOUT,
    ) -> None:
        """初始化播报器。

        Args:
            enabled: 是否启用；None 表示读取 settings.PET_ENABLED。
            base_url: dsh-pet 服务地址；None 表示读取 settings.PET_BASE_URL。
            timeout: (连接超时, 读取超时)，单位秒。
        """
        raw_enabled = settings.PET_ENABLED if enabled is None else bool(enabled)
        raw_base_url = settings.PET_BASE_URL if base_url is None else base_url
        self._base_url = self._normalize_base_url(raw_base_url)
        # 允许 PET_BASE_URL 直接填到 /say，也允许只填主机地址。
        self._say_url = ""
        if self._base_url:
            self._say_url = (
                self._base_url
                if self._base_url.endswith(_SAY_PATH)
                else f"{self._base_url}{_SAY_PATH}"
            )

        # 依赖缺失、地址非法或总开关关闭时，整个模块保持空转。
        self._enabled = bool(raw_enabled and requests is not None and self._base_url)
        self._timeout = (
            max(0.2, min(float(timeout[0]), 3.0)),
            max(0.2, min(float(timeout[1]), 3.0)),
        )
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """返回当前是否启用播报（不发起任何网络请求）。"""
        return self._enabled

    # ==================== 对外状态接口 ====================
    def planning_test_cases(self) -> None:
        """播报“规划用例”状态。"""
        self._enqueue(STATUS_PLANNING, "正在生成测试用例")

    def launching_app(self) -> None:
        """播报“启动App”状态。"""
        self._enqueue(STATUS_LAUNCH_APP, "正在启动被测应用")

    def executing_action(self, action_name: str = "") -> None:
        """播报“执行操作”状态。

        Args:
            action_name: Agent 动作类型（如 click_by_id）；只映射为固定中文标签，
                不会传递 resource-id、params 或输入内容。
        """
        self._enqueue(STATUS_EXECUTING, f"正在{self._action_label(action_name)}")

    def asserting(self) -> None:
        """播报“断言校验”状态。"""
        self._enqueue(STATUS_ASSERTION, "正在分析执行结果")

    def step_succeeded(self, action_name: str = "") -> None:
        """播报“步骤成功”状态。

        Args:
            action_name: Agent 动作类型；仅使用固定中文标签。
        """
        self._enqueue(STATUS_STEP_SUCCESS, self._action_label(action_name))

    def step_failed(self, action_name: str = "") -> None:
        """播报“步骤失败”状态。

        Args:
            action_name: Agent 动作类型；仅使用固定中文标签。
        """
        self._enqueue(STATUS_STEP_FAILURE, self._action_label(action_name))

    def test_finished(self, total: int = 0, succeeded: int = 0, failed: int = 0) -> None:
        """播报“整套测试结束”状态。

        Args:
            total: 动作总数。
            succeeded: 成功动作数。
            failed: 失败动作数。

        Note:
            只发送计数，不发送报告路径、缺陷描述或日志内容。
        """
        total = _safe_int(total)
        succeeded = _safe_int(succeeded)
        failed = _safe_int(failed)
        self._enqueue(
            STATUS_FINISHED,
            f"共 {total} 步，成功 {succeeded}，失败 {failed}",
        )

    # ==================== 内部实现 ====================
    @staticmethod
    def _normalize_base_url(raw_url: object) -> str:
        """校验并规范化桌宠服务地址。

        仅接受 http/https、带主机名且不含账号密码/查询串/锚点的地址。
        非法地址返回空字符串，播报器随之为空转状态。

        Args:
            raw_url: 原始地址。

        Returns:
            str: 规范化后的地址；非法时为空字符串。
        """
        url = str(raw_url or "").strip().rstrip("/")
        if not url:
            return ""
        try:
            parts = urlsplit(url)
        except ValueError:
            return ""
        if parts.scheme not in ("http", "https"):
            return ""
        if not parts.netloc or parts.username or parts.password:
            return ""
        if parts.query or parts.fragment:
            return ""
        return url

    @staticmethod
    def _action_label(action_name: object) -> str:
        """把动作类型转换为固定安全标签。

        Args:
            action_name: 动作类型。

        Returns:
            str: 中文标签；未识别动作统一为“测试动作”。
        """
        try:
            key = str(action_name or "").strip().lower()
        except Exception:
            # 异常对象也不允许把异常信息带进播报内容。
            return "测试动作"
        return _ACTION_LABELS.get(key, "测试动作")

    @staticmethod
    def _sanitize_text(value: object) -> str:
        """清洗单段文本，确保为简短纯文本。

        处理顺序：移除控制字符 → 移除标签 → 敏感特征拦截 → 遮蔽 URL/路径
        → 合并空白 → 截断。

        Args:
            value: 待清洗文本。

        Returns:
            str: 清洗后的文本；命中敏感特征时返回空字符串。
        """
        raw_text = _CONTROL_CHARS_RE.sub("", str(value or ""))
        # 先检查原始文本，再移除标签：避免 XML 属性被标签清洗后漏检。
        if _FORBIDDEN_MARKERS_RE.search(raw_text):
            # UI 树、控件属性、截图等内容一律丢弃，只保留固定状态词。
            return ""
        text = _HTML_TAG_RE.sub("", raw_text)
        if _FORBIDDEN_MARKERS_RE.search(text):
            return ""
        text = _URL_RE.sub("[链接已隐藏]", text)
        text = _WINDOWS_PATH_RE.sub("[路径已隐藏]", text)
        text = _UNIX_PATH_RE.sub("[路径已隐藏]", text)
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if len(text) > _MAX_TEXT_LENGTH:
            text = text[: _MAX_TEXT_LENGTH - 1].rstrip() + "…"
        return text

    def _enqueue(self, status: str, detail: str = "") -> None:
        """构造安全播报文本并投递到后台队列。

        本方法是所有对外接口的唯一出口，任何异常都在此吞掉，
        保证桌宠模块永远不会向上抛出异常影响测试主流程。

        Args:
            status: 固定状态词。
            detail: 固定词表生成的简短说明。
        """
        if not self._enabled:
            # PET_ENABLED=false：完全空转，不 sanitize、不建线程、不发请求。
            return
        try:
            safe_status = self._sanitize_text(status)
            safe_detail = self._sanitize_text(detail)
            if not safe_status:
                return
            text = f"{safe_status}：{safe_detail}" if safe_detail else safe_status
            # 对最终拼接结果再截断一次，确保单条播报始终简短。
            text = self._sanitize_text(text)
            if not text:
                return
            self._ensure_worker()
            self._queue.put_nowait(text)
        except Exception:
            # 队列满、线程创建失败、依赖异常等：静默跳过。
            return

    def _ensure_worker(self) -> None:
        """懒加载后台 daemon 发送线程（不轮询、不反向读取）。"""
        if self._worker is not None and self._worker.is_alive():
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            worker = threading.Thread(
                target=self._worker_loop,
                name="dsh-pet-broadcaster",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _worker_loop(self) -> None:
        """后台串行消费播报队列；所有请求异常均静默忽略。"""
        session = None
        try:
            if requests is not None:
                session = requests.Session()
                # 不读取系统代理/.netrc，避免涉密环境中的状态被环境代理转发。
                session.trust_env = False
                session.headers.update(
                    {
                        "User-Agent": "android-test-agent-pet-broadcaster/1.0",
                        "Accept": "text/plain",
                    }
                )
        except Exception:
            session = None

        while True:
            text = self._queue.get()
            try:
                if session is not None:
                    self._send(session, text)
            except Exception:
                # 桌宠离线、端口拒绝、超时、服务异常：静默跳过。
                pass
            finally:
                self._queue.task_done()

    def _send(self, session: "requests.Session", text: str) -> None:
        """发送单条状态到 dsh-pet 的 /say 接口。

        Args:
            session: 后台线程独占的 requests 会话。
            text: 已清洗的简短纯文本。
        """
        if not self._say_url or not text:
            return
        # 单向播报：不读取响应体，不解析返回内容，不触发任何回调。
        response = session.get(
            self._say_url,
            params={"text": text},
            timeout=self._timeout,
            allow_redirects=False,
            stream=True,
        )
        try:
            response.close()
        finally:
            pass


# 全局单例：默认读取 PET_ENABLED / PET_BASE_URL；关闭时不产生任何副作用。
pet_broadcaster = PetBroadcaster()
