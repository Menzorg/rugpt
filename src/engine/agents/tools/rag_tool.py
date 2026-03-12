"""
RAG Tool

LangChain tool for hybrid document search (vector + TSV rank fusion).
Uses PostgreSQL functions: search_related_docs -> search_rag.

org_id and user_id are injected via RunnableConfig — LLM sees only `query`.
"""
import logging
import asyncio
from typing import Annotated

import asyncpg
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from langchain_ollama.embeddings import OllamaEmbeddings

from ...config import Config

logger = logging.getLogger("rugpt.agents.tools.rag")


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
    emb = _get_embeddings()
    query_embedding = emb.embed_query(query)
    emb_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"

    pool = await asyncpg.create_pool(Config.get_postgres_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
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

    finally:
        await pool.close()


@tool(response_format="content")
def rag_search(
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

    # LangChain tools are sync; run async search in a new thread to avoid
    # blocking the running event loop (FastAPI / LangGraph).
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            asyncio.run,
            _search_rag_async(query, org_id, user_id),
        )
        return future.result(timeout=120)
