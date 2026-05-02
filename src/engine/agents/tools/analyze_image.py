"""
Analyze Image Tool

LangChain tool for asking the LLM about an uploaded image attachment.
"""
import logging
from typing import Optional
from uuid import UUID

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ...config import Config
from ...constants import IMAGE_TYPES
from ...storage.storage_adapter import StorageAdapter
from ...storage.user_file_storage import UserFileStorage
from ...utils.imageparser import image_bytes_to_data_url

logger = logging.getLogger("rugpt.agents.tools.analyze_image")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"


class AnalyzeImageInput(BaseModel):
    query: str = Field(description="Question or instruction for analyzing the image")
    file_id: str = Field(description="UUID of the uploaded image file")


def create_analyze_image_tool(
    file_storage: UserFileStorage,
    storage_adapter: StorageAdapter,
):
    """Create analyze_image tool wired to file metadata and binary storage."""

    async def _read_image_data_url(file_id: str) -> str:
        """Fetch uploaded image and format it as a JPEG data URL."""
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
        return image_bytes_to_data_url(data)

    async def _analyze_image_async(query: str, file_id: str) -> str:
        """Analyze an uploaded image with an LLM.

        Args:
            query: Question or instruction for the image.
            file_id: UUID of an uploaded image file.
        """
        logger.info("tool analyze_image: file_id=%s query=%r", file_id, query)
        try:
            image_url = await _read_image_data_url(file_id)
            if not image_url.startswith("data:image/jpeg;base64,"):
                return image_url

            llm = ChatOpenAI(
                base_url=Config.LLM_BASE_URL,
                api_key=Config.LLM_API_KEY,
                model=Config.IMAGE_ANALYSIS_MODEL,
                temperature=0.2,
                timeout=120,
            )
            result = await llm.ainvoke([
                HumanMessage(content=[
                    {"type": "text", "text": query},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                        },
                    },
                ])
            ])
            return str(result.content).strip()
        except Exception as e:
            logger.error("analyze_image failed: %s", e, exc_info=True)
            return _TOOL_ERROR_RESULT

    return StructuredTool.from_function(
        coroutine=_analyze_image_async,
        name="analyze_image",
        description="Analyze an uploaded image by file_id using a vision-capable LLM.",
        args_schema=AnalyzeImageInput,
    )
