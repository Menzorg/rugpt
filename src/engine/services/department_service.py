"""
Department Service

Business logic for departments and visibility.
Central method: get_visible_user_ids() used by all other services.
"""

from src.engine.unified_logger import get_logger
from typing import Optional, List, Set
from uuid import UUID

from ..models.department import Department, DepartmentVisibility
from ..storage.department_storage import DepartmentStorage
from ..storage.user_storage import UserStorage

logger = get_logger("services")

class DepartmentService:

    def __init__(self, department_storage: DepartmentStorage, user_storage: UserStorage):
        self._dept_storage = department_storage
        self._user_storage = user_storage

    # --- Department CRUD (admin only, enforced in routes) ---

    async def create_department(self, org_id: UUID, name: str) -> Department:
        dept = Department(org_id=org_id, name=name)
        return await self._dept_storage.create(dept)

    async def get_department(self, department_id: UUID) -> Optional[Department]:
        return await self._dept_storage.get_by_id(department_id)

    async def list_departments(self, org_id: UUID) -> List[Department]:
        return await self._dept_storage.list_by_org(org_id)

    async def update_department(self, department_id: UUID, name: str) -> Optional[Department]:
        return await self._dept_storage.update(department_id, name)

    async def delete_department(self, department_id: UUID) -> bool:
        return await self._dept_storage.delete(department_id)

    # --- Head management ---

    async def set_head(self, department_id: UUID, user_id: UUID) -> bool:
        """Set user as department head. Clears previous head of this department."""
        dept = await self._dept_storage.get_by_id(department_id)
        if not dept:
            return False
        user = await self._user_storage.get_by_id(user_id)
        if not user or user.department_id != department_id:
            return False
        # Clear previous head
        dept_users = await self._user_storage.list_by_org(dept.org_id)
        for u in dept_users:
            if u.department_id == department_id and u.is_head and u.id != user_id:
                u.is_head = False
                await self._user_storage.update(u)
        # Set new head
        user.is_head = True
        await self._user_storage.update(user)
        return True

    async def clear_head(self, department_id: UUID) -> bool:
        """Remove head status from department's current head."""
        dept = await self._dept_storage.get_by_id(department_id)
        if not dept:
            return False
        dept_users = await self._user_storage.list_by_org(dept.org_id)
        for u in dept_users:
            if u.department_id == department_id and u.is_head:
                u.is_head = False
                await self._user_storage.update(u)
        return True

    # --- Visibility rules ---

    async def create_visibility_rule(
        self, org_id: UUID, dept_a_id: UUID, dept_b_id: UUID,
    ) -> DepartmentVisibility:
        return await self._dept_storage.create_visibility_rule(org_id, dept_a_id, dept_b_id)

    async def delete_visibility_rule(self, rule_id: UUID) -> bool:
        return await self._dept_storage.delete_visibility_rule(rule_id)

    async def list_visibility_rules(self, org_id: UUID) -> List[DepartmentVisibility]:
        return await self._dept_storage.list_visibility_rules(org_id)

    # --- Central visibility method ---

    async def get_visible_user_ids(self, viewer_user_id: UUID, org_id: UUID) -> Set[UUID]:
        """
        Get set of user IDs visible to viewer within their organization.

        Algorithm:
        1. If viewer is admin -> return all users in org
        2. Collect visible department IDs (own + visibility rules)
        3. If viewer is_head -> add all is_head + all is_admin in org
        4. Add all users with department_id=NULL (no department = visible to all)
        5. Add all is_system users (AI users = visible to all)
        6. Return set of user IDs
        """
        viewer = await self._user_storage.get_by_id(viewer_user_id)
        if not viewer:
            return set()

        org_users = await self._user_storage.list_by_org(org_id)
        # System AI users live in RuGPT system org but must be visible everywhere.
        system_users = await self._user_storage.get_system_users()
        all_users = list(org_users) + list(system_users)

        # Admin sees everyone
        if viewer.is_admin:
            return {u.id for u in all_users}

        # Collect visible departments
        visible_dept_ids: Set[UUID] = set()
        if viewer.department_id:
            visible_dept_ids = await self._dept_storage.get_visible_department_ids(
                viewer.department_id,
            )

        result: Set[UUID] = set()
        for u in all_users:
            # System users always visible
            if u.is_system:
                result.add(u.id)
                continue
            # Users without department visible to all
            if u.department_id is None:
                result.add(u.id)
                continue
            # Users in visible departments
            if u.department_id in visible_dept_ids:
                result.add(u.id)
                continue
            # Heads see other heads and admins
            if viewer.is_head and (u.is_head or u.is_admin):
                result.add(u.id)
                continue

        # Always include self
        result.add(viewer_user_id)

        return result

    async def check_visible(self, viewer_user_id: UUID, target_user_id: UUID, org_id: UUID) -> bool:
        """Check if viewer can see target user."""
        visible = await self.get_visible_user_ids(viewer_user_id, org_id)
        return target_user_id in visible
