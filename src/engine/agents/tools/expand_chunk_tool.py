"""
Expand Chunk Tool

LangChain tool for fetching a chunk plus its immediate neighbors by chunk index.
Delegates all DB access to RAGService.

org_id and caller_user_id are injected via RunnableConfig — LLM sees only file_id
and chunk_index.
"""

from src.engine.unified_logger import get_logger
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ...services.rag_service import RAGService
from ...storage.chat_storage import ChatStorage
from ...storage.user_file_storage import UserFileStorage
from .util.file_access import can_access_file

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

class ExpandChunkInput(BaseModel):
    file_id: str = Field(description="Document ID of the source file")
    chunk_index: int = Field(description="Index of the center chunk inside the file")

def create_expand_chunk_tool(
    rag_service: RAGService,
    file_storage: UserFileStorage,
    chat_storage: Optional[ChatStorage] = None,
):
    """Create the expand_chunk tool wired to RAGService and UserFileStorage."""

    async def _expand_chunk_async(
        file_id: str,
        chunk_index: int,
        config: RunnableConfig = None,
    ) -> str:
        """Fetch one chunk and its immediate neighbors by file_id and chunk_index.
        Args:
            file_id: Document ID of the source file.
            chunk_index: Index of the center chunk inside the file.
        """
        try:
            configurable = (config or {}).get("configurable", {})
            caller_user_id = configurable["caller_user_id"]
            callee_user_id = configurable.get("callee_user_id", "")
            org_id = configurable["org_id"]
            is_admin = bool(configurable.get("is_admin", False))
            # In mention calls callee differs from caller — restrict to callee's public docs only.
            owner_user_id = callee_user_id or caller_user_id
            mention_mode = bool(callee_user_id and callee_user_id != caller_user_id)
            chat_id = configurable.get("chat_id")

            logger.info(
                "expand_chunk called: file_id=%s, chunk_index=%d, org_id=%s, user_id=%s, mention_mode=%s, chat_id=%s",
                file_id, chunk_index, org_id, owner_user_id, mention_mode, chat_id,
            )

            if not await can_access_file(
                file_id, org_id, owner_user_id, file_storage,
                mention_mode=mention_mode, is_admin=is_admin,
                chat_storage=chat_storage, chat_id=chat_id,
            ):
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
