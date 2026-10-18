# -*- coding: utf-8 -*-
"""
HTTP 网络请求模块（异步，不阻塞 UI）
基于 QNetworkAccessManager 封装 GET / POST（JSON 与 multipart 文件上传），
回调式异步处理，全部信号在 Qt 主线程触发，可直接安全操作 UI。
"""

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from desktop.core.app_log import log_error, log_info

# 请求超时（毫秒）：任务执行链路较长，设置为 30 分钟
REQUEST_TIMEOUT_MS: int = 30 * 60 * 1000


class HttpClient:
    """异步 HTTP 客户端封装（QNetworkAccessManager 单例持有）。

    全部回调在 Qt 主线程执行，可直接更新 UI 控件。
    """

    def __init__(self) -> None:
        """初始化网络访问管理器。"""
        self._manager = QNetworkAccessManager()

    @staticmethod
    def _make_request(url: str) -> QNetworkRequest:
        """构造带通用头的 QNetworkRequest。

        Args:
            url: 完整请求地址。

        Returns:
            QNetworkRequest: 已设置 URL 与超时的请求对象。
        """
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        return request

    def _handle_reply(
        self,
        reply: QNetworkReply,
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
        is_json: bool = True,
    ) -> None:
        """统一处理网络回复：完成后解析并分发回调。

        Args:
            reply: 网络回复对象。
            on_success: 成功回调（参数为解析后的 JSON dict 或原文）。
            on_error: 失败回调（参数为友好错误描述）。
            is_json: 是否按 JSON 解析响应体。
        """

        def on_finished() -> None:
            reply.deleteLater()
            if reply.error() != QNetworkReply.NetworkError.NoError:
                status_code = reply.attribute(
                    QNetworkRequest.HttpStatusCodeAttribute
                )
                detail = bytes(reply.readAll()).decode("utf-8", errors="replace")
                # 优先提取后端返回的 detail 字段（FastAPI 标准错误结构）
                message = reply.errorString()
                if detail:
                    try:
                        import json

                        parsed = json.loads(detail)
                        message = str(parsed.get("detail", message))
                    except json.JSONDecodeError:
                        pass
                log_error(f"HTTP 请求失败: {reply.url().toString()} -> {message}")
                on_error(f"[{status_code}] {message}" if status_code else message)
                return

            body = bytes(reply.readAll())
            if is_json:
                try:
                    import json

                    on_success(json.loads(body.decode("utf-8")) if body else {})
                except json.JSONDecodeError as e:
                    on_error(f"响应 JSON 解析失败: {e}")
            else:
                on_success(body.decode("utf-8", errors="replace"))

        reply.finished.connect(on_finished)

    def get(
        self,
        url: str,
        on_success: Callable[[Dict[str, Any]], None],
        on_error: Callable[[str], None],
    ) -> QNetworkReply:
        """发起异步 GET 请求。

        Args:
            url: 完整地址。
            on_success: 成功回调（JSON dict）。
            on_error: 失败回调（错误描述）。

        Returns:
            QNetworkReply: 回复对象（可用于 abort 中止）。
        """
        log_info(f"GET {url}")
        reply = self._manager.get(self._make_request(url))
        self._handle_reply(reply, on_success, on_error)
        return reply

    def post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        on_success: Callable[[Dict[str, Any]], None],
        on_error: Callable[[str], None],
    ) -> QNetworkReply:
        """发起异步 POST 请求（JSON 请求体）。

        Args:
            url: 完整地址。
            payload: JSON 序列化请求体。
            on_success: 成功回调（JSON dict）。
            on_error: 失败回调（错误描述）。

        Returns:
            QNetworkReply: 回复对象。
        """
        import json

        log_info(f"POST {url}")
        request = self._make_request(url)
        data = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        buffer = QBuffer()
        buffer.setData(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reply = self._manager.post(request, buffer)
        buffer.setParent(reply)  # 生命周期跟随 reply
        self._handle_reply(reply, on_success, on_error)
        return reply

    def post_multipart(
        self,
        url: str,
        file_field: str,
        file_path: str,
        on_success: Callable[[Dict[str, Any]], None],
        on_error: Callable[[str], None],
    ) -> Optional[QNetworkReply]:
        """发起异步 POST 文件上传（multipart/form-data）。

        Args:
            url: 完整地址。
            file_field: 表单字段名（后端为 file）。
            file_path: 本地待上传文件路径。
            on_success: 成功回调（JSON dict）。
            on_error: 失败回调（错误描述）。

        Returns:
            Optional[QNetworkReply]: 回复对象；文件不可读时返回 None。
        """
        from PySide6.QtCore import QFile

        file = QFile(file_path)
        if not file.open(QIODevice.OpenModeFlag.ReadOnly):
            on_error(f"无法读取本地文件: {file_path}")
            return None

        log_info(f"UPLOAD {url} <- {file_path}")
        multi_part = QHttpMultiPartStub.build(file_field, file)
        request = self._make_request(url)
        request.setHeader(
            QNetworkRequest.ContentTypeHeader,
            "multipart/form-data; boundary=" + multi_part.boundary().data().decode(),
        )
        reply = self._manager.post(request, multi_part)
        multi_part.setParent(reply)
        self._handle_reply(reply, on_success, on_error)
        return reply


class QHttpMultiPartStub:
    """QHttpMultiPart 辅助构造器（避免顶层导入分散注意力）。"""

    @staticmethod
    def build(file_field: str, file: Any) -> Any:
        """构造包含单个文件 Part 的 QHttpMultiPart。

        Args:
            file_field: 表单字段名。
            file: 已打开的 QFile。

        Returns:
            Any: 配置完成的 QHttpMultiPart 对象。
        """
        from pathlib import Path

        from PySide6.QtNetwork import QHttpPart

        from desktop.core.app_log import log_error

        multi_part = QHttpMultiPart(QHttpMultiPart.ContentType.FormDataType)
        part = QHttpPart()
        part.setHeader(
            QNetworkRequest.ContentTypeHeader, "text/plain"
        )
        part.setHeader(
            QNetworkRequest.ContentDispositionHeader,
            f'form-data; name="{file_field}"; filename="{Path(str(file.fileName())).name}"',
        )
        part.setBodyDevice(file)
        multi_part.append(part)
        return multi_part


# 模块级单例
http_client = HttpClient()
