# -*- coding: utf-8 -*-
"""
FastAPI 接口模块
对外提供两个核心接口：
1. POST /upload_doc —— 上传 txt 测试知识文档，切分后写入 Chroma 向量库
2. POST /run_test   —— 接收 App 测试需求文本，启动 LangGraph Agent 完整测试流程

入参非法时统一返回友好错误信息（400），不向外抛出原生堆栈；
未处理异常统一由全局异常处理器兜底（500）并记录 traceback 日志。

前置条件（重要）：
1. 需预先启动 Appium 服务（默认 http://127.0.0.1:4723）
2. 需预先启动安卓模拟器，或连接真机并开启 USB 调试
"""

import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from agent.graph import run_test_agent
from config.settings import settings
from rag.document_loader import load_text_document
from rag.vector_store import add_documents, rebuild_collection_from_dir
from utils.log_buffer import attach_to_logger, ring_log_handler
from utils.logger import logger
from utils.resource_path import resource_path

# 将运行日志挂载到环形缓冲，供桌面客户端通过 /api/logs 增量拉取（仅内存）
attach_to_logger(logger)

# 上传文档大小上限（字节）：2MB 已足够纯文本知识文档
MAX_UPLOAD_SIZE_BYTES: int = 2 * 1024 * 1024
# 测试需求文本长度上限
MAX_REQUIREMENT_LENGTH: int = 2000
# 最近一次测试任务的完整结果缓存（供前端 /api/last_result 查询测试用例与执行日志）
_last_result_state: Optional[Dict[str, Any]] = None

app = FastAPI(
    title="面向安卓 App 的智能测试 Agent",
    description="基于 LangGraph + RAG + Appium + ADB 的安卓 App 自动化测试原型系统",
    version="1.2.0",
)

# Jinja2 模板引擎：渲染 templates/ 目录下的 Web 操作页面
# 通过 resource_path 定位，兼容开发环境与 PyInstaller 打包环境
templates = Jinja2Templates(directory=str(resource_path("templates")))


# ==================== 全局异常处理（友好错误，不抛原生堆栈） ====================
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求参数校验失败处理器，返回友好的 400 错误信息。

    Args:
        request: 当前请求对象。
        exc: 参数校验异常实例。

    Returns:
        JSONResponse: 400 响应，body 含统一错误码与逐项错误描述。
    """
    error_items = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        error_items.append(f"{loc}: {err.get('msg', '参数不合法')}")
    logger.warning(f"请求参数校验失败: {error_items}")
    return JSONResponse(
        status_code=400,
        content={"code": 400, "message": "请求参数不合法，请检查后重试", "detail": error_items},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """未处理异常兜底处理器，返回友好的 500 错误信息并记录 traceback。

    Args:
        request: 当前请求对象。
        exc: 未捕获的异常实例。

    Returns:
        JSONResponse: 500 响应，body 仅含友好提示，不暴露内部堆栈。
    """
    logger.error(f"接口未处理异常 [{request.url.path}]: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "服务器内部错误，请稍后重试或查看服务日志"},
    )


# ==================== 请求 / 响应模型 ====================
class RunTestRequest(BaseModel):
    """启动测试任务请求体。

    密钥字段为可选：传入则仅用于本次任务内存调用（不落盘），
    未传入则回退使用 .env 中的配置密钥，两种方式兼容。
    """

    requirement: str = Field(
        ...,
        description="App 测试需求文本（5~2000 字符）",
        min_length=5,
        max_length=MAX_REQUIREMENT_LENGTH,
    )
    qwen_api_key: Optional[str] = Field(
        default=None, description="通义千问临时 API 密钥（仅本次任务内存使用，不持久化）"
    )
    deepseek_api_key: Optional[str] = Field(
        default=None, description="DeepSeek 临时 API 密钥（仅本次任务内存使用，不持久化）"
    )
    llm_provider: Optional[str] = Field(
        default=None, description="模型覆盖值：qwen / deepseek；为空时使用 .env 配置"
    )
    is_real_device: Optional[bool] = Field(
        default=None, description="设备模式覆盖值：true=真机 / false=模拟器；为空时使用 .env 配置"
    )
    real_udid: Optional[str] = Field(
        default=None, description="真机 UDID 覆盖值（真机模式下生效）"
    )


class UploadDocResponse(BaseModel):
    """文档上传响应体。"""

    message: str
    file_name: str
    chunk_count: int


class RunTestResponse(BaseModel):
    """测试任务响应体。"""

    message: str
    report_path: str
    defect_count: int
    defects: list
    error: str = ""


# ==================== 接口 1：上传文档入库 ====================
@app.post("/upload_doc", response_model=UploadDocResponse, summary="上传 txt 测试知识文档入库")
async def upload_doc(file: UploadFile = File(..., description="txt 格式测试知识文档")) -> UploadDocResponse:
    """接收 txt 文档上传，保存到本地后进行文本切分，写入 Chroma 向量库。

    Args:
        file: 上传的 txt 文档（大小不超过 2MB）。

    Returns:
        UploadDocResponse: 上传结果（文件名与入库片段数）。

    Raises:
        HTTPException: 文件格式/大小不合法（400）或入库失败（500）。
    """
    # 文件名与格式校验
    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="仅支持上传 .txt 格式文档")

    try:
        content = await file.read()
        # 文件内容与大小校验
        if not content.strip():
            raise HTTPException(status_code=400, detail="上传文档内容为空")
        if len(content) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(status_code=400, detail="文档过大，请上传不超过 2MB 的 txt 文档")

        # 保存上传文件到本地 uploads 目录
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        save_path = Path(settings.upload_dir) / f"{timestamp}_{file.filename}"
        save_path.write_bytes(content)
        logger.info(f"文档上传保存成功: {save_path}")

        # 加载、切分并写入向量库
        documents = load_text_document(str(save_path))
        chunk_count = add_documents(documents)
        if chunk_count == 0:
            raise HTTPException(status_code=500, detail="文档切分或向量入库失败")

        return UploadDocResponse(
            message="文档上传并成功入库",
            file_name=file.filename,
            chunk_count=chunk_count,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文档上传接口异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档上传处理失败: {e}")


# ==================== 接口 2：启动测试任务 ====================
@app.post("/run_test", response_model=RunTestResponse, summary="启动 Agent 测试流程")
async def run_test(request: RunTestRequest) -> RunTestResponse:
    """接收 App 测试需求文本，启动 LangGraph Agent 完整测试链路。

    链路：RAG 检索 → 生成用例 → 动作规划 → Appium 执行（失败自动重规划 1 次）
    → adb 日志缺陷分析 → 输出 Markdown 报告。

    Args:
        request: 测试任务请求体（requirement 5~2000 字符）。

    Returns:
        RunTestResponse: 测试结果摘要（报告路径、缺陷列表与数量、错误信息）。

    Raises:
        HTTPException: 测试任务执行失败（500）。
    """
    logger.info(f"收到测试任务请求: {request.requirement[:80]}...")
    global _last_result_state
    try:
        # 原型系统采用同步执行，直接返回最终测试结果
        final_state = run_test_agent(
            request.requirement,
            qwen_api_key=request.qwen_api_key,
            deepseek_api_key=request.deepseek_api_key,
            llm_provider=request.llm_provider,
            is_real_device=request.is_real_device,
            real_udid=request.real_udid,
        )
        # 缓存最近一次完整结果，供 GET /api/last_result 查询（不影响本接口返回结构）
        # 安全约束：剥离临时密钥字段，密钥仅在图执行周期内存活，不做任何缓存
        _last_result_state = {
            k: v for k, v in final_state.items() if k not in ("user_qwen_key", "user_deepseek_key")
        }
        defects = final_state.get("defects", [])
        error = final_state.get("error", "")

        return RunTestResponse(
            message="测试流程执行完成" if not error else "测试流程执行完成（存在异常，请查看 error 字段）",
            report_path=final_state.get("report_path", ""),
            defect_count=len(defects),
            defects=defects,
            error=error,
        )
    except Exception as e:
        logger.error(f"测试任务接口异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"测试任务执行失败: {e}")


@app.get("/health", summary="服务健康检查")
async def health_check() -> dict:
    """服务健康检查接口。

    Returns:
        dict: 服务状态与服务标识。
    """
    return {"status": "ok", "service": "android-app-test-agent"}


# ==================== Web 前端页面与配置查询接口 ====================
@app.get("/", response_class=HTMLResponse, summary="Web 操作首页")
async def index(request: Request) -> Any:
    """渲染 Web 操作首页（templates/index.html）。

    页面支持：模型/设备配置展示、知识库文档上传、测试需求输入、
    一键启动自动化测试、执行结果与缺陷报告展示。

    Args:
        request: 当前请求对象（Jinja2 模板渲染必需）。

    Returns:
        Any: 渲染后的 HTML 页面响应。
    """
    return templates.TemplateResponse(
        request=request, name="index.html", context={}
    )


@app.get("/api/config", summary="查询当前运行配置（供前端展示）")
async def get_config() -> dict:
    """查询当前生效的运行配置，供前端页面初始化展示。

    Returns:
        dict: 含 LLM Provider、模型名、设备模式、设备 UDID、被测应用包名。
    """
    return {
        "llm_provider": settings.validate_llm_provider(),
        "llm_model": settings.active_llm_model_name,
        "is_real_device": settings.IS_REAL_DEVICE,
        "device_udid": settings.device_name,
        "app_package": settings.app_package,
    }


@app.get("/api/last_result", summary="查询最近一次测试任务完整结果")
async def get_last_result() -> dict:
    """查询最近一次测试任务的完整结果（含测试用例与 adb 日志摘录）。

    用于前端在不改变 /run_test 返回结构的前提下，补充展示
    生成的测试用例与执行日志明细。

    Returns:
        dict: 最近一次任务的测试用例文本与 adb 日志摘录；
        尚无任务记录时返回提示字段。
    """
    if not _last_result_state:
        return {"test_cases": "", "adb_log_excerpt": ""}
    log_excerpt = (_last_result_state.get("adb_log_content") or "")[-3000:]
    return {
        "test_cases": _last_result_state.get("test_cases", ""),
        "adb_log_excerpt": log_excerpt,
    }


# ==================== 桌面客户端支撑接口（增量新增） ====================
@app.get("/api/logs", summary="增量拉取后端运行日志（供桌面客户端实时展示）")
async def get_logs(after_seq: int = 0) -> dict:
    """增量拉取环形缓冲中的运行日志。

    Args:
        after_seq: 客户端已持有的最大日志序号，仅返回更大序号的日志。

    Returns:
        dict: {"logs": [ {seq,ts,level,message}, ... ], "last_seq": 当前最大序号}。
    """
    records = ring_log_handler.get_after(after_seq)
    return {
        "logs": [r.to_dict() for r in records],
        "last_seq": ring_log_handler.last_seq(),
    }


@app.post("/rebuild_kb", summary="重建 RAG 向量库（重新入库 uploads 目录全部文档）")
async def rebuild_kb() -> dict:
    """清空当前 Chroma collection，重新入库 uploads 目录下的全部 txt 文档。

    Returns:
        dict: 重建结果（入库片段数）；失败时抛 500。

    Raises:
        HTTPException: 向量库重建失败（500）。
    """
    logger.info("收到向量库重建请求")
    try:
        chunk_count = rebuild_collection_from_dir(settings.upload_dir)
        if chunk_count == 0:
            return {"message": "重建完成：没有可入库的 txt 文档", "chunk_count": 0}
        return {"message": f"重建完成，共入库 {chunk_count} 个片段", "chunk_count": chunk_count}
    except Exception as e:
        logger.error(f"向量库重建接口异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"向量库重建失败: {e}")
