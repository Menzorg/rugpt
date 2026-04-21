"""
RAG Tool

LangChain tool for hybrid document search (vector + TSV rank fusion).
Uses PostgreSQL functions: search_related_docs -> search_rag.

org_id and user_id are injected via RunnableConfig — LLM sees only `query`.

Pool lifecycle: call init_rag_pool(pool) once during engine startup to set the
shared asyncpg pool. The pool is reused across all calls without reconnecting.
"""
import logging
from typing import Annotated, Optional

import asyncpg
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from langchain_openai import OpenAIEmbeddings

from ...config import Config

logger = logging.getLogger("rugpt.agents.tools.rag")

# Shared pool set once during engine startup via init_rag_pool()
_pool: Optional[asyncpg.Pool] = None


def init_rag_pool(pool: asyncpg.Pool) -> None:
    """Set the shared asyncpg pool for all RAG tool calls."""
    global _pool
    _pool = pool
    logger.info("RAG tool pool initialized")


def _get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=Config.EMBEDDING_MODEL,
        base_url=Config.LLM_BASE_URL,
        api_key=Config.LLM_API_KEY,
    )


async def _search_rag_async(
    query: str,
    org_id: str,
    user_id: str,
    top_k_docs: int = 3,
    chunks_per_doc: int = 3,
    file_id: Optional[str] = None,
    doc_query: Optional[str] = None,
) -> str:
    """
    Two-stage hybrid RAG search:
    1. search_related_docs — find relevant documents by org/user scope
    2. search_rag — retrieve chunks/table rows from each document

    If file_id is provided, skip document search and use that document directly.
    If doc_query is provided (and file_id is not), use it for document search stage.
    """
    if _pool is None:
        logger.error("rag_search: pool not initialized, call init_rag_pool() at startup")
        return "RAG search unavailable: database pool not initialized."

    emb = _get_embeddings()
    # aembed_query — async version; synchronous embed_query would block the
    # event loop for the entire embeddings HTTP round-trip to LiteLLM.
    query_embedding = await emb.aembed_query(query)
    emb_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"

    async with _pool.acquire() as conn:
        if file_id is not None:
            # Use the provided file_id directly — skip document search stage
            doc_row = await conn.fetchrow(
                "SELECT doc_id, title AS doc_title FROM documents WHERE doc_id = $1::uuid",
                file_id,
            )
            if doc_row is None:
                logger.info(f"rag_search: document not found for file_id='{file_id}'")
                return "No relevant documents found."
            docs = [doc_row]
        else:
            # Stage 1: find relevant documents
            search_query = doc_query if doc_query else query
            docs = await conn.fetch(
                """
                SELECT doc_id, doc_title, summary, vec_dist, tsv_score, mode_used
                FROM search_related_docs($1::uuid, $2::uuid, $3, $4::vector, $5)
                """,
                org_id, user_id, search_query, emb_literal, top_k_docs,
            )

        if not docs:
            logger.info(f"rag_search: no documents found for query='{query}'")
            return "No relevant documents found."

        results = []
        for doc in docs:
            doc_id = doc["doc_id"]
            title = doc["doc_title"] or "Untitled"

            # Stage 2: search chunks within document (same method as doc search used)
            method = "concrete"
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
    file_id: Optional[str] = None,
    doc_query: Optional[str] = None,
) -> str:
    """Search documents in the knowledge base.
    Args:
        query: Search query in Russian or English
        file_id: Optional document ID. If provided, searches only within that specific document, skipping the document discovery stage.
        doc_query: Optional query used to find relevant documents before searching their contents. If provided (and file_id is not), this query is used for the document search stage instead of the main query.
    """
    configurable = config.get("configurable", {})
    org_id = configurable.get("org_id", "")
    user_id = configurable.get("user_id", "")

    logger.info(f"rag_search called: query={query}, org_id={org_id}, user_id={user_id}, file_id={file_id}, doc_query={doc_query}")

    if not org_id or not user_id:
        logger.error("rag_search: missing org_id or user_id in config")
        return "RAG search unavailable: missing context."

    return await _search_rag_async(query, org_id, user_id, file_id=file_id, doc_query=doc_query)
