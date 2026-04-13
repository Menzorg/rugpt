# Departments & Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add departments (flat list within organization), symmetric visibility rules between departments, and org_context injection into AI prompts.

**Architecture:** New model/storage/service/route layer for departments. Central `DepartmentService.get_visible_user_ids()` method used by all existing services and routes for visibility filtering. Org context stored as text field on organizations, injected into PromptCache before role prompt.

**Tech Stack:** Python 3.10+, FastAPI, asyncpg, PostgreSQL (pgvector already enabled), Tika (for org_context file upload)

**Spec:** `docs/superpowers/specs/2026-04-10-departments-visibility-design.md`

---

### Task 1: Migration

**Files:**
- Create: `src/engine/migrations/015_departments.sql`

- [ ] **Step 1: Write migration file**

```sql
-- 015_departments.sql
-- Departments, visibility rules, user department fields, org context

-- Departments (flat list, no nesting)
CREATE TABLE departments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_departments_org_id ON departments(org_id);

-- Symmetric visibility rules between departments
-- CHECK ensures department_a_id < department_b_id to prevent duplicate pairs
CREATE TABLE department_visibility (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    department_a_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    department_b_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(department_a_id, department_b_id),
    CHECK(department_a_id < department_b_id)
);
CREATE INDEX idx_dept_vis_a ON department_visibility(department_a_id);
CREATE INDEX idx_dept_vis_b ON department_visibility(department_b_id);

-- User belongs to one department, is_head = department manager
ALTER TABLE users ADD COLUMN department_id UUID REFERENCES departments(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN is_head BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX idx_users_department_id ON users(department_id);

-- Org context text injected into all role prompts
ALTER TABLE organizations ADD COLUMN org_context TEXT NOT NULL DEFAULT '';
```

- [ ] **Step 2: Run migration**

Run: `cd /root/rugpt && ./migrate.sh`
Expected: Migration 015 applied successfully.

- [ ] **Step 3: Verify tables exist**

Run: `psql -U postgres -h localhost -d rugpt -c "\dt departments; \dt department_visibility;"`
Expected: Both tables listed.

Run: `psql -U postgres -h localhost -d rugpt -c "SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name IN ('department_id','is_head');"`
Expected: Both columns listed.

Run: `psql -U postgres -h localhost -d rugpt -c "SELECT column_name FROM information_schema.columns WHERE table_name='organizations' AND column_name='org_context';"`
Expected: org_context listed.

---

### Task 2: Department Model

**Files:**
- Create: `src/engine/models/department.py`

- [ ] **Step 1: Create department model**

```python
"""
Department Model

Represents a department within an organization.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class Department:
    """
    Department entity.

    Flat structure within organization (no nesting).
    Users belong to one department via users.department_id.
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    name: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Department":
        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            org_id=UUID(data["org_id"]) if isinstance(data.get("org_id"), str) else data.get("org_id", uuid4()),
            name=data.get("name", ""),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
        )


@dataclass
class DepartmentVisibility:
    """
    Symmetric visibility rule between two departments.

    If exists: both departments see each other.
    department_a_id < department_b_id enforced by DB CHECK.
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    department_a_id: UUID = field(default_factory=uuid4)
    department_b_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "department_a_id": str(self.department_a_id),
            "department_b_id": str(self.department_b_id),
            "created_at": self.created_at.isoformat(),
        }
```

---

### Task 3: Department Storage

**Files:**
- Create: `src/engine/storage/department_storage.py`

- [ ] **Step 1: Create department storage**

```python
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
```

---

### Task 4: User Model + Storage Changes

**Files:**
- Modify: `src/engine/models/user.py`
- Modify: `src/engine/storage/user_storage.py`

- [ ] **Step 1: Add department_id and is_head to User model**

In `src/engine/models/user.py`, add two fields after `is_system` (line 36):

```python
    is_system: bool = False                          # Is system user (AI assistant for admins)
    department_id: Optional[UUID] = None             # Department this user belongs to
    is_head: bool = False                            # Is department head
    is_active: bool = True                           # Active/inactive status
```

In `to_dict()` add after `"is_system"` line:

```python
            "department_id": str(self.department_id) if self.department_id else None,
            "is_head": self.is_head,
```

In `from_dict()` add after `is_system` parsing:

```python
            department_id=UUID(data["department_id"]) if data.get("department_id") and isinstance(data["department_id"], str) else data.get("department_id"),
            is_head=data.get("is_head", False),
```

- [ ] **Step 2: Update UserStorage**

In `src/engine/storage/user_storage.py`:

**`create()` method** — add `department_id` and `is_head` to INSERT:

```python
    async def create(self, user: User) -> User:
        query = """
            INSERT INTO users (
                id, org_id, name, username, email, password_hash, role_id,
                is_admin, is_system, is_active, avatar_url, created_at, updated_at, last_seen_at,
                department_id, is_head
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            user.id, user.org_id, user.name, user.username, user.email,
            user.password_hash, user.role_id, user.is_admin, user.is_system, user.is_active,
            user.avatar_url, user.created_at, user.updated_at, user.last_seen_at,
            user.department_id, user.is_head,
        )
        return self._row_to_user(row)
```

**`update()` method** — add `department_id` and `is_head` to UPDATE:

```python
    async def update(self, user: User) -> User:
        user.updated_at = datetime.utcnow()
        query = """
            UPDATE users
            SET name = $2, username = $3, email = $4, password_hash = $5,
                role_id = $6, is_admin = $7, is_system = $8, is_active = $9, avatar_url = $10,
                updated_at = $11, last_seen_at = $12, department_id = $13, is_head = $14
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            user.id, user.name, user.username, user.email, user.password_hash,
            user.role_id, user.is_admin, user.is_system, user.is_active, user.avatar_url,
            user.updated_at, user.last_seen_at, user.department_id, user.is_head,
        )
        return self._row_to_user(row)
```

**`_row_to_user()` method** — add department_id and is_head:

```python
    def _row_to_user(self, row) -> User:
        return User(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            username=row["username"],
            email=row["email"],
            password_hash=row["password_hash"],
            role_id=row["role_id"],
            is_admin=row["is_admin"],
            is_system=row.get("is_system", False),
            department_id=row.get("department_id"),
            is_head=row.get("is_head", False),
            is_active=row["is_active"],
            avatar_url=row["avatar_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"],
        )
```

---

### Task 5: Organization Model + Storage Changes (org_context)

**Files:**
- Modify: `src/engine/models/organization.py`
- Modify: `src/engine/storage/org_storage.py`

- [ ] **Step 1: Add org_context to Organization model**

In `src/engine/models/organization.py`, add field after `timezone` (line 24):

```python
    timezone: str = "Europe/Moscow"         # IANA timezone for scheduler
    org_context: str = ""                   # Org structure description for AI prompts
    is_active: bool = True                  # Active/inactive status
```

In `to_dict()` add after `"timezone"`:

```python
            "org_context": self.org_context,
```

In `from_dict()` add after `timezone` parsing:

```python
            org_context=data.get("org_context", ""),
```

- [ ] **Step 2: Update OrgStorage**

Add `org_context` to all INSERT/UPDATE/SELECT queries and `_row_to_org()` method. Follow the same pattern as existing fields — read from row, include in create/update queries.

---

### Task 6: Department Service (visibility logic)

**Files:**
- Create: `src/engine/services/department_service.py`

- [ ] **Step 1: Create department service**

```python
"""
Department Service

Business logic for departments and visibility.
Central method: get_visible_user_ids() used by all other services.
"""
import logging
from typing import Optional, List, Set
from uuid import UUID

from ..models.department import Department, DepartmentVisibility
from ..storage.department_storage import DepartmentStorage
from ..storage.user_storage import UserStorage

logger = logging.getLogger("rugpt.services.department")


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

        all_users = await self._user_storage.list_by_org(org_id)

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
```

---

### Task 7: Wire into EngineService

**Files:**
- Modify: `src/engine/services/engine_service.py`

- [ ] **Step 1: Add imports**

After line 25 (`from ..storage.device_storage import DeviceStorage`), add:

```python
from ..storage.department_storage import DepartmentStorage
```

After line 39 (`from .correction_rule_service import CorrectionRuleService`), add:

```python
from .department_service import DepartmentService
```

- [ ] **Step 2: Add storage + service initialization**

In `__init__()`, after `self.device_storage` (line 81), add:

```python
        self.department_storage = DepartmentStorage(self.postgres_dsn)
```

After `self.calendar_service` initialization (line 91), add:

```python
        # Initialize department service
        self.department_service = DepartmentService(self.department_storage, self.user_storage)
```

- [ ] **Step 3: Add init/close**

In `initialize()`, after `await self.device_storage.init()` (line 262), add:

```python
        await self.department_storage.init()
```

In `close()`, after `await self.device_storage.close()` (line 294), add:

```python
        await self.department_storage.close()
```

---

### Task 8: Department Routes

**Files:**
- Create: `src/engine/routes/departments.py`
- Modify: `src/engine/app.py` (register router)

- [ ] **Step 1: Create departments router**

```python
"""
Department Routes

Admin-only endpoints for managing departments and visibility rules.
"""
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.departments")
router = APIRouter(prefix="/departments", tags=["departments"])


class CreateDepartmentRequest(BaseModel):
    name: str


class UpdateDepartmentRequest(BaseModel):
    name: str


class CreateVisibilityRuleRequest(BaseModel):
    department_a_id: str
    department_b_id: str


def _require_admin(current_user: dict):
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.post("/")
async def create_department(
    request: CreateDepartmentRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.create_department(
        org_id=current_user["org_id"],
        name=request.name,
    )
    return dept.to_dict()


@router.get("/")
async def list_departments(current_user: dict = Depends(get_current_user)):
    _require_admin(current_user)
    engine = get_engine_service()
    depts = await engine.department_service.list_departments(current_user["org_id"])
    return [d.to_dict() for d in depts]


@router.get("/{department_id}")
async def get_department(
    department_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    return dept.to_dict()


@router.patch("/{department_id}")
async def update_department(
    department_id: str,
    request: UpdateDepartmentRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    updated = await engine.department_service.update_department(UUID(department_id), request.name)
    return updated.to_dict()


@router.delete("/{department_id}")
async def delete_department(
    department_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    deleted = await engine.department_service.delete_department(UUID(department_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Department not found")
    return {"status": "deleted"}


@router.post("/{department_id}/head/{user_id}")
async def set_head(
    department_id: str,
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    success = await engine.department_service.set_head(UUID(department_id), UUID(user_id))
    if not success:
        raise HTTPException(status_code=400, detail="User not found or not in this department")
    return {"status": "ok"}


@router.delete("/{department_id}/head")
async def clear_head(
    department_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    await engine.department_service.clear_head(UUID(department_id))
    return {"status": "ok"}


@router.post("/visibility")
async def create_visibility_rule(
    request: CreateVisibilityRuleRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    rule = await engine.department_service.create_visibility_rule(
        org_id=current_user["org_id"],
        dept_a_id=UUID(request.department_a_id),
        dept_b_id=UUID(request.department_b_id),
    )
    return rule.to_dict()


@router.delete("/visibility/{rule_id}")
async def delete_visibility_rule(
    rule_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    deleted = await engine.department_service.delete_visibility_rule(UUID(rule_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "deleted"}


@router.get("/visibility")
async def list_visibility_rules(current_user: dict = Depends(get_current_user)):
    _require_admin(current_user)
    engine = get_engine_service()
    rules = await engine.department_service.list_visibility_rules(current_user["org_id"])
    return [r.to_dict() for r in rules]
```

- [ ] **Step 2: Register router in app.py**

In `src/engine/app.py`, add import and `app.include_router` for departments router, following the pattern of other routers. Add it after the existing router registrations.

```python
from .routes.departments import router as departments_router
# ... in the router registration block:
app.include_router(departments_router, prefix="/api/v1", tags=["departments"])
```

---

### Task 9: Auth Changes (current_user + login response)

**Files:**
- Modify: `src/engine/routes/auth.py`

- [ ] **Step 1: Add department_id and is_head to get_current_user()**

In `get_current_user()` (line 95-120), update the return dict to include department fields:

```python
    return {
        "user_id": user_id,
        "org_id": org_id,
        "is_admin": user.is_admin,
        "department_id": user.department_id,
        "is_head": user.is_head,
    }
```

- [ ] **Step 2: Add fields to LoginResponse**

After `is_admin` (line 46):

```python
    department_id: Optional[str] = None
    is_head: bool = False
```

- [ ] **Step 3: Add fields to login() response**

In `login()` return (line 170-179), add:

```python
        department_id=str(user.department_id) if user.department_id else None,
        is_head=user.is_head,
```

---

### Task 10: Visibility Filtering in Users Routes

**Files:**
- Modify: `src/engine/routes/users.py`

- [ ] **Step 1: Filter list_users by visibility**

In `list_users()`, after getting users from service, filter by visibility:

```python
    engine = get_engine_service()
    visible_ids = await engine.department_service.get_visible_user_ids(
        current_user["user_id"], current_user["org_id"],
    )
    # Filter users list to only visible users
    users = [u for u in users if u.id in visible_ids]
```

- [ ] **Step 2: Check visibility in get_user() and get_user_by_username()**

After fetching the user, check visibility:

```python
    engine = get_engine_service()
    if not await engine.department_service.check_visible(
        current_user["user_id"], user.id, current_user["org_id"],
    ):
        raise HTTPException(status_code=403, detail="Access denied")
```

- [ ] **Step 3: Add department_id to create/update user requests**

Add `department_id: Optional[str] = None` to CreateUserRequest and UpdateUserRequest models.

---

### Task 11: Visibility Filtering in Chats Routes

**Files:**
- Modify: `src/engine/routes/chats.py`

- [ ] **Step 1: Check visibility in create_direct_chat()**

Before creating the chat, verify the other user is visible:

```python
    engine = get_engine_service()
    if not await engine.department_service.check_visible(
        user_id, UUID(request.other_user_id), current_user["org_id"],
    ):
        raise HTTPException(status_code=403, detail="User not visible")
```

- [ ] **Step 2: Check visibility in create_group_chat()**

Verify all participant_ids are visible to the creator:

```python
    engine = get_engine_service()
    visible_ids = await engine.department_service.get_visible_user_ids(
        user_id, current_user["org_id"],
    )
    for pid in participant_uuids:
        if pid not in visible_ids:
            raise HTTPException(status_code=403, detail=f"User {pid} not visible")
```

- [ ] **Step 3: Check visibility in add_participant()**

Same pattern as create_direct_chat — verify participant is visible.

---

### Task 12: Visibility Filtering in Tasks Routes

**Files:**
- Modify: `src/engine/routes/tasks.py`

- [ ] **Step 1: Check visibility in create_task()**

Before creating, verify assignee is visible:

```python
    engine = get_engine_service()
    if not await engine.department_service.check_visible(
        current_user["user_id"], UUID(request.assignee_user_id), current_user["org_id"],
    ):
        raise HTTPException(status_code=403, detail="Assignee not visible")
```

- [ ] **Step 2: Filter list_tasks() for is_head**

For is_head users (not admin, not regular), show tasks of visible users:

```python
    if current_user.get("is_head") and not current_user.get("is_admin"):
        visible_ids = await engine.department_service.get_visible_user_ids(
            current_user["user_id"], current_user["org_id"],
        )
        tasks = [t for t in tasks if t.assignee_user_id in visible_ids]
```

- [ ] **Step 3: Check visibility in update_task() when changing assignee**

If `assignee_user_id` is being changed, verify new assignee is visible.

---

### Task 13: Visibility Filtering in Mentions

**Files:**
- Modify: `src/engine/services/mention_service.py`
- Modify: `src/engine/routes/chats.py` (pass sender_id)

- [ ] **Step 1: Add sender_id parameter to resolve_mentions()**

Change signature from `resolve_mentions(content, org_id)` to `resolve_mentions(content, org_id, sender_id=None)`.

After resolving a user, check visibility if sender_id is provided:

```python
    if sender_id and user:
        engine = get_engine_service()
        if not await engine.department_service.check_visible(sender_id, user.id, org_id):
            logger.warning(f"Mention @{username} skipped: not visible to sender {sender_id}")
            continue
```

- [ ] **Step 2: Pass sender_id in chats route send_message()**

Where `resolve_mentions()` is called in the chats route or ai_service, pass the sender's user_id.

---

### Task 14: Visibility in Task Tools

**Files:**
- Modify: `src/engine/agents/tools/task_tool.py`

- [ ] **Step 1: Add visibility check in _task_create()**

After getting `user_id` from RunnableConfig, check visibility of assignee:

```python
    engine = get_engine_service()
    if not await engine.department_service.check_visible(
        UUID(user_id), UUID(assignee_user_id), UUID(org_id),
    ):
        return "Cannot assign task: user not visible to you."
```

- [ ] **Step 2: Filter _task_query() by visibility**

After getting tasks list, filter by visible users:

```python
    engine = get_engine_service()
    visible_ids = await engine.department_service.get_visible_user_ids(
        UUID(user_id), UUID(org_id),
    )
    tasks = [t for t in tasks if t.assignee_user_id in visible_ids]
```

---

### Task 15: Visibility in Roles Routes

**Files:**
- Modify: `src/engine/routes/roles.py`

- [ ] **Step 1: Filter get_role_users() by visibility**

After getting users with role, filter by visibility:

```python
    engine = get_engine_service()
    visible_ids = await engine.department_service.get_visible_user_ids(
        current_user["user_id"], current_user["org_id"],
    )
    users = [u for u in users if u.id in visible_ids]
```

---

### Task 16: Org Context in PromptCache

**Files:**
- Modify: `src/engine/services/prompt_cache.py`

- [ ] **Step 1: Add org_context support to get_prompt()**

Change `get_prompt(role)` to `get_prompt(role, org_context: str = "")`:

```python
    def get_prompt(self, role, org_context: str = "") -> str:
        role_prompt = ""
        if role.prompt_file:
            if role.prompt_file not in self._cache:
                path = os.path.join(self._prompts_dir, role.prompt_file)
                try:
                    self._cache[role.prompt_file] = Path(path).read_text(encoding="utf-8")
                    logger.info(f"Loaded prompt from file: {role.prompt_file}")
                except FileNotFoundError:
                    logger.warning(f"Prompt file not found: {path}")
                    role_prompt = role.system_prompt or ""
            if not role_prompt:
                role_prompt = self._cache.get(role.prompt_file, "")
        else:
            role_prompt = role.system_prompt or ""

        if org_context:
            return f"{org_context}\n\n---\n\n{role_prompt}"
        return role_prompt
```

- [ ] **Step 2: Pass org_context in AgentExecutor**

In `src/engine/agents/executor.py`, where `prompt_cache.get_prompt(role)` is called, load org_context from org_storage and pass it:

```python
    org = await engine.org_storage.get_by_id(role.org_id)
    org_context = org.org_context if org else ""
    system_prompt = self.prompt_cache.get_prompt(role, org_context=org_context)
```

This requires passing `org_storage` to AgentExecutor or accessing it through the engine singleton.

---

### Task 17: Org Context Upload Endpoint

**Files:**
- Modify: `src/engine/routes/organizations.py`

- [ ] **Step 1: Add upload endpoint**

```python
@router.post("/{org_id}/context/upload")
async def upload_org_context(
    org_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload a file, extract text with Tika, save as org_context."""
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    engine = get_engine_service()
    org = await engine.org_storage.get_by_id(UUID(org_id))
    if not org or org.id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Organization not found")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    from tika import parser as tika_parser
    parsed = tika_parser.from_buffer(data, serverEndpoint=Config.RAG_TIKA_SERVER_ENDPOINT)
    content = ""
    if isinstance(parsed, dict):
        content = parsed.get("content", "") or ""
    elif isinstance(parsed, tuple) and len(parsed) >= 2:
        payload = parsed[1]
        if isinstance(payload, dict):
            content = payload.get("content", "") or ""
    content = content.strip()

    if not content:
        raise HTTPException(status_code=400, detail="Could not extract text from file")

    org.org_context = content
    await engine.org_storage.update(org)

    return {"status": "ok", "org_context_length": len(content)}
```

- [ ] **Step 2: Add org_context to PATCH /organizations/{id}**

Ensure the existing update endpoint accepts and saves `org_context` field. Add `org_context: Optional[str] = None` to the update request model.

---

### Task 18: Update Documentation

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/api.md`
- Modify: `docs/services.md`
- Modify: `docs/storage.md`
- Modify: `docs/models.md`

- [ ] **Step 1: Add departments section to architecture.md**

Add departments to the PostgreSQL tables list, add DepartmentService to services section.

- [ ] **Step 2: Add /departments/* endpoints to api.md**

Add the full departments API section.

- [ ] **Step 3: Add DepartmentService to services.md**

Document the service with `get_visible_user_ids()` algorithm.

- [ ] **Step 4: Add DepartmentStorage to storage.md**

Document storage methods and new tables schema.

- [ ] **Step 5: Add Department model to models.md**

Document Department and DepartmentVisibility dataclasses.
