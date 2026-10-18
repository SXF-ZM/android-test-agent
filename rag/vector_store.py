# -*- coding: utf-8 -*-
"""
Chroma 向量数据库模块
基于 sentence-transformers 本地 Embedding 模型（all-MiniLM-L6-v2）与 Chroma，
提供文档片段入库与相似度检索能力，支撑 Agent 的 RAG 检索节点。
"""

from typing import List

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

from config.settings import settings
from utils.logger import logger

# 模块级单例，避免重复加载 Embedding 模型与重复连接 Chroma
_embedding_model: HuggingFaceEmbeddings | None = None
_vector_store: Chroma | None = None


def get_embedding_model() -> HuggingFaceEmbeddings:
    """
    获取 Embedding 模型单例（all-MiniLM-L6-v2 本地模型）

    :return: HuggingFaceEmbeddings 实例
    """
    global _embedding_model
    if _embedding_model is None:
        logger.info(f"正在加载本地 Embedding 模型: {settings.embedding_model_name}")
        _embedding_model = HuggingFaceEmbeddings(
            model_name=settings.embedding_model_name,
            cache_folder=settings.embedding_cache_dir,
        )
        logger.info("Embedding 模型加载完成")
    return _embedding_model


def get_vector_store() -> Chroma:
    """
    获取 Chroma 向量库单例（本地持久化存储）

    :return: Chroma 向量库实例
    """
    global _vector_store
    if _vector_store is None:
        logger.info(f"正在连接 Chroma 向量库: {settings.chroma_persist_dir}")
        _vector_store = Chroma(
            collection_name=settings.chroma_collection_name,
            embedding_function=get_embedding_model(),
            persist_directory=settings.chroma_persist_dir,
        )
        logger.info("Chroma 向量库连接完成")
    return _vector_store


def add_documents(documents: List[Document]) -> int:
    """
    将文档片段写入 Chroma 向量库

    :param documents: 待入库的 Document 列表
    :return: 成功入库的文档片段数量
    """
    if not documents:
        logger.warning("待入库文档列表为空，跳过入库")
        return 0
    try:
        store = get_vector_store()
        store.add_documents(documents)
        logger.info(f"成功入库 {len(documents)} 个文档片段")
        return len(documents)
    except Exception as e:
        logger.error(f"文档入库失败: {e}", exc_info=True)
        return 0


def rebuild_collection_from_dir(source_dir: str) -> int:
    """
    重建向量库：清空当前 collection，重新入库指定目录下全部 txt 文档

    供桌面客户端"向量库重建"功能调用（增量新增能力，不影响原有检索/入库逻辑）。

    :param source_dir: 待重新入库的 txt 文档目录
    :return: 重建后入库的文档片段数量
    """
    from pathlib import Path

    from rag.document_loader import load_text_document

    dir_path = Path(source_dir)
    txt_files = sorted(dir_path.glob("*.txt")) if dir_path.exists() else []
    if not txt_files:
        logger.warning(f"重建向量库：目录 {source_dir} 下没有可入库的 txt 文档")
        return 0

    logger.info(f"开始重建向量库：清空 collection 后重新入库 {len(txt_files)} 个文档")
    global _vector_store
    try:
        store = get_vector_store()
        # 删除远端 collection 并复位本地单例，下次访问按配置自动重建
        store.delete_collection()
        _vector_store = None
        store = get_vector_store()

        total_chunks = 0
        for txt_file in txt_files:
            docs = load_text_document(str(txt_file))
            total_chunks += add_documents(docs)
            logger.info(f"重建入库完成: {txt_file.name} -> {len(docs)} 个片段")
        logger.info(f"向量库重建完成，共入库 {total_chunks} 个片段")
        return total_chunks
    except Exception as e:
        logger.error(f"向量库重建失败: {e}", exc_info=True)
        _vector_store = None
        return 0


def similarity_search(query: str, top_k: int | None = None) -> List[str]:
    """
    基于相似度检索与 query 最相关的文档片段（默认返回 top3）

    :param query: 检索查询文本（如用户测试需求）
    :param top_k: 返回的文档片段数量，默认取配置中的 rag_top_k
    :return: 相关文档片段的文本内容列表
    """
    k = top_k or settings.rag_top_k
    try:
        store = get_vector_store()
        results = store.similarity_search(query, k=k)
        contents = [doc.page_content for doc in results]
        logger.info(f"RAG 检索完成，query: [{query[:50]}...]，命中 {len(contents)} 个片段")
        return contents
    except Exception as e:
        logger.error(f"RAG 相似度检索失败: {e}")
        return []
