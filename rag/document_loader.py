# -*- coding: utf-8 -*-
"""
RAG 文档加载模块
负责加载用户上传的 txt 需求文档 / 测试知识文档，
并使用 LangChain 文本切分器切分为适合向量入库的文档片段。
"""

from pathlib import Path
from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config.settings import settings
from utils.logger import logger


def load_text_document(file_path: str) -> List[Document]:
    """
    加载单个 txt 文档并切分为文档片段

    :param file_path: txt 文档本地路径
    :return: 切分后的 Document 列表；失败返回空列表
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        logger.error(f"文档不存在: {file_path}")
        return []

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        if not content.strip():
            logger.warning(f"文档内容为空: {file_path}")
            return []

        # 递归字符切分器：优先按段落、再按句子切分，保证语义完整性
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        )
        documents = splitter.create_documents(
            texts=[content],
            metadatas=[{"source": path.name}],
        )
        logger.info(f"文档 {path.name} 加载并切分完成，共 {len(documents)} 个片段")
        return documents
    except Exception as e:
        logger.error(f"加载文档失败 {file_path}: {e}")
        return []


def load_text_from_string(text: str, source_name: str = "inline_text") -> List[Document]:
    """
    直接对传入的文本字符串进行切分（适用于 API 直接上传文本内容的场景）

    :param text: 原始文本内容
    :param source_name: 文本来源标识，写入 metadata
    :return: 切分后的 Document 列表；失败返回空列表
    """
    if not text or not text.strip():
        logger.warning("传入文本为空，无法切分")
        return []

    try:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        )
        documents = splitter.create_documents(
            texts=[text],
            metadatas=[{"source": source_name}],
        )
        logger.info(f"文本 [{source_name}] 切分完成，共 {len(documents)} 个片段")
        return documents
    except Exception as e:
        logger.error(f"文本切分失败: {e}")
        return []
