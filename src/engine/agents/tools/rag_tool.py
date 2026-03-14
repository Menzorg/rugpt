"""
RAG Tool

LangChain tool for hybrid document search (vector + TSV rank fusion).
Uses PostgreSQL functions: search_related_docs -> search_rag.

org_id and user_id are injected via RunnableConfig — LLM sees only `query`.

Pool lifecycle: call init_rag_pool(pool) once during engine startup to set the
shared asyncpg pool. The pool is reused across all calls without reconnecting.
"""
import concurrent.futures
import logging
from typing import Annotated, Optional

import asyncpg
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from langchain_ollama.embeddings import OllamaEmbeddings

from ...config import Config

logger = logging.getLogger("rugpt.agents.tools.rag")

# Shared pool set once during engine startup via init_rag_pool()
_pool: Optional[asyncpg.Pool] = None


def init_rag_pool(pool: asyncpg.Pool) -> None:
    """Set the shared asyncpg pool for all RAG tool calls."""
    global _pool
    _pool = pool
    logger.info("RAG tool pool initialized")


def _get_embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(
        model=Config.EMBEDDING_MODEL,
        base_url=Config.LLM_BASE_URL,
    )


async def _search_rag_async(
    query: str,
    org_id: str,
    user_id: str,
    top_k_docs: int = 3,
    chunks_per_doc: int = 3,
) -> str:
    """
    Two-stage hybrid RAG search:
    1. search_related_docs — find relevant documents by org/user scope
    2. search_rag — retrieve chunks/table rows from each document
    """
    if _pool is None:
        logger.error("rag_search: pool not initialized, call init_rag_pool() at startup")
        return "RAG search unavailable: database pool not initialized."

    emb = _get_embeddings()
    query_embedding = emb.embed_query(query)
    emb_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"

    async with _pool.acquire() as conn:
        # Stage 1: find relevant documents
        docs = await conn.fetch(
            """
            SELECT doc_id, doc_title, summary, vec_dist, tsv_score, mode_used
            FROM search_related_docs($1::uuid, $2::uuid, $3, $4::vector, $5)
            """,
            org_id, user_id, query, emb_literal, top_k_docs,
        )

        if not docs:
            logger.info(f"rag_search: no documents found for query='{query}'")
            return "No relevant documents found."

        results = []
        for doc in docs:
            doc_id = doc["doc_id"]
            title = doc["doc_title"] or "Untitled"
            mode = doc["mode_used"]

            # Stage 2: search chunks within document (same method as doc search used)
            method = mode if mode in ("concrete", "abstract") else "concrete"
            chunks = await conn.fetch(
                """
                SELECT item_id, text_content, vec_dist, tsv_score, source_type
                FROM search_rag($1::uuid, $2, $3::vector, $4, $5)
                """,
                doc_id, query, emb_literal, chunks_per_doc, method,
            )

            # Fallback to other method if empty
            if not chunks:
                fallback = "abstract" if method == "concrete" else "concrete"
                chunks = await conn.fetch(
                    """
                    SELECT item_id, text_content, vec_dist, tsv_score, source_type
                    FROM search_rag($1::uuid, $2, $3::vector, $4, $5)
                    """,
                    doc_id, query, emb_literal, chunks_per_doc, fallback,
                )

            doc_block = f"## {title}\n"
            if chunks:
                for chunk in chunks:
                    src = chunk["source_type"]
                    text = chunk["text_content"]
                    doc_block += f"\n[{src}] {text}\n"
            else:
                doc_block += "\n(no matching content)\n"

            results.append(doc_block)

        output = "\n\n---\n\n".join(results)
        logger.info(f"rag_search: found {len(docs)} docs with chunks")
        return output


@tool(response_format="content")
async def rag_search(
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Search documents in the knowledge base.
    Args:
        query: Search query in Russian or English
    """
    configurable = config.get("configurable", {})
    org_id = configurable.get("org_id", "")
    user_id = configurable.get("user_id", "")

    logger.info(f"rag_search called: query={query}, org_id={org_id}, user_id={user_id}")

    if not org_id or not user_id:
        logger.error("rag_search: missing org_id or user_id in config")
        return "RAG search unavailable: missing context."

    return await _search_rag_async(query, org_id, user_id)
