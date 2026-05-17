"""
Expand Chunk Tool

LangChain tool for fetching a chunk plus its immediate neighbors by chunk index.
Delegates all DB access to RAGService.

org_id and user_id are injected via RunnableConfig — LLM sees only file_id
and chunk_index.
"""

from src.engine.unified_logger import get_logger
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

class ExpandChunkInput(BaseModel):
    file_id: str = Field(description="Document ID of the source file")
    chunk_index: int = Field(description="Index of the center chunk inside the file")

def create_expand_chunk_tool(
    rag_service: RAGService,
    file_storage: UserFileStorage,
):
    """Create the expand_chunk tool wired to RAGService and UserFileStorage."""

    async def _can_access_file(file_id: str, org_id: str, user_id: str) -> bool:
        try:
            org_uuid = UUID(org_id)
            user_uuid = UUID(user_id)
            file_uuid = UUID(file_id)
        except ValueError:
            return False
        all_files = await file_storage.list_by_org(org_uuid)
        return any(
            f.id == file_uuid and (f.uploaded_by_user_id == user_uuid or f.is_public)
            for f in all_files
        )

    async def _expand_chunk_async(
        file_id: str,
        chunk_index: int,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """Fetch one chunk and its immediate neighbors by file_id and chunk_index.
        Args:
            file_id: Document ID of the source file.
            chunk_index: Index of the center chunk inside the file.
        """
        try:
            configurable = (config or {}).get("configurable", {})
            org_id = configurable.get("org_id", "")
            user_id = configurable.get("user_id", "")

            logger.info(
                "expand_chunk called: file_id=%s, chunk_index=%d, org_id=%s, user_id=%s",
                file_id, chunk_index, org_id, user_id,
            )

            if not org_id or not user_id:
                logger.error("expand_chunk: missing org_id or user_id in config")
                return "Expand chunk unavailable: missing context."

            if not await _can_access_file(file_id, org_id, user_id):
                return "You don't have access to that document."

            doc = await rag_service.get_doc_by_id(file_id)
            if doc is None:
                logger.info("expand_chunk: document not found for file_id='%s'", file_id)
                return "Document not found."

            chunks = await rag_service.get_expanded_context_by_index(
                file_id=file_id,
                chunk_index=chunk_index,
            )

            if not chunks:
                return f"No chunk found in '{doc.doc_title or file_id}' for chunk_index={chunk_index}."

            lines = [f"## {doc.doc_title or file_id}", f"Center chunk index: {chunk_index}"]
            for chunk in chunks:
                idx = chunk.chunk_index if chunk.chunk_index is not None else "?"
                lines.append(f"\n[chunk {idx}] {chunk.chunk_text}")

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"expand_chunk failed: {e}", exc_info=True)
            return _TOOL_ERROR_RESULT

    return StructuredTool.from_function(
        coroutine=_expand_chunk_async,
        name="expand_chunk",
        description="Fetch one chunk and its immediate neighbors by file_id and chunk_index.",
        args_schema=ExpandChunkInput,
    )
