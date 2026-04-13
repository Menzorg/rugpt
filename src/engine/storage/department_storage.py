"""
Department Storage

PostgreSQL storage for departments and visibility rules.
"""
import logging
from datetime import datetime
from typing import Optional, List, Set
from uuid import UUID

from .base import BaseStorage
from ..models.department import Department, DepartmentVisibility

logger = logging.getLogger("rugpt.storage.department")


class DepartmentStorage(BaseStorage):

    # --- Department CRUD ---

    async def create(self, department: Department) -> Department:
        query = """
            INSERT INTO departments (id, org_id, name, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
        """
        row = await self.fetchrow(
            query, department.id, department.org_id, department.name,
            department.created_at, department.updated_at,
        )
        return self._row_to_department(row)

    async def get_by_id(self, department_id: UUID) -> Optional[Department]:
        row = await self.fetchrow("SELECT * FROM departments WHERE id = $1", department_id)
        return self._row_to_department(row) if row else None

    async def list_by_org(self, org_id: UUID) -> List[Department]:
        rows = await self.fetch(
            "SELECT * FROM departments WHERE org_id = $1 ORDER BY name", org_id,
        )
        return [self._row_to_department(r) for r in rows]

    async def update(self, department_id: UUID, name: str) -> Optional[Department]:
        row = await self.fetchrow(
            """
            UPDATE departments SET name = $2, updated_at = $3
            WHERE id = $1 RETURNING *
            """,
            department_id, name, datetime.utcnow(),
        )
        return self._row_to_department(row) if row else None

    async def delete(self, department_id: UUID) -> bool:
        result = await self.execute("DELETE FROM departments WHERE id = $1", department_id)
        return "DELETE 1" in result

    # --- Visibility rules ---

    async def create_visibility_rule(
        self, org_id: UUID, dept_a_id: UUID, dept_b_id: UUID,
    ) -> DepartmentVisibility:
        # Enforce a < b ordering for CHECK constraint
        a, b = (dept_a_id, dept_b_id) if dept_a_id < dept_b_id else (dept_b_id, dept_a_id)
        row = await self.fetchrow(
            """
            INSERT INTO department_visibility (org_id, department_a_id, department_b_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (department_a_id, department_b_id) DO NOTHING
            RETURNING *
            """,
            org_id, a, b,
        )
        if not row:
            # Already exists
            row = await self.fetchrow(
                "SELECT * FROM department_visibility WHERE department_a_id = $1 AND department_b_id = $2",
                a, b,
            )
        return self._row_to_visibility(row)

    async def delete_visibility_rule(self, rule_id: UUID) -> bool:
        result = await self.execute("DELETE FROM department_visibility WHERE id = $1", rule_id)
        return "DELETE 1" in result

    async def list_visibility_rules(self, org_id: UUID) -> List[DepartmentVisibility]:
        rows = await self.fetch(
            "SELECT * FROM department_visibility WHERE org_id = $1 ORDER BY created_at", org_id,
        )
        return [self._row_to_visibility(r) for r in rows]

    async def get_visible_department_ids(self, department_id: UUID) -> Set[UUID]:
        """Get all department IDs visible from given department (including self)."""
        rows = await self.fetch(
            """
            SELECT department_b_id AS other_id FROM department_visibility WHERE department_a_id = $1
            UNION
            SELECT department_a_id AS other_id FROM department_visibility WHERE department_b_id = $1
            """,
            department_id,
        )
        result = {department_id}  # always see own department
        for row in rows:
            result.add(row["other_id"])
        return result

    # --- Row converters ---

    def _row_to_department(self, row) -> Department:
        return Department(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_visibility(self, row) -> DepartmentVisibility:
        return DepartmentVisibility(
            id=row["id"],
            org_id=row["org_id"],
            department_a_id=row["department_a_id"],
            department_b_id=row["department_b_id"],
            created_at=row["created_at"],
        )
