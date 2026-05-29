"""
Project Service

Business logic for Project entity: creation, listing, update, soft-delete.
Create — any authenticated user (project stores creator's department_id, frozen).
Update/delete — creator, admin, or head of the project's department.
Multi-tenancy via org_id guard.
"""

from src.engine.unified_logger import get_logger
from typing import List, Optional, TYPE_CHECKING
from uuid import UUID

from ..models.project import Project
from ..models.user import User
from ..storage.project_storage import ProjectStorage

if TYPE_CHECKING:
    from .chat_service import ChatService

logger = get_logger("services")

class ProjectService:

    def __init__(
        self,
        storage: ProjectStorage,
        chat_service: Optional["ChatService"] = None,
    ):
        self.storage = storage
        self.chat_service = chat_service

    # --- Permission / multi-tenancy helpers ---

    def _check_same_org(self, project: Project, user: User) -> None:
        """Hide cross-org projects as 'not found' to avoid existence leak."""
        if project.org_id != user.org_id:
            raise ValueError(f"Project {project.id} not found")

    def _check_can_modify(self, project: Project, user: User) -> None:
        """Creator, admin (whole org), or head of the project's department."""
        if user.is_admin:
            return
        if project.created_by_user_id == user.id:
            return
        if (user.is_head and user.department_id is not None
                and project.department_id == user.department_id):
            return
        raise PermissionError(
            "Only the creator, the head of the project's department, or an admin "
            "can modify this project"
        )

    # --- CRUD ---

    async def create(
        self,
        name: str,
        user: User,
        description: Optional[str] = None,
    ) -> Project:
        if not name or not name.strip():
            raise ValueError("Project name is required")
        project = Project(
            org_id=user.org_id,
            name=name.strip(),
            description=description,
            created_by_user_id=user.id,
            department_id=user.department_id,
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

    async def list_visible(self, user: User, include_archived: bool = False) -> List[Project]:
        if user.is_admin:
            return await self.storage.list_by_org(user.org_id, include_archived)
        department_id = user.department_id if (user.is_head and user.department_id is not None) else None
        return await self.storage.list_visible_for_user(
            user.id, user.org_id, include_archived, department_id=department_id,
        )

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
        project = await self.storage.get_by_id(project_id)
        if project is None:
            raise ValueError(f"Project {project_id} not found")
        self._check_same_org(project, user)
        self._check_can_modify(project, user)
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
        project = await self.storage.get_by_id(project_id)
        if project is None:
            return False
        self._check_same_org(project, user)
        self._check_can_modify(project, user)
        await self.storage.deactivate(project_id)
        if self.chat_service is not None:
            await self.chat_service.archive_project_chat(project_id)
        logger.info(f"Project {project_id} archived by {user.id}")
        return True
