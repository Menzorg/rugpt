"""
Project Service

Business logic for Project entity: creation, listing, update, soft-delete.
Head/admin-only for mutating operations. Multi-tenancy via org_id guard.
"""
import logging
from typing import List, Optional, TYPE_CHECKING
from uuid import UUID

from ..models.project import Project
from ..models.user import User
from ..storage.project_storage import ProjectStorage

if TYPE_CHECKING:
    from .chat_service import ChatService

logger = logging.getLogger("rugpt.services.project")


class ProjectService:

    def __init__(
        self,
        storage: ProjectStorage,
        chat_service: Optional["ChatService"] = None,
    ):
        self.storage = storage
        self.chat_service = chat_service

    # --- Permission / multi-tenancy helpers ---

    def _check_can_create(self, user: User) -> None:
        """Only is_head or is_admin can create/edit/delete projects."""
        if not (user.is_admin or user.is_head):
            raise PermissionError("Only department head or admin can create projects")

    def _check_same_org(self, project: Project, user: User) -> None:
        """Hide cross-org projects as 'not found' to avoid existence leak."""
        if project.org_id != user.org_id:
            raise ValueError(f"Project {project.id} not found")

    # --- CRUD ---

    async def create(
        self,
        name: str,
        user: User,
        description: Optional[str] = None,
    ) -> Project:
        self._check_can_create(user)
        if not name or not name.strip():
            raise ValueError("Project name is required")
        project = Project(
            org_id=user.org_id,
            name=name.strip(),
            description=description,
            created_by_user_id=user.id,
        )
        created = await self.storage.create(project)
        logger.info(
            f"Project '{created.name}' created in org {user.org_id} by {user.id}"
        )
        return created

    async def list_by_org(
        self, org_id: UUID, include_archived: bool = False,
    ) -> List[Project]:
        return await self.storage.list_by_org(org_id, include_archived)

    async def get(self, project_id: UUID, user: User) -> Optional[Project]:
        project = await self.storage.get_by_id(project_id)
        if project is None:
            return None
        if project.org_id != user.org_id:
            return None  # hide cross-org as non-existent
        return project

    async def update(
        self,
        project_id: UUID,
        user: User,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Project:
        self._check_can_create(user)
        project = await self.storage.get_by_id(project_id)
        if project is None:
            raise ValueError(f"Project {project_id} not found")
        self._check_same_org(project, user)
        if name is not None:
            stripped = name.strip()
            if not stripped:
                raise ValueError("Project name cannot be empty")
            project.name = stripped
        if description is not None:
            project.description = description
        return await self.storage.update(project)

    async def delete(self, project_id: UUID, user: User) -> bool:
        """Soft-delete project and archive its chat.
        Tasks retain project_id; use PATCH project_id=null to detach.
        """
        self._check_can_create(user)
        project = await self.storage.get_by_id(project_id)
        if project is None:
            return False
        self._check_same_org(project, user)
        await self.storage.deactivate(project_id)
        if self.chat_service is not None:
            await self.chat_service.archive_project_chat(project_id)
        logger.info(f"Project {project_id} archived by {user.id}")
        return True
