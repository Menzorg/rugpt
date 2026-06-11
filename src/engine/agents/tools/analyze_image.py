"""
Analyze Image Tool

LangChain tool for asking the LLM about an uploaded image attachment.
"""

from src.engine.unified_logger import get_logger
from typing import Optional
from uuid import UUID

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from ..runtime import RuntimeContext
from ...config import Config
from ...constants import IMAGE_TYPES
from ...storage.storage_adapter import StorageAdapter
from ...storage.user_file_storage import UserFileStorage
from ...utils.image_parser import image_bytes_to_data_url
from ...utils.token_counter import count_tokens

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

class AnalyzeImageInput(BaseModel):
    query: str = Field(description="Question or instruction for analyzing the image")
    file_id: str = Field(description="UUID of the uploaded image file")
    accent_proposal: Optional[str] = Field(
        default=None,
        description=(
            "Optional. Specify what exactly to extract or focus on in the image "
            "(e.g. 'Every single row in table verbatim', 'count of people on image'). "
            "Use this to narrow the analysis to the details you care about."
        ),
    )

def create_analyze_image_tool(
    file_storage: UserFileStorage,
    storage_adapter: StorageAdapter,
):
    """Create analyze_image tool wired to file metadata and binary storage."""

    async def _read_image_payload(file_id: str) -> dict | str:
        """Fetch uploaded image and format it for a multimodal message."""
        file_uuid = UUID(file_id)
        file = await file_storage.get_by_id(file_uuid)
        if file is None:
            return "Image file not found."
        if file.file_size <= 0:
            return "Image file is empty."

        file_type = (file.file_type or "").lower()
        if file_type not in IMAGE_TYPES:
            allowed = ", ".join(sorted(IMAGE_TYPES))
            return f"File {file_id} is not an image. Supported image types: {allowed}."

        data = await storage_adapter.read(file.storage_key)
        if not data:
            return "Image file is empty."
        payload_type = "video_url" if file_type == "gif" else "image_url"
        return {
            "type": payload_type,
            payload_type: {
                "url": image_bytes_to_data_url(data, file_type=file_type),
            },
        }

    async def _analyze_image_async(
        query: str,
        file_id: str,
        accent_proposal: Optional[str] = None,
        config: RunnableConfig = None,
        runtime: ToolRuntime[RuntimeContext] = None,
    ) -> str:
        """Analyze an uploaded image with an LLM.

        Args:
            query: Question or instruction for the image.
            file_id: UUID of an uploaded image file.
            accent_proposal: Optional focus on what specifically to extract.
        """
        logger.info(
            "tool analyze_image: file_id=%s query=%r accent_proposal=%r",
            file_id,
            query,
            accent_proposal,
        )
        try:
            configurable = (config or {}).get("configurable", {})
            logger.info(
                "analyze_image identity: caller_user_id=%s callee_user_id=%s org_id=%s is_admin=%s invocation=%s",
                configurable.get("caller_user_id", ""),
                configurable.get("callee_user_id", ""),
                configurable.get("org_id", ""),
                bool(configurable.get("is_admin", False)),
                configurable.get("invocation_kind", ""),
            )
            media_payload = await _read_image_payload(file_id)
            if isinstance(media_payload, str):
                return media_payload

            prompt = query
            if accent_proposal:
                prompt = f"{query}\n\nFocus specifically on: {accent_proposal}"

            llm = ChatOpenAI(
                base_url=Config.LLM_BASE_URL,
                api_key=Config.LLM_API_KEY,
                model=Config.IMAGE_ANALYSIS_MODEL,
                temperature=0.2,
                timeout=120,
            )
            llm_result = await llm.ainvoke([
                HumanMessage(content=[
                    {"type": "text", "text": prompt},
                    media_payload,
                ])
            ])
            result = str(llm_result.content).strip()
            if runtime is not None:
                async with runtime.context.lock:
                    runtime.context.total_tokens_spent += count_tokens(result)
            return result
        except Exception as e:
            logger.error("analyze_image failed: %s", e, exc_info=True)
            return _TOOL_ERROR_RESULT

    return StructuredTool.from_function(
        coroutine=_analyze_image_async,
        name="analyze_image",
        description="Analyze an uploaded image by file_id using a vision-capable LLM.",
        args_schema=AnalyzeImageInput,
    )
