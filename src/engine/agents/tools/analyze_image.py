"""
Analyze Image Tool

LangChain tool for asking the LLM about an uploaded image attachment.
"""
import base64
from io import BytesIO
import logging
from typing import Optional
from uuid import UUID

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from ...config import Config
from ...constants import IMAGE_TYPES
from ...storage.storage_adapter import StorageAdapter
from ...storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.agents.tools.analyze_image")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"
_MAX_IMAGE_SIDE = 2048
_JPEG_QUALITY = 85


class AnalyzeImageInput(BaseModel):
    query: str = Field(description="Question or instruction for analyzing the image")
    file_id: str = Field(description="UUID of the uploaded image file")


def create_analyze_image_tool(
    file_storage: UserFileStorage,
    storage_adapter: StorageAdapter,
):
    """Create analyze_image tool wired to file metadata and binary storage."""

    async def _read_normalized_image_jpeg(file_id: str) -> bytes | str:
        """Fetch uploaded image and normalize it to bounded JPEG bytes."""
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
        with Image.open(BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((_MAX_IMAGE_SIDE, _MAX_IMAGE_SIDE))
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGBA")
                background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                image = Image.alpha_composite(background, image).convert("RGB")
            else:
                image = image.convert("RGB")

            out = BytesIO()
            image.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            return out.getvalue()

    async def _analyze_image_async(query: str, file_id: str) -> str:
        """Analyze an uploaded image with an LLM.

        Args:
            query: Question or instruction for the image.
            file_id: UUID of an uploaded image file.
        """
        logger.info("tool analyze_image: file_id=%s query=%r", file_id, query)
        try:
            jpeg_or_error = await _read_normalized_image_jpeg(file_id)
            if isinstance(jpeg_or_error, str):
                return jpeg_or_error
            b64 = base64.b64encode(jpeg_or_error).decode("ascii")

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
                            "url": f"data:image/jpeg;base64,{b64}",
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
