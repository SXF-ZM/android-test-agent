# -*- coding: utf-8 -*-
"""
后端 API 服务层（桌面客户端）
将后端 FastAPI 各接口封装为高层方法，UI 层只关心业务回调。
"""

from typing import Any, Callable, Dict

from PySide6.QtNetwork import QNetworkReply

from desktop.core.config import runtime_config
from desktop.net.http_client import http_client

# 成功回调与错误回调类型别名
SuccessCallback = Callable[[Dict[str, Any]], None]
ErrorCallback = Callable[[str], None]


class ApiService:
    """后端接口封装：健康检查、配置、日志、RAG、测试任务。"""

    @staticmethod
    def _url(path: str) -> str:
        """拼接后端完整地址。

        Args:
            path: 接口路径（以 / 开头）。

        Returns:
            str: 完整 URL。
        """
        return runtime_config.api_base_url.rstrip("/") + path

    # ==================== 基础接口 ====================
    def health(self, on_success: SuccessCallback, on_error: ErrorCallback) -> QNetworkReply:
        """服务健康检查 GET /health。

        Args:
            on_success: 成功回调。
            on_error: 失败回调。

        Returns:
            QNetworkReply: 回复对象。
        """
        return http_client.get(self._url("/health"), on_success, on_error)

    def get_config(self, on_success: SuccessCallback, on_error: ErrorCallback) -> QNetworkReply:
        """查询后端当前配置 GET /api/config。

        Args:
            on_success: 成功回调。
            on_error: 失败回调。

        Returns:
            QNetworkReply: 回复对象。
        """
        return http_client.get(self._url("/api/config"), on_success, on_error)

    def get_logs(self, after_seq: int, on_success: SuccessCallback, on_error: ErrorCallback) -> QNetworkReply:
        """增量拉取后端日志 GET /api/logs。

        Args:
            after_seq: 已持有最大日志序号。
            on_success: 成功回调。
            on_error: 失败回调。

        Returns:
            QNetworkReply: 回复对象。
        """
        return http_client.get(
            self._url(f"/api/logs?after_seq={after_seq}"), on_success, on_error
        )

    # ==================== RAG 知识库 ====================
    def upload_doc(self, file_path: str, on_success: SuccessCallback, on_error: ErrorCallback) -> None:
        """上传 txt 知识文档入库 POST /upload_doc。

        Args:
            file_path: 本地 txt 文件路径。
            on_success: 成功回调。
            on_error: 失败回调。
        """
        http_client.post_multipart(
            self._url("/upload_doc"), "file", file_path, on_success, on_error
        )

    def rebuild_kb(self, on_success: SuccessCallback, on_error: ErrorCallback) -> QNetworkReply:
        """重建 RAG 向量库 POST /rebuild_kb。

        Args:
            on_success: 成功回调。
            on_error: 失败回调。

        Returns:
            QNetworkReply: 回复对象。
        """
        return http_client.post_json(self._url("/rebuild_kb"), {}, on_success, on_error)

    # ==================== 测试任务 ====================
    def run_test(
        self,
        requirement: str,
        on_success: SuccessCallback,
        on_error: ErrorCallback,
    ) -> QNetworkReply:
        """启动 Agent 测试任务 POST /run_test（密钥仅随本次请求内存传递）。

        Args:
            requirement: 测试需求文本。
            on_success: 成功回调（含 report_path/defects/error）。
            on_error: 失败回调。

        Returns:
            QNetworkReply: 回复对象（可 abort 终止任务）。
        """
        payload: Dict[str, Any] = {
            "requirement": requirement,
            "llm_provider": runtime_config.llm_provider,
            "is_real_device": runtime_config.is_real_device,
        }
        # 密钥仅在内存中随请求传递，后端不持久化
        if runtime_config.qwen_key:
            payload["qwen_api_key"] = runtime_config.qwen_key
        if runtime_config.deepseek_key:
            payload["deepseek_api_key"] = runtime_config.deepseek_key
        if runtime_config.is_real_device and runtime_config.device_udid:
            payload["real_udid"] = runtime_config.device_udid
        return http_client.post_json(self._url("/run_test"), payload, on_success, on_error)


# 模块级单例
api_service = ApiService()
