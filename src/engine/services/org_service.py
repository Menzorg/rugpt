"""
Organization Service

Business logic for organization management.
"""
import logging
import re
from typing import Optional, List
from uuid import UUID

from ..models.organization import Organization
from ..storage.org_storage import OrgStorage

logger = logging.getLogger("rugpt.services.org")


class OrgService:
    """Service for organization management"""

    def __init__(self, storage: OrgStorage):
        self.storage = storage

    async def create_organization(
        self,
        name: str,
        slug: Optional[str] = None,
        description: Optional[str] = None
    ) -> Organization:
        """
        Create a new organization.

        Args:
            name: Organization display name
            slug: URL-safe identifier (auto-generated if not provided)
            description: Optional description

        Returns:
            Created organization

        Raises:
            ValueError: If slug already exists
        """
        # Generate slug if not provided
        if not slug:
            slug = self._generate_slug(name)

        # Validate slug format
        if not self._is_valid_slug(slug):
            raise ValueError(f"Invalid slug format: {slug}")

        # Check if slug exists
        if await self.storage.exists_by_slug(slug):
            raise ValueError(f"Organization with slug '{slug}' already exists")

        org = Organization(
            name=name,
            slug=slug,
            description=description
        )

        created = await self.storage.create(org)
        logger.info(f"Created organization: {created.name} ({created.slug})")
        return created

    async def get_organization(self, org_id: UUID) -> Optional[Organization]:
        """Get organization by ID"""
        return await self.storage.get_by_id(org_id)

    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug"""
        return await self.storage.get_by_slug(slug)

    async def list_organizations(self, active_only: bool = True) -> List[Organization]:
        """List all organizations"""
        return await self.storage.list_all(active_only)

    async def update_organization(
        self,
        org_id: UUID,
        name: Optional[str] = None,
        slug: Optional[str] = None,
        description: Optional[str] = None
    ) -> Optional[Organization]:
        """
        Update organization.

        Args:
            org_id: Organization ID
            name: New name (optional)
            slug: New slug (optional)
            description: New description (optional)

        Returns:
            Updated organization or None if not found
        """
        org = await self.storage.get_by_id(org_id)
        if not org:
            return None

        if name:
            org.name = name

        if slug:
            if not self._is_valid_slug(slug):
                raise ValueError(f"Invalid slug format: {slug}")
            if await self.storage.exists_by_slug(slug, exclude_id=org_id):
                raise ValueError(f"Organization with slug '{slug}' already exists")
            org.slug = slug

        if description is not None:
            org.description = description

        updated = await self.storage.update(org)
        logger.info(f"Updated organization: {updated.name}")
        return updated

    async def deactivate_organization(self, org_id: UUID) -> bool:
        """Deactivate (soft delete) organization"""
        result = await self.storage.delete(org_id)
        if result:
            logger.info(f"Deactivated organization: {org_id}")
        return result

    def _generate_slug(self, name: str) -> str:
        """Generate URL-safe slug from name"""
        # Convert to lowercase, replace spaces with hyphens
        slug = name.lower().strip()
        # Replace non-alphanumeric chars (except hyphens) with hyphens
        slug = re.sub(r'[^a-z0-9-]', '-', slug)
        # Remove consecutive hyphens
        slug = re.sub(r'-+', '-', slug)
        # Remove leading/trailing hyphens
        slug = slug.strip('-')
        return slug or 'org'

    def _is_valid_slug(self, slug: str) -> bool:
        """Check if slug is valid"""
        return bool(re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', slug) or
                    re.match(r'^[a-z0-9]$', slug))
