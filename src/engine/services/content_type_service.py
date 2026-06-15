"""
Content Type Service

Business logic for the admin-managed per-org content-type catalog.
Admin-only enforcement lives in routes. See migration 051.
"""

from src.engine.unified_logger import get_logger
from typing import Optional, List
from uuid import UUID

from ..models.content_type import ContentType
from ..storage.content_type_storage import ContentTypeStorage

logger = get_logger("services")


class ContentTypeService:

    def __init__(self, content_type_storage: ContentTypeStorage):
        self._storage = content_type_storage

    async def create(self, org_id: UUID, name: str, description: str = "") -> ContentType:
        ct = ContentType(org_id=org_id, name=name.strip(), description=description or "")
        return await self._storage.create(ct)

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
        return await self._storage.update(
            content_type_id,
            name=name.strip() if name is not None else None,
            description=description,
            is_active=is_active,
        )

    async def delete(self, content_type_id: UUID) -> bool:
        return await self._storage.soft_delete(content_type_id)
