# -*- coding: utf-8 -*-
"""单次测试任务的产物目录管理。

每次测试任务生成唯一 ``run_id``，并把日志、截图、报告统一写入
``outputs/runs/<run_id>/{logs,screenshots,reports}``。这样多次任务的
产物不会混在同一个目录，报告也能通过 run_id 与某一轮执行对应起来。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config.settings import settings

# run_id 只允许安全字符，避免外部传入值影响目录结构
_SAFE_RUN_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class RunPaths:
    """一次测试任务的全部产物路径。"""

    run_id: str
    root: Path
    logs: Path
    screenshots: Path
    reports: Path


def create_run_id() -> str:
    """生成按时间排序且带随机后缀的任务 ID。

    Returns:
        str: 形如 ``20261031_142530_1a2b3c4d`` 的任务 ID。
    """
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _safe_run_id(value: Optional[str]) -> str:
    """清洗 run_id，非法字符替换为下划线并限制长度。"""
    text = str(value or "").strip()
    if not text:
        return create_run_id()
    return _SAFE_RUN_ID_RE.sub("_", text)[:100] or create_run_id()


def create_run_paths(run_id: Optional[str] = None) -> RunPaths:
    """创建（或复用）一次任务的产物目录。

    Args:
        run_id: 已有任务 ID；为空时自动生成。

    Returns:
        RunPaths: 已确保存在的日志/截图/报告目录。
    """
    effective_id = _safe_run_id(run_id)
    root = Path(settings.run_output_dir) / effective_id
    paths = RunPaths(
        run_id=effective_id,
        root=root,
        logs=root / "logs",
        screenshots=root / "screenshots",
        reports=root / "reports",
    )
    for directory in (paths.logs, paths.screenshots, paths.reports):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def ensure_run_paths(run_id: Optional[str] = None) -> RunPaths:
    """确保指定任务的产物目录存在，等价于 :func:`create_run_paths`。"""
    return create_run_paths(run_id)
