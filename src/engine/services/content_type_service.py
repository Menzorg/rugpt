"""
Content Type Service

Business logic for the admin-managed per-org content-type catalog.
Admin-only enforcement lives in routes. See migration 051.
"""

from src.engine.unified_logger import get_logger
from typing import Any
from typing import Optional, List
from uuid import UUID

from langchain_openai import OpenAIEmbeddings

from ..agents.metadata import build_initial_extra_body, resolve_litellm_session_id
from ..models.content_type import ContentType
from ..storage.content_type_storage import ContentTypeStorage

logger = get_logger("services")


class ContentTypeService:

    def __init__(
        self,
        content_type_storage: ContentTypeStorage,
        embedding_model: str = "",
        llm_base_url: str = "",
        llm_api_key: str = "",
        vector_dim: int = 1024,
    ):
        self._storage = content_type_storage
        self._vector_dim = vector_dim
        self._embeddings = None
        if embedding_model:
            self._embeddings = OpenAIEmbeddings(
                model=embedding_model,
                base_url=llm_base_url,
                api_key=llm_api_key,
                timeout=180,
            )

    def _build_embedding_extra_body(self) -> dict[str, Any]:
        return build_initial_extra_body(
            litellm_session_id=resolve_litellm_session_id(),
            agent_name="content_type_embedding",
            chat_id=None,
        )

    def _format_embedding_text(self, name: str, description: str) -> str:
        return f"Name: {name}\nDescription: {description or ''}"

    def _validate_embedding(self, embedding: list[float]) -> None:
        if len(embedding) != self._vector_dim:
            raise ValueError(
                f"Embedding size {len(embedding)} does not match VECTOR_DIM={self._vector_dim}."
            )

    async def _embed_content_type(self, name: str, description: str) -> list[float]:
        if self._embeddings is None:
            raise RuntimeError(
                "content type embeddings are required but embedding_model is not configured"
            )
        # TODO(tech-debt): move embedding creation into a single queue-friendly
        # pipeline shared by RAG summaries, manual comments, content types, and
        # correction rules. Content type create/update should not stay coupled
        # to synchronous LiteLLM availability.
        # Until that exists, fail category create/update before writing the row:
        # categories without embeddings silently degrade RAG quality and are hard
        # to diagnose after the fact.
        embedding = await self._embeddings.aembed_query(
            self._format_embedding_text(name, description),
            extra_body=self._build_embedding_extra_body(),
        )
        self._validate_embedding(embedding)
        return embedding

    async def create(self, org_id: UUID, name: str, description: str = "") -> ContentType:
        clean_name = name.strip()
        clean_description = description or ""
        embedding = await self._embed_content_type(clean_name, clean_description)
        ct = ContentType(
            org_id=org_id,
            name=clean_name,
            description=clean_description,
        )
        return await self._storage.create(ct, embedding=embedding)

    async def get(self, content_type_id: UUID) -> Optional[ContentType]:
        return await self._storage.get_by_id(content_type_id)

    async def list(self, org_id: UUID, include_inactive: bool = False) -> List[ContentType]:
        return await self._storage.list_by_org(org_id, include_inactive)

    async def update(
        self,
        content_type_id: UUID,
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[ContentType]:
        clean_name = name.strip() if name is not None else None
        embedding = None
        if clean_name is not None or description is not None:
            current = await self._storage.get_by_id(content_type_id)
            if current is None:
                return None
            final_name = clean_name if clean_name is not None else current.name
            final_description = description if description is not None else current.description
            embedding = await self._embed_content_type(final_name, final_description)

        return await self._storage.update(
            content_type_id,
            name=clean_name,
            description=description,
            is_active=is_active,
            embedding=embedding,
        )

    async def search(
        self,
        org_id: UUID,
        query: str,
        threshold: float = 0.1,
    ) -> List[ContentType]:
        """Fuzzy search active content types by name using trigram similarity."""
        return await self._storage.search_by_name(org_id, query.strip(), threshold)

    async def delete(self, content_type_id: UUID) -> bool:
        return await self._storage.soft_delete(content_type_id)
