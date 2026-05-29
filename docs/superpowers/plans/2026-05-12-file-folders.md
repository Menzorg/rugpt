# File Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать пользователю возможность организовывать свои файлы в дерево папок (CRUD + nest + cascade delete). Чистый UI/UX без интеграции с агентными тулзами — те идут в техдолг.

**Architecture:** Adjacency list (`user_file_folders.parent_folder_id`) + добавление `user_files.folder_id`. Recursive CTE только в storage. Cascade delete — последовательный soft-delete (cross-pool tx в codebase нет), идемпотентный при retry. Webclient — тонкий proxy с явным snake↔camel mapping. Frontend — переписанная `/files` страница с FolderTree + Breadcrumb + URL-state.

**Tech Stack:** Engine — Python 3.10+, FastAPI, asyncpg, PostgreSQL 16. WebClient — NestJS 11, Next.js 15, React 19, Tailwind. Spec: `/root/rugpt/docs/superpowers/specs/2026-05-12-file-folders-design.md`.

> **Git policy (override):** Шаги `git add` / `git commit` в задачах ниже **НЕ выполнять**. На dev-машине git не используется (юзер ведёт git сам со своего Mac). Subagent'ы тоже не запускают git. Каждая «commit»-инструкция превращается в «отметить task завершённым, изменения остаются в working tree, юзер коммитит позже».

---

## Phase 1: Engine — DB migration

### Task 1: Create migration `037_user_file_folders.sql`

**Files:**
- Create: `/root/rugpt/src/engine/migrations/037_user_file_folders.sql`

- [ ] **Step 1: Write migration SQL**

```sql
-- =================================================================
-- 037: user_file_folders + user_files.folder_id
-- =================================================================
-- Личные папки пользователя для организации файлов.
-- Adjacency list (parent_folder_id), soft-delete через is_active.
-- Уникальность имени в одном parent (case-insensitive) — partial unique index
-- с NULLS NOT DISTINCT (PG 15+).
-- =================================================================

CREATE TABLE IF NOT EXISTS user_file_folders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    parent_folder_id UUID REFERENCES user_file_folders(id),
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_folders_user_parent
    ON user_file_folders(user_id, parent_folder_id)
    WHERE is_active = true;

CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_name_unique
    ON user_file_folders(user_id, parent_folder_id, lower(name))
    NULLS NOT DISTINCT
    WHERE is_active = true;

ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS folder_id UUID REFERENCES user_file_folders(id);

CREATE INDEX IF NOT EXISTS idx_user_files_folder
    ON user_files(user_id, folder_id)
    WHERE is_active = true;

COMMENT ON TABLE user_file_folders IS
    'Personal folders for user files. Adjacency list, soft-delete via is_active.';
COMMENT ON COLUMN user_file_folders.parent_folder_id IS
    'NULL = root level. NOT ON DELETE CASCADE — soft-delete only.';
COMMENT ON COLUMN user_files.folder_id IS
    'NULL = root level. folder.user_id must equal file.user_id (service-enforced).';
```

- [ ] **Step 2: Apply migration on dev**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python src/engine/migrations/migrate.py
```

Expected output includes:
```
Running migration: 037_user_file_folders.sql
```

- [ ] **Step 3: Verify schema in psql**

```bash
psql -U postgres -h localhost -d rugpt -c "\d user_file_folders"
```

Expected: table exists with all columns.

```bash
psql -U postgres -h localhost -d rugpt -c "\d user_files" | grep folder_id
```

Expected: column `folder_id` listed.

- [ ] **Step 4: Commit**

```bash
cd /root/rugpt && git add src/engine/migrations/037_user_file_folders.sql && git commit -m "feat(folders): add migration 037 for user_file_folders + user_files.folder_id"
```

---

## Phase 2: Engine — Models

### Task 2: UserFileFolder model

**Files:**
- Create: `/root/rugpt/src/engine/models/user_file_folder.py`

- [ ] **Step 1: Write the model**

```python
"""
User File Folder Model

Personal folder for organizing user files.
Adjacency list (parent_folder_id), soft-delete via is_active.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class UserFileFolder:
    """
    Folder metadata record. Personal (owned by user_id).
    parent_folder_id IS NULL → root-level folder.
    """
    id: UUID = field(default_factory=uuid4)
    user_id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    parent_folder_id: Optional[UUID] = None
    name: str = ""
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "org_id": str(self.org_id),
            "parent_folder_id": str(self.parent_folder_id) if self.parent_folder_id else None,
            "name": self.name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
```

- [ ] **Step 2: Commit**

```bash
cd /root/rugpt && git add src/engine/models/user_file_folder.py && git commit -m "feat(folders): add UserFileFolder model"
```

### Task 3: Add `folder_id` to UserFile model

**Files:**
- Modify: `/root/rugpt/src/engine/models/user_file.py`

- [ ] **Step 1: Add `folder_id` field after `cloned_from_file_id`**

Insert after the line `cloned_from_file_id: Optional[UUID] = None  # if non-null, metadata-only clone of source file_id`:

```python
    folder_id: Optional[UUID] = None  # NULL = root level; folder.user_id must equal self.user_id
```

- [ ] **Step 2: Update `to_dict()`**

After the `"cloned_from_file_id": ...,` line in `to_dict()`, add:

```python
            "folder_id": str(self.folder_id) if self.folder_id else None,
```

- [ ] **Step 3: Commit**

```bash
cd /root/rugpt && git add src/engine/models/user_file.py && git commit -m "feat(folders): add folder_id to UserFile model"
```

---

## Phase 3: Engine — Storage

### Task 4: Write failing tests for `UserFileFolderStorage`

**Files:**
- Create: `/root/rugpt/tests/test_user_file_folder_storage.py`

- [ ] **Step 1: Write the integration tests**

```python
"""Integration tests for UserFileFolderStorage against real PostgreSQL."""
import pytest
import pytest_asyncio
from uuid import uuid4

import asyncpg

from src.engine.config import Config
from src.engine.storage.user_file_folder_storage import UserFileFolderStorage
from src.engine.models.user_file_folder import UserFileFolder
from src.engine.storage.org_storage import OrgStorage
from src.engine.storage.user_storage import UserStorage
from src.engine.models.organization import Organization
from src.engine.models.user import User


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def storage():
    s = UserFileFolderStorage(Config.get_postgres_dsn())
    await s.init()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def fixtures():
    """Create an org + user, return their ids + cleanup."""
    org_storage = OrgStorage(Config.get_postgres_dsn())
    user_storage = UserStorage(Config.get_postgres_dsn())
    await org_storage.init()
    await user_storage.init()

    org = await org_storage.create(Organization(
        name=f"test-org-{uuid4().hex[:8]}",
        slug=f"test-{uuid4().hex[:8]}",
    ))
    user = await user_storage.create(User(
        org_id=org.id, name="Tester", username=f"tester-{uuid4().hex[:8]}",
        email=f"{uuid4().hex[:8]}@test.local", password_hash="", is_admin=False,
    ))
    yield {"org_id": org.id, "user_id": user.id}
    await user_storage.execute("DELETE FROM user_file_folders WHERE user_id = $1", user.id)
    await org_storage.close()
    await user_storage.close()


async def test_create_root_folder(storage, fixtures):
    f = UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Root1")
    saved = await storage.create(f)
    assert saved.id == f.id
    assert saved.parent_folder_id is None
    assert saved.is_active is True


async def test_create_nested_folder(storage, fixtures):
    parent = await storage.create(UserFileFolder(
        user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Parent"))
    child = await storage.create(UserFileFolder(
        user_id=fixtures["user_id"], org_id=fixtures["org_id"],
        parent_folder_id=parent.id, name="Child"))
    assert child.parent_folder_id == parent.id


async def test_list_subtree_ids_includes_self(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="B"))
    c = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=b.id, name="C"))
    ids = await storage.list_subtree_ids(a.id)
    assert ids == {a.id, b.id, c.id}


async def test_list_subtree_ids_inactive_excluded(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="B"))
    await storage.execute("UPDATE user_file_folders SET is_active = false WHERE id = $1", b.id)
    ids = await storage.list_subtree_ids(a.id)
    assert ids == {a.id}


async def test_deactivate_subtree_returns_ids(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="B"))
    deactivated = await storage.deactivate_subtree(a.id)
    assert set(deactivated) == {a.id, b.id}
    assert await storage.get_by_id(a.id) is None


async def test_unique_name_in_same_parent(storage, fixtures):
    await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Same"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Same"))


async def test_unique_name_case_insensitive(storage, fixtures):
    await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="docs"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="DOCS"))


async def test_same_name_different_parents_allowed(storage, fixtures):
    p1 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="P1"))
    p2 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="P2"))
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p1.id, name="Same"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p2.id, name="Same"))
    assert a.id != b.id


async def test_inactive_name_does_not_block(storage, fixtures):
    f1 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="X"))
    await storage.execute("UPDATE user_file_folders SET is_active = false WHERE id = $1", f1.id)
    f2 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="X"))
    assert f2.id != f1.id


async def test_list_by_user(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="B"))
    all_ = await storage.list_by_user(fixtures["user_id"])
    assert {f.id for f in all_} == {a.id, b.id}


async def test_update_folder(storage, fixtures):
    f = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Old"))
    f.name = "New"
    updated = await storage.update(f)
    assert updated.name == "New"


async def test_list_children_root(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="B"))
    await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="Nested"))
    roots = await storage.list_children(fixtures["user_id"], None)
    assert {f.id for f in roots} == {a.id, b.id}


async def test_list_children_under_parent(storage, fixtures):
    p = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="P"))
    c1 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p.id, name="C1"))
    c2 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p.id, name="C2"))
    children = await storage.list_children(fixtures["user_id"], p.id)
    assert {f.id for f in children} == {c1.id, c2.id}
```

- [ ] **Step 2: Run tests, verify they fail with ModuleNotFoundError**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -m pytest tests/test_user_file_folder_storage.py -v 2>&1 | tail -20
```

Expected: FAIL with `ModuleNotFoundError` (storage class doesn't exist yet).

- [ ] **Step 3: Commit failing tests**

```bash
cd /root/rugpt && git add tests/test_user_file_folder_storage.py && git commit -m "test(folders): failing tests for UserFileFolderStorage"
```

### Task 5: Implement `UserFileFolderStorage`

**Files:**
- Create: `/root/rugpt/src/engine/storage/user_file_folder_storage.py`

- [ ] **Step 1: Write the storage class**

```python
"""
User File Folder Storage

PostgreSQL CRUD for user_file_folders.
Recursive CTE for subtree reads (capped at depth 20).
"""
import logging
from typing import List, Optional, Set
from uuid import UUID

from .base import BaseStorage
from ..models.user_file_folder import UserFileFolder

logger = logging.getLogger("rugpt.storage.user_file_folder")


class UserFileFolderStorage(BaseStorage):

    async def create(self, folder: UserFileFolder) -> UserFileFolder:
        """Insert a new folder. Raises asyncpg.UniqueViolationError on name conflict."""
        row = await self.fetchrow(
            """
            INSERT INTO user_file_folders
                (id, user_id, org_id, parent_folder_id, name,
                 is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            folder.id, folder.user_id, folder.org_id, folder.parent_folder_id,
            folder.name, folder.is_active, folder.created_at, folder.updated_at,
        )
        return self._row_to_folder(row)

    async def get_by_id(self, folder_id: UUID) -> Optional[UserFileFolder]:
        row = await self.fetchrow(
            "SELECT * FROM user_file_folders WHERE id = $1 AND is_active = true",
            folder_id,
        )
        return self._row_to_folder(row) if row else None

    async def list_by_user(self, user_id: UUID) -> List[UserFileFolder]:
        rows = await self.fetch(
            """
            SELECT * FROM user_file_folders
            WHERE user_id = $1 AND is_active = true
            ORDER BY parent_folder_id NULLS FIRST, lower(name)
            """,
            user_id,
        )
        return [self._row_to_folder(r) for r in rows]

    async def list_children(
        self, user_id: UUID, parent_folder_id: Optional[UUID],
    ) -> List[UserFileFolder]:
        rows = await self.fetch(
            """
            SELECT * FROM user_file_folders
            WHERE user_id = $1
              AND parent_folder_id IS NOT DISTINCT FROM $2
              AND is_active = true
            ORDER BY lower(name)
            """,
            user_id, parent_folder_id,
        )
        return [self._row_to_folder(r) for r in rows]

    async def list_subtree_ids(self, folder_id: UUID) -> Set[UUID]:
        """Self + all active descendants. Hard-cap depth < 20."""
        rows = await self.fetch(
            """
            WITH RECURSIVE subtree AS (
                SELECT id, 0 AS depth FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, s.depth + 1
                FROM user_file_folders f
                JOIN subtree s ON f.parent_folder_id = s.id
                WHERE f.is_active = true AND s.depth < 20
            )
            SELECT id FROM subtree
            """,
            folder_id,
        )
        return {r["id"] for r in rows}

    async def deactivate_subtree(self, folder_id: UUID) -> List[UUID]:
        """Soft-delete self + all active descendants. Returns deactivated ids."""
        rows = await self.fetch(
            """
            WITH RECURSIVE subtree AS (
                SELECT id, 0 AS depth FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, s.depth + 1
                FROM user_file_folders f
                JOIN subtree s ON f.parent_folder_id = s.id
                WHERE f.is_active = true AND s.depth < 20
            )
            UPDATE user_file_folders f
            SET is_active = false, updated_at = NOW()
            FROM subtree s
            WHERE f.id = s.id
            RETURNING f.id
            """,
            folder_id,
        )
        return [r["id"] for r in rows]

    async def update(self, folder: UserFileFolder) -> Optional[UserFileFolder]:
        """Update name and/or parent_folder_id. Raises UniqueViolationError on conflict."""
        row = await self.fetchrow(
            """
            UPDATE user_file_folders
            SET name = $2,
                parent_folder_id = $3,
                updated_at = NOW()
            WHERE id = $1 AND is_active = true
            RETURNING *
            """,
            folder.id, folder.name, folder.parent_folder_id,
        )
        return self._row_to_folder(row) if row else None

    async def get_depth(self, folder_id: UUID) -> Optional[int]:
        """Compute depth of a folder by walking parent_folder_id chain up to root. None if not active."""
        return await self.fetchval(
            """
            WITH RECURSIVE up AS (
                SELECT id, parent_folder_id, 0 AS depth
                FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, f.parent_folder_id, up.depth + 1
                FROM user_file_folders f
                JOIN up ON up.parent_folder_id = f.id
                WHERE f.is_active = true AND up.depth < 20
            )
            SELECT MAX(depth) FROM up
            """,
            folder_id,
        )

    async def get_subtree_max_depth(self, folder_id: UUID) -> int:
        """Max depth from folder down to its deepest active descendant (0 if no children)."""
        val = await self.fetchval(
            """
            WITH RECURSIVE down AS (
                SELECT id, 0 AS d FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, d.d + 1 FROM user_file_folders f
                JOIN down d ON f.parent_folder_id = d.id
                WHERE f.is_active = true AND d.d < 20
            )
            SELECT MAX(d) FROM down
            """,
            folder_id,
        )
        return int(val) if val is not None else 0

    def _row_to_folder(self, row) -> UserFileFolder:
        return UserFileFolder(
            id=row["id"],
            user_id=row["user_id"],
            org_id=row["org_id"],
            parent_folder_id=row["parent_folder_id"],
            name=row["name"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
```

- [ ] **Step 2: Run tests, verify they pass**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -m pytest tests/test_user_file_folder_storage.py -v 2>&1 | tail -20
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
cd /root/rugpt && git add src/engine/storage/user_file_folder_storage.py && git commit -m "feat(folders): implement UserFileFolderStorage"
```

### Task 6: Extend `UserFileStorage` with folder-aware methods

**Files:**
- Modify: `/root/rugpt/src/engine/storage/user_file_storage.py`

- [ ] **Step 1: Update `create()` to include `folder_id`**

Replace the existing `create` method (the `INSERT` SQL plus the call) with:

```python
    async def create(self, file: UserFile) -> UserFile:
        """Create a new file metadata record"""
        query = """
            INSERT INTO user_files
                (id, user_id, org_id, uploaded_by_user_id,
                 storage_key, original_filename, file_type,
                 file_size, content_hash, summary, is_table, is_public, rag_status,
                 is_active, cloned_from_file_id, folder_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            file.id, file.user_id, file.org_id, file.uploaded_by_user_id,
            file.storage_key, file.original_filename, file.file_type,
            file.file_size, file.content_hash, file.summary, file.is_table, file.is_public, file.rag_status,
            file.is_active, file.cloned_from_file_id, file.folder_id, file.created_at, file.updated_at,
        )
        return self._row_to_file(row)
```

- [ ] **Step 2: Update `_row_to_file()` to read `folder_id`**

In the existing `_row_to_file`, after the line:

```python
            cloned_from_file_id=row["cloned_from_file_id"] if "cloned_from_file_id" in keys else None,
```

insert:

```python
            folder_id=row["folder_id"] if "folder_id" in keys else None,
```

- [ ] **Step 3: Add `list_by_user_in_folder` method**

Append before the `_row_to_file` definition:

```python
    async def list_by_user_in_folder(
        self, user_id: UUID, folder_id: Optional[UUID],
    ) -> List[UserFile]:
        """List active files of a user inside a specific folder. folder_id=None → root."""
        rows = await self.fetch(
            """
            SELECT * FROM user_files
            WHERE user_id = $1
              AND folder_id IS NOT DISTINCT FROM $2
              AND is_active = true
            ORDER BY created_at DESC
            """,
            user_id, folder_id,
        )
        return [self._row_to_file(r) for r in rows]
```

- [ ] **Step 4: Add `list_by_folder_ids` method**

Append:

```python
    async def list_by_folder_ids(
        self, folder_ids: List[UUID],
    ) -> List[UserFile]:
        """List active files in any of the given folders. Used by cascade-delete."""
        if not folder_ids:
            return []
        rows = await self.fetch(
            """
            SELECT * FROM user_files
            WHERE folder_id = ANY($1::uuid[]) AND is_active = true
            """,
            list(folder_ids),
        )
        return [self._row_to_file(r) for r in rows]
```

- [ ] **Step 5: Add `move_to_folder` method**

Append:

```python
    async def move_to_folder(
        self, file_id: UUID, folder_id: Optional[UUID],
    ) -> Optional[UserFile]:
        """Set folder_id on a file. folder_id=None → move to root."""
        row = await self.fetchrow(
            """
            UPDATE user_files
            SET folder_id = $2, updated_at = $3
            WHERE id = $1 AND is_active = true
            RETURNING *
            """,
            file_id, folder_id, datetime.utcnow(),
        )
        return self._row_to_file(row) if row else None
```

- [ ] **Step 6: Add `deactivate_by_folder_ids` method**

Append:

```python
    async def deactivate_by_folder_ids(self, folder_ids: List[UUID]) -> List[UUID]:
        """Soft-delete all active files whose folder_id is in the set. Returns affected file_ids."""
        if not folder_ids:
            return []
        rows = await self.fetch(
            """
            UPDATE user_files
            SET is_active = false, updated_at = $2
            WHERE folder_id = ANY($1::uuid[]) AND is_active = true
            RETURNING id
            """,
            list(folder_ids), datetime.utcnow(),
        )
        return [r["id"] for r in rows]
```

- [ ] **Step 7: Write integration tests for new methods**

Create `/root/rugpt/tests/test_user_file_storage_folders.py`:

```python
"""Integration tests for UserFileStorage folder-aware methods."""
import pytest
import pytest_asyncio
from uuid import uuid4

from src.engine.config import Config
from src.engine.storage.user_file_storage import UserFileStorage
from src.engine.storage.user_file_folder_storage import UserFileFolderStorage
from src.engine.storage.org_storage import OrgStorage
from src.engine.storage.user_storage import UserStorage
from src.engine.models.user_file import UserFile
from src.engine.models.user_file_folder import UserFileFolder
from src.engine.models.organization import Organization
from src.engine.models.user import User


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env():
    org_s = OrgStorage(Config.get_postgres_dsn())
    user_s = UserStorage(Config.get_postgres_dsn())
    file_s = UserFileStorage(Config.get_postgres_dsn())
    folder_s = UserFileFolderStorage(Config.get_postgres_dsn())
    for s in (org_s, user_s, file_s, folder_s):
        await s.init()
    org = await org_s.create(Organization(name=f"o-{uuid4().hex[:6]}", slug=f"o-{uuid4().hex[:6]}"))
    user = await user_s.create(User(
        org_id=org.id, name="t", username=f"u-{uuid4().hex[:6]}",
        email=f"{uuid4().hex[:6]}@x.x", password_hash="", is_admin=False,
    ))
    yield {"org_id": org.id, "user_id": user.id, "file_s": file_s, "folder_s": folder_s}
    await user_s.execute("DELETE FROM user_files WHERE user_id=$1", user.id)
    await user_s.execute("DELETE FROM user_file_folders WHERE user_id=$1", user.id)
    for s in (org_s, user_s, file_s, folder_s):
        await s.close()


async def _make_file(env, folder_id=None, name="f.pdf"):
    return await env["file_s"].create(UserFile(
        user_id=env["user_id"], org_id=env["org_id"], uploaded_by_user_id=env["user_id"],
        storage_key=f"k/{uuid4()}", original_filename=name, file_type="pdf",
        file_size=10, content_hash=uuid4().hex, summary="", is_table=False,
        is_public=False, rag_status="not_indexed", folder_id=folder_id,
    ))


async def test_create_file_with_folder_id(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env, folder_id=folder.id, name="a.pdf")
    assert f.folder_id == folder.id


async def test_list_by_user_in_folder_root(env):
    root_f = await _make_file(env, folder_id=None, name="root.pdf")
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    await _make_file(env, folder_id=folder.id, name="nested.pdf")
    roots = await env["file_s"].list_by_user_in_folder(env["user_id"], None)
    assert {f.id for f in roots} == {root_f.id}


async def test_list_by_user_in_folder_specific(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env, folder_id=folder.id, name="x.pdf")
    in_folder = await env["file_s"].list_by_user_in_folder(env["user_id"], folder.id)
    assert {ff.id for ff in in_folder} == {f.id}


async def test_move_to_folder(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env)
    moved = await env["file_s"].move_to_folder(f.id, folder.id)
    assert moved.folder_id == folder.id


async def test_move_to_root(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env, folder_id=folder.id)
    moved = await env["file_s"].move_to_folder(f.id, None)
    assert moved.folder_id is None


async def test_deactivate_by_folder_ids(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f1 = await _make_file(env, folder_id=folder.id, name="a.pdf")
    f2 = await _make_file(env, folder_id=folder.id, name="b.pdf")
    affected = await env["file_s"].deactivate_by_folder_ids([folder.id])
    assert set(affected) == {f1.id, f2.id}
    fresh = await env["file_s"].get_by_id(f1.id)
    assert fresh is None
```

- [ ] **Step 8: Run tests**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -m pytest tests/test_user_file_storage_folders.py -v 2>&1 | tail -20
```

Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
cd /root/rugpt && git add src/engine/storage/user_file_storage.py tests/test_user_file_storage_folders.py && git commit -m "feat(folders): extend UserFileStorage with folder-aware methods"
```

---

## Phase 4: Engine — Service

### Task 7: FolderService + unit tests

**Files:**
- Create: `/root/rugpt/src/engine/services/folder_service.py`
- Create: `/root/rugpt/tests/test_folder_service.py`

- [ ] **Step 1: Write the service**

```python
"""
Folder Service

Business logic for personal file folders.
Validates ownership, cycles, depth, name uniqueness (via DB).
"""
import logging
from typing import List, Optional
from uuid import UUID

import asyncpg

from ..models.user import User
from ..models.user_file import UserFile
from ..models.user_file_folder import UserFileFolder
from ..storage.user_file_folder_storage import UserFileFolderStorage
from ..storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.services.folder")


class FolderError(Exception):
    """Base typed error for folder operations.

    code: machine-readable identifier (EMPTY_NAME, MAX_DEPTH_EXCEEDED, ...).
    message: human-readable description (Russian default).
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class FolderNotFound(FolderError):
    pass


class FolderForbidden(FolderError):
    pass


class FolderInvalidName(FolderError):
    pass


class FolderMaxDepthExceeded(FolderError):
    pass


class FolderCyclicMove(FolderError):
    pass


class FolderInvalidParentOwner(FolderError):
    pass


class FolderParentNotFound(FolderError):
    pass


class FolderNameConflict(FolderError):
    pass


class FolderService:
    MAX_DEPTH = 10
    MAX_NAME_LEN = 255

    def __init__(
        self,
        folder_storage: UserFileFolderStorage,
        file_storage: UserFileStorage,
        rag_service=None,
        storage_adapter=None,
    ):
        self.folder_storage = folder_storage
        self.file_storage = file_storage
        self.rag_service = rag_service
        self.storage_adapter = storage_adapter

    @staticmethod
    def _validate_name(name: str) -> str:
        if not name or not name.strip():
            raise FolderInvalidName("EMPTY_NAME", "Имя папки не может быть пустым")
        stripped = name.strip()
        if len(stripped) > FolderService.MAX_NAME_LEN:
            raise FolderInvalidName(
                "NAME_TOO_LONG",
                f"Имя папки слишком длинное (макс {FolderService.MAX_NAME_LEN} символов)",
            )
        return stripped

    def _check_actor_can_modify(self, folder: UserFileFolder, actor: User) -> None:
        if actor.id == folder.user_id:
            return
        if actor.is_admin and actor.org_id == folder.org_id:
            return
        raise FolderForbidden("FORBIDDEN", "Доступ запрещён")

    async def verify_folder_owner(
        self, *, folder_id: UUID, user_id: UUID, org_id: UUID,
    ) -> UserFileFolder:
        """Used by routes/file ops to confirm a folder is usable for a given user."""
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        if folder.user_id != user_id or folder.org_id != org_id:
            raise FolderInvalidParentOwner(
                "INVALID_PARENT_OWNER",
                "Папка принадлежит другому пользователю",
            )
        return folder

    async def create(
        self, *, user_id: UUID, org_id: UUID,
        parent_folder_id: Optional[UUID], name: str,
    ) -> UserFileFolder:
        clean_name = self._validate_name(name)

        if parent_folder_id is not None:
            parent = await self.folder_storage.get_by_id(parent_folder_id)
            if parent is None:
                raise FolderParentNotFound("PARENT_NOT_FOUND", "Родительская папка не найдена")
            if parent.user_id != user_id or parent.org_id != org_id:
                raise FolderInvalidParentOwner(
                    "INVALID_PARENT_OWNER",
                    "Родительская папка принадлежит другому пользователю",
                )
            parent_depth = await self.folder_storage.get_depth(parent_folder_id)
            if parent_depth is None:
                parent_depth = 0
            if parent_depth + 1 >= self.MAX_DEPTH:
                raise FolderMaxDepthExceeded(
                    "MAX_DEPTH_EXCEEDED",
                    f"Превышена максимальная глубина вложенности ({self.MAX_DEPTH} уровней)",
                )

        folder = UserFileFolder(
            user_id=user_id, org_id=org_id,
            parent_folder_id=parent_folder_id, name=clean_name,
        )
        try:
            return await self.folder_storage.create(folder)
        except asyncpg.UniqueViolationError:
            raise FolderNameConflict(
                "DUPLICATE_NAME",
                f"Папка с именем '{clean_name}' уже существует здесь",
            )

    async def rename(
        self, *, folder_id: UUID, new_name: str, actor: User,
    ) -> UserFileFolder:
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        self._check_actor_can_modify(folder, actor)
        folder.name = self._validate_name(new_name)
        try:
            return await self.folder_storage.update(folder)
        except asyncpg.UniqueViolationError:
            raise FolderNameConflict(
                "DUPLICATE_NAME",
                f"Папка с именем '{folder.name}' уже существует здесь",
            )

    async def move(
        self, *, folder_id: UUID, new_parent_id: Optional[UUID], actor: User,
    ) -> UserFileFolder:
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        self._check_actor_can_modify(folder, actor)

        if new_parent_id == folder_id:
            raise FolderCyclicMove(
                "CYCLIC_MOVE",
                "Нельзя переместить папку в саму себя",
            )

        if new_parent_id is not None:
            parent = await self.folder_storage.get_by_id(new_parent_id)
            if parent is None:
                raise FolderParentNotFound("PARENT_NOT_FOUND", "Родительская папка не найдена")
            if parent.user_id != folder.user_id or parent.org_id != folder.org_id:
                raise FolderInvalidParentOwner(
                    "INVALID_PARENT_OWNER",
                    "Родительская папка принадлежит другому пользователю",
                )
            subtree = await self.folder_storage.list_subtree_ids(folder_id)
            if new_parent_id in subtree:
                raise FolderCyclicMove(
                    "CYCLIC_MOVE",
                    "Нельзя переместить папку внутрь её собственной подпапки",
                )
            new_parent_depth = await self.folder_storage.get_depth(new_parent_id) or 0
            subtree_max = await self.folder_storage.get_subtree_max_depth(folder_id)
            if new_parent_depth + 1 + subtree_max >= self.MAX_DEPTH:
                raise FolderMaxDepthExceeded(
                    "MAX_DEPTH_EXCEEDED",
                    f"Превышена максимальная глубина вложенности ({self.MAX_DEPTH} уровней)",
                )

        folder.parent_folder_id = new_parent_id
        try:
            return await self.folder_storage.update(folder)
        except asyncpg.UniqueViolationError:
            raise FolderNameConflict(
                "DUPLICATE_NAME",
                f"Папка с именем '{folder.name}' уже существует в выбранной директории",
            )

    async def delete(
        self, *, folder_id: UUID, actor: User,
    ) -> dict:
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        self._check_actor_can_modify(folder, actor)

        subtree_ids = await self.folder_storage.list_subtree_ids(folder_id)
        if not subtree_ids:
            return {"deleted_folders": 0, "deleted_files": 0}

        files_before = await self.file_storage.list_by_folder_ids(list(subtree_ids))

        # Sequential — cross-pool tx not available in this codebase.
        # Idempotent on retry: repeat call is no-op (subtree empty after first run).
        deactivated_files = await self.file_storage.deactivate_by_folder_ids(list(subtree_ids))
        deactivated_folders = await self.folder_storage.deactivate_subtree(folder_id)

        # Best-effort RAG + disk cleanup. Failures don't roll back DB.
        for f in files_before:
            if self.rag_service is not None and f.rag_status == "indexed":
                try:
                    await self.rag_service.delete_document(f.id)
                except Exception as e:
                    logger.warning(f"RAG cleanup failed for file {f.id}: {e}")
            if self.storage_adapter is not None:
                try:
                    await self.storage_adapter.delete(f.storage_key)
                except Exception as e:
                    logger.warning(f"Storage adapter delete failed for {f.storage_key}: {e}")

        return {
            "deleted_folders": len(deactivated_folders),
            "deleted_files": len(deactivated_files),
        }

    async def get_tree(
        self, *, user_id: UUID, org_id: UUID,
    ) -> List[dict]:
        """Returns nested tree starting at root level."""
        all_folders = await self.folder_storage.list_by_user(user_id)
        all_folders = [f for f in all_folders if f.org_id == org_id]
        by_parent: dict = {}
        for f in all_folders:
            by_parent.setdefault(f.parent_folder_id, []).append(f)

        def build(parent_id: Optional[UUID]) -> List[dict]:
            nodes = by_parent.get(parent_id, [])
            result = []
            for f in nodes:
                d = f.to_dict()
                d["children"] = build(f.id)
                result.append(d)
            return result

        return build(None)

    async def list_children(
        self, *, user_id: UUID, parent_folder_id: Optional[UUID],
    ) -> List[UserFileFolder]:
        return await self.folder_storage.list_children(user_id, parent_folder_id)

    async def list_in_folder(
        self, *, user_id: UUID, folder_id: Optional[UUID],
    ) -> List[UserFile]:
        return await self.file_storage.list_by_user_in_folder(user_id, folder_id)
```

- [ ] **Step 2: Write unit tests**

Create `/root/rugpt/tests/test_folder_service.py`:

```python
"""Unit tests for FolderService with mocked storage."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg

from src.engine.services.folder_service import (
    FolderService,
    FolderNotFound, FolderForbidden, FolderInvalidName,
    FolderMaxDepthExceeded, FolderCyclicMove,
    FolderInvalidParentOwner, FolderParentNotFound, FolderNameConflict,
)
from src.engine.models.user_file_folder import UserFileFolder
from src.engine.models.user import User


pytestmark = pytest.mark.asyncio


def _make_user(user_id=None, org_id=None, is_admin=False) -> User:
    return User(
        id=user_id or uuid4(),
        org_id=org_id or uuid4(),
        name="t", username="t", email="t@x.x",
        password_hash="", is_admin=is_admin,
    )


def _mocks():
    fs = MagicMock()
    fs.get_by_id = AsyncMock()
    fs.list_by_user = AsyncMock()
    fs.list_children = AsyncMock()
    fs.list_subtree_ids = AsyncMock()
    fs.deactivate_subtree = AsyncMock()
    fs.update = AsyncMock()
    fs.create = AsyncMock()
    fs.get_depth = AsyncMock()
    fs.get_subtree_max_depth = AsyncMock()

    files = MagicMock()
    files.list_by_user_in_folder = AsyncMock(return_value=[])
    files.list_by_folder_ids = AsyncMock(return_value=[])
    files.deactivate_by_folder_ids = AsyncMock(return_value=[])

    return fs, files


async def test_create_empty_name_raises():
    fs, files = _mocks()
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidName) as ei:
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=None, name="   ")
    assert ei.value.code == "EMPTY_NAME"


async def test_create_name_too_long_raises():
    fs, files = _mocks()
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidName) as ei:
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=None, name="x" * 300)
    assert ei.value.code == "NAME_TOO_LONG"


async def test_create_parent_not_found_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderParentNotFound):
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=uuid4(), name="X")


async def test_create_parent_wrong_owner_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = UserFileFolder(user_id=uuid4(), org_id=uuid4(), name="P")
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidParentOwner):
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=uuid4(), name="X")


async def test_create_depth_exceeded_raises():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=user_id, org_id=org_id, name="P")
    fs.get_depth.return_value = 9
    svc = FolderService(fs, files)
    with pytest.raises(FolderMaxDepthExceeded):
        await svc.create(user_id=user_id, org_id=org_id, parent_folder_id=uuid4(), name="X")


async def test_create_name_conflict_maps_to_folder_name_conflict():
    fs, files = _mocks()
    fs.create.side_effect = asyncpg.UniqueViolationError("dup", severity="ERROR")
    svc = FolderService(fs, files)
    with pytest.raises(FolderNameConflict):
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=None, name="X")


async def test_create_root_ok_strips_name():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    fs.create.return_value = UserFileFolder(user_id=user_id, org_id=org_id, name="X")
    svc = FolderService(fs, files)
    await svc.create(user_id=user_id, org_id=org_id, parent_folder_id=None, name="  X  ")
    args = fs.create.call_args[0][0]
    assert args.name == "X"


async def test_rename_not_found_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderNotFound):
        await svc.rename(folder_id=uuid4(), new_name="Y", actor=_make_user())


async def test_rename_forbidden_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = UserFileFolder(user_id=uuid4(), org_id=uuid4(), name="X")
    svc = FolderService(fs, files)
    with pytest.raises(FolderForbidden):
        await svc.rename(folder_id=uuid4(), new_name="Y", actor=_make_user(user_id=uuid4()))


async def test_rename_admin_same_org_allowed():
    fs, files = _mocks()
    owner, org = uuid4(), uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=org, name="X")
    fs.update.return_value = UserFileFolder(user_id=owner, org_id=org, name="Y")
    svc = FolderService(fs, files)
    actor = _make_user(org_id=org, is_admin=True)
    out = await svc.rename(folder_id=uuid4(), new_name="Y", actor=actor)
    assert out.name == "Y"


async def test_move_into_self_raises():
    fs, files = _mocks()
    owner = uuid4()
    fid = uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    svc = FolderService(fs, files)
    with pytest.raises(FolderCyclicMove):
        await svc.move(folder_id=fid, new_parent_id=fid, actor=_make_user(user_id=owner))


async def test_move_into_descendant_raises():
    fs, files = _mocks()
    owner = uuid4()
    fid, desc_id = uuid4(), uuid4()
    org = uuid4()
    fs.get_by_id.side_effect = [
        UserFileFolder(user_id=owner, org_id=org, name="X"),
        UserFileFolder(user_id=owner, org_id=org, name="D"),
    ]
    fs.list_subtree_ids.return_value = {fid, desc_id}
    svc = FolderService(fs, files)
    with pytest.raises(FolderCyclicMove):
        await svc.move(folder_id=fid, new_parent_id=desc_id, actor=_make_user(user_id=owner))


async def test_move_depth_exceeded():
    fs, files = _mocks()
    owner = uuid4()
    org = uuid4()
    fs.get_by_id.side_effect = [
        UserFileFolder(user_id=owner, org_id=org, name="src"),
        UserFileFolder(user_id=owner, org_id=org, name="parent"),
    ]
    fs.list_subtree_ids.return_value = set()
    fs.get_depth.return_value = 5
    fs.get_subtree_max_depth.return_value = 4
    svc = FolderService(fs, files)
    with pytest.raises(FolderMaxDepthExceeded):
        await svc.move(folder_id=uuid4(), new_parent_id=uuid4(), actor=_make_user(user_id=owner))


async def test_move_to_root_ok():
    fs, files = _mocks()
    owner, org = uuid4(), uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=org, name="X")
    fs.update.return_value = UserFileFolder(user_id=owner, org_id=org, name="X", parent_folder_id=None)
    svc = FolderService(fs, files)
    out = await svc.move(folder_id=uuid4(), new_parent_id=None, actor=_make_user(user_id=owner))
    assert out.parent_folder_id is None


async def test_delete_not_found_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderNotFound):
        await svc.delete(folder_id=uuid4(), actor=_make_user())


async def test_delete_cascade_no_files():
    fs, files = _mocks()
    owner = uuid4()
    fid = uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    fs.list_subtree_ids.return_value = {fid}
    fs.deactivate_subtree.return_value = [fid]
    svc = FolderService(fs, files)
    out = await svc.delete(folder_id=fid, actor=_make_user(user_id=owner))
    assert out == {"deleted_folders": 1, "deleted_files": 0}


async def test_delete_cascade_with_files_and_rag():
    from src.engine.models.user_file import UserFile
    fs, files = _mocks()
    rag = MagicMock()
    rag.delete_document = AsyncMock()
    adapter = MagicMock()
    adapter.delete = AsyncMock()
    owner = uuid4()
    fid = uuid4()
    file1 = UserFile(user_id=owner, org_id=uuid4(), folder_id=fid,
                     storage_key="k1", rag_status="indexed")
    file2 = UserFile(user_id=owner, org_id=uuid4(), folder_id=fid,
                     storage_key="k2", rag_status="not_indexed")
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    fs.list_subtree_ids.return_value = {fid}
    fs.deactivate_subtree.return_value = [fid]
    files.list_by_folder_ids.return_value = [file1, file2]
    files.deactivate_by_folder_ids.return_value = [file1.id, file2.id]
    svc = FolderService(fs, files, rag_service=rag, storage_adapter=adapter)
    out = await svc.delete(folder_id=fid, actor=_make_user(user_id=owner))
    assert out == {"deleted_folders": 1, "deleted_files": 2}
    rag.delete_document.assert_awaited_once_with(file1.id)
    assert adapter.delete.await_count == 2


async def test_delete_rag_failure_does_not_rollback():
    from src.engine.models.user_file import UserFile
    fs, files = _mocks()
    rag = MagicMock()
    rag.delete_document = AsyncMock(side_effect=RuntimeError("rag down"))
    owner = uuid4()
    fid = uuid4()
    file1 = UserFile(user_id=owner, org_id=uuid4(), folder_id=fid, rag_status="indexed")
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    fs.list_subtree_ids.return_value = {fid}
    fs.deactivate_subtree.return_value = [fid]
    files.list_by_folder_ids.return_value = [file1]
    files.deactivate_by_folder_ids.return_value = [file1.id]
    svc = FolderService(fs, files, rag_service=rag)
    out = await svc.delete(folder_id=fid, actor=_make_user(user_id=owner))
    assert out["deleted_folders"] == 1


async def test_verify_folder_owner_ok():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    folder = UserFileFolder(user_id=user_id, org_id=org_id, name="X")
    fs.get_by_id.return_value = folder
    svc = FolderService(fs, files)
    out = await svc.verify_folder_owner(folder_id=uuid4(), user_id=user_id, org_id=org_id)
    assert out == folder


async def test_verify_folder_owner_wrong_user():
    fs, files = _mocks()
    fs.get_by_id.return_value = UserFileFolder(user_id=uuid4(), org_id=uuid4(), name="X")
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidParentOwner):
        await svc.verify_folder_owner(folder_id=uuid4(), user_id=uuid4(), org_id=uuid4())


async def test_verify_folder_owner_not_found():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderNotFound):
        await svc.verify_folder_owner(folder_id=uuid4(), user_id=uuid4(), org_id=uuid4())


async def test_get_tree_builds_nested_structure():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    a = UserFileFolder(user_id=user_id, org_id=org_id, name="A")
    b = UserFileFolder(user_id=user_id, org_id=org_id, name="B", parent_folder_id=a.id)
    c = UserFileFolder(user_id=user_id, org_id=org_id, name="C")
    fs.list_by_user.return_value = [a, b, c]
    svc = FolderService(fs, files)
    tree = await svc.get_tree(user_id=user_id, org_id=org_id)
    names = {n["name"] for n in tree}
    assert names == {"A", "C"}
    a_node = next(n for n in tree if n["name"] == "A")
    assert len(a_node["children"]) == 1
    assert a_node["children"][0]["name"] == "B"
```

- [ ] **Step 3: Run unit tests**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -m pytest tests/test_folder_service.py -v 2>&1 | tail -30
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
cd /root/rugpt && git add src/engine/services/folder_service.py tests/test_folder_service.py && git commit -m "feat(folders): FolderService + unit tests"
```

### Task 8: Extend `FileService` with folder support

**Files:**
- Modify: `/root/rugpt/src/engine/services/file_service.py`

- [ ] **Step 1: Add `folder_id` to `upload()`**

In the `upload` method signature, add `folder_id: Optional[UUID] = None` after the `is_public: bool = False,` parameter. Add to the `UserFile(...)` construction: `folder_id=folder_id,` after `rag_status="not_indexed",`.

The block becomes:

```python
        # folder_id is trusted: routes call folder_service.verify_folder_owner before upload.
        file_record = UserFile(
            user_id=user_id,
            org_id=org_id,
            uploaded_by_user_id=uploaded_by_user_id,
            original_filename=filename,
            file_type=ext,
            file_size=len(data),
            content_hash=content_hash,
            is_public=is_public,
            is_table=is_table,
            rag_status="not_indexed",
            folder_id=folder_id,
        )
```

- [ ] **Step 2: Add `move_to_folder` method**

Append before `change_public`:

```python
    async def move_to_folder(
        self, file_id: UUID, new_folder_id: Optional[UUID],
        actor_user_id: UUID, actor_is_admin: bool, actor_org_id: UUID,
    ) -> Optional[UserFile]:
        """Move a file to a different folder (or to root if new_folder_id is None).

        Authorization: actor must be the file owner, or admin of the same org.
        Caller MUST verify new_folder_id (if not None) belongs to file owner via
        folder_service.verify_folder_owner before calling.
        """
        file_record = await self.file_storage.get_by_id(file_id)
        if file_record is None:
            return None
        is_owner = file_record.user_id == actor_user_id
        is_admin_same_org = actor_is_admin and file_record.org_id == actor_org_id
        if not (is_owner or is_admin_same_org):
            raise PermissionError("Only the file owner or admin can move this file")
        return await self.file_storage.move_to_folder(file_id, new_folder_id)
```

- [ ] **Step 3: Commit**

```bash
cd /root/rugpt && git add src/engine/services/file_service.py && git commit -m "feat(folders): FileService.upload accepts folder_id, add move_to_folder"
```

---

## Phase 5: Engine — Composite wiring

### Task 9: Register folder_storage + folder_service in EngineService

**Files:**
- Modify: `/root/rugpt/src/engine/services/engine_service.py`

- [ ] **Step 1: Add storage import**

Near the other storage imports (around line 27):

```python
from ..storage.user_file_folder_storage import UserFileFolderStorage
```

- [ ] **Step 2: Add service import**

Near the other service imports (around line 55):

```python
from .folder_service import FolderService
```

- [ ] **Step 3: Instantiate storage**

In `__init__` block where storages are created, after `self.user_file_storage = UserFileStorage(self.postgres_dsn)`:

```python
        self.user_file_folder_storage = UserFileFolderStorage(self.postgres_dsn)
```

- [ ] **Step 4: Instantiate service after rag_service**

Find the line `self.rag_service = RAGService(...)` block end. After it (and after `self.file_service = FileService(...)`):

```python
        # Folder service — depends on folder storage, file storage, rag_service, storage_adapter.
        self.folder_service = FolderService(
            folder_storage=self.user_file_folder_storage,
            file_storage=self.user_file_storage,
            rag_service=self.rag_service,
            storage_adapter=self.storage_adapter,
        )
```

- [ ] **Step 5: Add storage init call**

In `initialize()`, near `await self.user_file_storage.init()`:

```python
        await self.user_file_folder_storage.init()
```

- [ ] **Step 6: Add storage close call**

In `close()`, near `await self.user_file_storage.close()`:

```python
        await self.user_file_folder_storage.close()
```

- [ ] **Step 7: Smoke-import check**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -c "from src.engine.services.engine_service import EngineService; print('import ok')"
```

Expected: `import ok`.

- [ ] **Step 8: Commit**

```bash
cd /root/rugpt && git add src/engine/services/engine_service.py && git commit -m "feat(folders): wire folder_storage + folder_service into EngineService"
```

---

## Phase 6: Engine — Routes

### Task 10: Implement `/folders` router

**Files:**
- Create: `/root/rugpt/src/engine/routes/folders.py`
- Modify: `/root/rugpt/src/engine/routes/__init__.py`
- Modify: `/root/rugpt/src/engine/app.py`
- Modify: `/root/rugpt/src/engine/routes/files.py`

- [ ] **Step 1: Write the router file**

```python
"""
Folder Routes — personal file folders.

Endpoints under /api/v1/folders:
- POST    /folders              Create
- GET     /folders              List (flat)
- GET     /folders/tree         Tree (nested children)
- GET     /folders/{id}         Get one
- PATCH   /folders/{id}         Rename and/or move
- DELETE  /folders/{id}         Cascade soft-delete
- GET     /folders/{id}/files   List direct files in this folder
"""
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..services.folder_service import (
    FolderError, FolderNotFound, FolderForbidden, FolderInvalidName,
    FolderMaxDepthExceeded, FolderCyclicMove, FolderInvalidParentOwner,
    FolderParentNotFound, FolderNameConflict,
)
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.folders")
router = APIRouter(prefix="/folders", tags=["folders"])


class FolderResponse(BaseModel):
    id: str
    user_id: str
    org_id: str
    parent_folder_id: Optional[str]
    name: str
    is_active: bool
    created_at: str
    updated_at: str


class FolderCreateRequest(BaseModel):
    name: str
    parent_folder_id: Optional[str] = None


class FolderDeleteResponse(BaseModel):
    success: bool
    deleted_folders: int
    deleted_files: int


_STATUS_MAP = {
    "EMPTY_NAME": 400, "NAME_TOO_LONG": 400,
    "MAX_DEPTH_EXCEEDED": 400, "CYCLIC_MOVE": 400,
    "INVALID_PARENT_OWNER": 400,
    "FORBIDDEN": 403,
    "FOLDER_NOT_FOUND": 404, "PARENT_NOT_FOUND": 404,
    "DUPLICATE_NAME": 409,
}


def _raise_for(e: FolderError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_MAP.get(e.code, 500),
        detail={"code": e.code, "message": e.message},
    )


def _resolve_target_user(current_user: dict, query_user_id: Optional[str]) -> UUID:
    """Non-admin must use own user_id. Admin can pass ?user_id= to target another."""
    if not query_user_id:
        return current_user["user_id"]
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Only admin can target other users"})
    try:
        return UUID(query_user_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid user_id")


def _user_to_obj(current_user: dict):
    """Build a User-like duck for FolderService permission checks."""
    class _Actor:
        id = current_user["user_id"]
        org_id = current_user["org_id"]
        is_admin = current_user.get("is_admin", False)
    return _Actor()


@router.post("", response_model=FolderResponse)
async def create_folder(
    body: FolderCreateRequest,
    user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    target_user_id = _resolve_target_user(current_user, user_id)
    parent_uuid: Optional[UUID] = None
    if body.parent_folder_id:
        try:
            parent_uuid = UUID(body.parent_folder_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid parent_folder_id")
    try:
        created = await engine.folder_service.create(
            user_id=target_user_id,
            org_id=current_user["org_id"],
            parent_folder_id=parent_uuid,
            name=body.name,
        )
        return FolderResponse(**created.to_dict())
    except FolderError as e:
        raise _raise_for(e)


@router.get("", response_model=List[FolderResponse])
async def list_folders(
    user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    target_user_id = _resolve_target_user(current_user, user_id)
    folders = await engine.user_file_folder_storage.list_by_user(target_user_id)
    return [FolderResponse(**f.to_dict()) for f in folders]


@router.get("/tree")
async def get_folder_tree(
    user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    target_user_id = _resolve_target_user(current_user, user_id)
    return await engine.folder_service.get_tree(
        user_id=target_user_id,
        org_id=current_user["org_id"],
    )


@router.get("/{folder_id}", response_model=FolderResponse)
async def get_folder(
    folder_id: str,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    folder = await engine.user_file_folder_storage.get_by_id(fuuid)
    if folder is None:
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND", "message": "Папка не найдена"})
    if folder.user_id != current_user["user_id"] and not (
        current_user.get("is_admin") and folder.org_id == current_user["org_id"]
    ):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Доступ запрещён"})
    return FolderResponse(**folder.to_dict())


@router.patch("/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: str,
    body: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """Rename and/or move. Body keys: 'name' (string) and/or 'parent_folder_id' (string|null|absent)."""
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    actor = _user_to_obj(current_user)
    try:
        result = None
        if "name" in body:
            result = await engine.folder_service.rename(
                folder_id=fuuid, new_name=body["name"], actor=actor,
            )
        if "parent_folder_id" in body:
            new_parent = body["parent_folder_id"]
            new_parent_uuid = UUID(new_parent) if new_parent else None
            result = await engine.folder_service.move(
                folder_id=fuuid, new_parent_id=new_parent_uuid, actor=actor,
            )
        if result is None:
            folder = await engine.user_file_folder_storage.get_by_id(fuuid)
            if folder is None:
                raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND", "message": "Папка не найдена"})
            return FolderResponse(**folder.to_dict())
        return FolderResponse(**result.to_dict())
    except FolderError as e:
        raise _raise_for(e)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID in payload")


@router.delete("/{folder_id}", response_model=FolderDeleteResponse)
async def delete_folder(
    folder_id: str,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    actor = _user_to_obj(current_user)
    try:
        out = await engine.folder_service.delete(folder_id=fuuid, actor=actor)
        return FolderDeleteResponse(success=True, **out)
    except FolderError as e:
        raise _raise_for(e)


@router.get("/{folder_id}/files")
async def list_folder_files(
    folder_id: str,
    current_user: dict = Depends(get_current_user),
):
    from .files import FileResponse
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    folder = await engine.user_file_folder_storage.get_by_id(fuuid)
    if folder is None:
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND", "message": "Папка не найдена"})
    if folder.user_id != current_user["user_id"] and not (
        current_user.get("is_admin") and folder.org_id == current_user["org_id"]
    ):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Доступ запрещён"})
    files = await engine.user_file_storage.list_by_user_in_folder(folder.user_id, fuuid)
    return [FileResponse(**f.to_dict()) for f in files]
```

- [ ] **Step 2: Register in `routes/__init__.py`**

After the line `from .corrections import router as corrections_router`, add:

```python
from .folders import router as folders_router
```

Append `'folders_router'` to the `__all__` list.

- [ ] **Step 3: Include router in `app.py`**

In the imports block (around lines 26-43), add `folders_router,` to the list. In the include_router section after `app.include_router(files_router, ...)`:

```python
app.include_router(folders_router, prefix="/api/v1", tags=["folders"])
```

- [ ] **Step 4: Add `folder_id` to `FileResponse`**

In `/root/rugpt/src/engine/routes/files.py`, find the `FileResponse(BaseModel)` class. After the `cloned_from_file_id: Optional[str] = None` line, add:

```python
    folder_id: Optional[str] = None
```

- [ ] **Step 5: Smoke check**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -c "from src.engine.app import app; paths = [r.path for r in app.routes if '/folders' in r.path]; print(paths); assert '/api/v1/folders/tree' in paths"
```

Expected: list of paths printed, includes `/api/v1/folders/tree`.

- [ ] **Step 6: Commit**

```bash
cd /root/rugpt && git add src/engine/routes/folders.py src/engine/routes/__init__.py src/engine/app.py src/engine/routes/files.py && git commit -m "feat(folders): /folders router + FileResponse.folder_id"
```

### Task 11: Add `folder_id` support to `/files` endpoints

**Files:**
- Modify: `/root/rugpt/src/engine/routes/files.py`

- [ ] **Step 1: Import FolderError**

After existing imports, add:

```python
from ..services.folder_service import FolderError
```

- [ ] **Step 2: Update `upload_file` to accept `folder_id`**

In the existing function signature add a Form parameter:

```python
    folder_id: Optional[str] = Form(None, description="Target folder UUID (defaults to root)"),
```

Before the `engine.file_service.upload(...)` call, validate the folder:

```python
    folder_uuid: Optional[UUID] = None
    if folder_id:
        try:
            folder_uuid = UUID(folder_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid folder_id")
        try:
            await engine.folder_service.verify_folder_owner(
                folder_id=folder_uuid, user_id=user_uuid, org_id=current_user["org_id"],
            )
        except FolderError as e:
            raise HTTPException(
                status_code={"FOLDER_NOT_FOUND": 404, "INVALID_PARENT_OWNER": 400}.get(e.code, 400),
                detail={"code": e.code, "message": e.message},
            )
```

Pass `folder_id=folder_uuid` to the `engine.file_service.upload(...)` kwargs.

- [ ] **Step 3: Update `list_files`**

Replace the function with:

```python
@router.get("", response_model=List[FileResponse])
async def list_files(
    folder_id: Optional[str] = Query(None, description="Filter by folder. 'null'=root, UUID=specific, omit=all"),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    if folder_id is not None:
        if folder_id == "null":
            folder_uuid = None
        else:
            try:
                folder_uuid = UUID(folder_id)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid folder_id")
        files = await engine.user_file_storage.list_by_user_in_folder(current_user["user_id"], folder_uuid)
    elif current_user.get("is_admin"):
        files = await engine.file_service.list_by_org(current_user["org_id"])
    else:
        files = await engine.file_service.list_by_user(current_user["user_id"])
    return [FileResponse(**f.to_dict()) for f in files]
```

- [ ] **Step 4: Add `PATCH /files/{id}/folder` endpoint**

After the `set_file_public` function:

```python
class MoveFileBody(BaseModel):
    folder_id: Optional[str] = None


@router.patch("/{file_id}/folder", response_model=FileResponse)
async def move_file_to_folder(
    file_id: str,
    body: MoveFileBody,
    current_user: dict = Depends(get_current_user),
):
    """Move file to a different folder. body.folder_id=null → move to root."""
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid file ID")

    file_record = await engine.file_service.get(file_uuid)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    new_folder_uuid: Optional[UUID] = None
    if body.folder_id:
        try:
            new_folder_uuid = UUID(body.folder_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid folder_id")
        try:
            await engine.folder_service.verify_folder_owner(
                folder_id=new_folder_uuid,
                user_id=file_record.user_id,
                org_id=file_record.org_id,
            )
        except FolderError as e:
            raise HTTPException(
                status_code={"FOLDER_NOT_FOUND": 404, "INVALID_PARENT_OWNER": 400}.get(e.code, 400),
                detail={"code": e.code, "message": e.message},
            )

    try:
        moved = await engine.file_service.move_to_folder(
            file_id=file_uuid,
            new_folder_id=new_folder_uuid,
            actor_user_id=current_user["user_id"],
            actor_is_admin=current_user.get("is_admin", False),
            actor_org_id=current_user["org_id"],
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not moved:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(**moved.to_dict())
```

- [ ] **Step 5: Smoke check**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -c "from src.engine.app import app; paths = [r.path for r in app.routes]; assert '/api/v1/files/{file_id}/folder' in paths; print('ok')"
```

Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
cd /root/rugpt && git add src/engine/routes/files.py && git commit -m "feat(folders): /files endpoints accept folder_id + new PATCH /files/{id}/folder"
```

### Task 12: API integration tests

**Files:**
- Create: `/root/rugpt/tests/test_folders_api.py`

- [ ] **Step 1: Write the API tests**

```python
"""Smoke tests for /folders API routes using FastAPI TestClient + dependency override."""
import pytest
import pytest_asyncio
from uuid import uuid4
from fastapi.testclient import TestClient

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from src.engine.routes.auth import get_current_user
from src.engine.models.organization import Organization
from src.engine.models.user import User
from src.engine.config import Config
from src.engine.storage.org_storage import OrgStorage
from src.engine.storage.user_storage import UserStorage


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="module")
async def engine_initialized():
    engine = get_engine_service()
    await engine.initialize()
    yield engine
    await engine.close()


@pytest_asyncio.fixture
async def user_ctx(engine_initialized):
    org_s = OrgStorage(Config.get_postgres_dsn())
    user_s = UserStorage(Config.get_postgres_dsn())
    await org_s.init()
    await user_s.init()
    org = await org_s.create(Organization(name=f"o-{uuid4().hex[:6]}", slug=f"o-{uuid4().hex[:6]}"))
    u = await user_s.create(User(
        org_id=org.id, name="t", username=f"u-{uuid4().hex[:6]}",
        email=f"{uuid4().hex[:6]}@x.x", password_hash="", is_admin=False,
    ))
    client = TestClient(app)

    async def _override():
        return {"user_id": u.id, "org_id": org.id, "is_admin": False}
    app.dependency_overrides[get_current_user] = _override

    yield {"user_id": u.id, "org_id": org.id, "client": client}

    app.dependency_overrides.clear()
    await user_s.execute("DELETE FROM user_files WHERE user_id=$1", u.id)
    await user_s.execute("DELETE FROM user_file_folders WHERE user_id=$1", u.id)
    await org_s.close()
    await user_s.close()


async def test_create_then_list_then_get_tree(user_ctx):
    client = user_ctx["client"]
    r = client.post("/api/v1/folders", json={"name": "Docs"})
    assert r.status_code == 200, r.text
    folder = r.json()
    assert folder["name"] == "Docs"
    assert folder["parent_folder_id"] is None

    r = client.get("/api/v1/folders")
    assert r.status_code == 200
    assert any(f["id"] == folder["id"] for f in r.json())

    r = client.get("/api/v1/folders/tree")
    assert r.status_code == 200
    tree = r.json()
    assert any(n["id"] == folder["id"] for n in tree)


async def test_create_duplicate_name_returns_409(user_ctx):
    client = user_ctx["client"]
    client.post("/api/v1/folders", json={"name": "Dup"})
    r = client.post("/api/v1/folders", json={"name": "Dup"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DUPLICATE_NAME"


async def test_empty_name_returns_400(user_ctx):
    client = user_ctx["client"]
    r = client.post("/api/v1/folders", json={"name": "   "})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "EMPTY_NAME"


async def test_rename_and_move(user_ctx):
    client = user_ctx["client"]
    parent = client.post("/api/v1/folders", json={"name": "Parent"}).json()
    child = client.post("/api/v1/folders", json={"name": "Child", "parent_folder_id": parent["id"]}).json()

    r = client.patch(f"/api/v1/folders/{child['id']}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

    r = client.patch(f"/api/v1/folders/{child['id']}", json={"parent_folder_id": None})
    assert r.status_code == 200
    assert r.json()["parent_folder_id"] is None


async def test_cyclic_move_blocked(user_ctx):
    client = user_ctx["client"]
    a = client.post("/api/v1/folders", json={"name": "A"}).json()
    b = client.post("/api/v1/folders", json={"name": "B", "parent_folder_id": a["id"]}).json()
    r = client.patch(f"/api/v1/folders/{a['id']}", json={"parent_folder_id": b["id"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "CYCLIC_MOVE"


async def test_delete_cascade_returns_counts(user_ctx):
    client = user_ctx["client"]
    a = client.post("/api/v1/folders", json={"name": "ToDelete"}).json()
    client.post("/api/v1/folders", json={"name": "Child", "parent_folder_id": a["id"]})
    r = client.delete(f"/api/v1/folders/{a['id']}")
    assert r.status_code == 200
    out = r.json()
    assert out["success"] is True
    assert out["deleted_folders"] == 2
    assert out["deleted_files"] == 0
```

- [ ] **Step 2: Run tests**

```bash
cd /root/rugpt && /root/rugpt/venv/bin/python -m pytest tests/test_folders_api.py -v 2>&1 | tail -30
```

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
cd /root/rugpt && git add tests/test_folders_api.py && git commit -m "test(folders): API integration tests"
```

### Task 13: Manual verification on dev engine

- [ ] **Step 1: User restarts engine**

User runs locally:
```bash
cd /root/rugpt && ./local_restart.sh
```

- [ ] **Step 2: Tell user to open Swagger**

Message: "Открой `http://127.0.0.1:8100/docs#/folders` — раздел folders должен появиться с 7 endpoints. Через `POST /api/v1/folders` создай тест-папку, проверь её в `GET /api/v1/folders/tree`."

- [ ] **Step 3: No commit (manual verification)**

---

## Phase 7: WebClient — Backend (NestJS)

### Task 14: Add folder commands to `RuGPTEngineAdapter`

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`

- [ ] **Step 1: Find the `execute(command, payload)` switch**

```bash
grep -n "case 'get_files'\|case 'upload_file'\|case 'delete_file'" /root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts
```

Locate the switch block.

- [ ] **Step 2: Add folder commands**

Insert new cases. Adapt to the file's existing helper conventions (`request`, `authHeaders`, etc.) — match the style of existing `case 'get_files':` block.

```typescript
      case 'create_folder':
        return this.request('POST', '/folders' + this.maybeQuery(payload, ['user_id']), {
          name: payload.name,
          parent_folder_id: payload.parent_folder_id ?? null,
        }, this.authHeaders(payload.token));
      case 'list_folders':
        return this.request('GET', '/folders' + this.maybeQuery(payload, ['user_id']), null, this.authHeaders(payload.token));
      case 'get_folder_tree':
        return this.request('GET', '/folders/tree' + this.maybeQuery(payload, ['user_id']), null, this.authHeaders(payload.token));
      case 'get_folder':
        return this.request('GET', `/folders/${payload.id}`, null, this.authHeaders(payload.token));
      case 'update_folder': {
        const body: any = {};
        if (payload.name !== undefined) body.name = payload.name;
        if (payload.parent_folder_id !== undefined) body.parent_folder_id = payload.parent_folder_id;
        return this.request('PATCH', `/folders/${payload.id}`, body, this.authHeaders(payload.token));
      }
      case 'delete_folder':
        return this.request('DELETE', `/folders/${payload.id}`, null, this.authHeaders(payload.token));
      case 'list_folder_files':
        return this.request('GET', `/folders/${payload.id}/files`, null, this.authHeaders(payload.token));
      case 'move_file_to_folder':
        return this.request('PATCH', `/files/${payload.id}/folder`, {
          folder_id: payload.folder_id ?? null,
        }, this.authHeaders(payload.token));
```

- [ ] **Step 3: Add `maybeQuery` helper if missing**

Check if a query-builder helper exists:

```bash
grep -n "maybeQuery\|queryString" /root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts | head -3
```

If not present, add to the class (private helpers section):

```typescript
  private maybeQuery(payload: any, keys: string[]): string {
    const parts: string[] = [];
    for (const k of keys) {
      const v = payload?.[k];
      if (v !== undefined && v !== null && v !== '') {
        parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
      }
    }
    return parts.length ? '?' + parts.join('&') : '';
  }
```

- [ ] **Step 4: Update `get_files` command to forward `folder_id`**

Replace existing `case 'get_files':` with:

```typescript
      case 'get_files':
        return this.request(
          'GET',
          '/files' + this.maybeQuery(payload, ['folder_id']),
          null,
          this.authHeaders(payload.token),
        );
```

- [ ] **Step 5: Update `upload_file` to forward `folder_id` form field**

Find `case 'upload_file':`. The handler uses `payload.formData` (a FormData instance). The folder_id is appended by the controller (Task 16) before calling adapter — no change needed here. If the existing case extracts fields differently, ensure `folder_id` is preserved.

- [ ] **Step 6: Build**

```bash
cd /root/webclient_rugpt/packages/backend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 7: Commit**

```bash
cd /root/webclient_rugpt && git add packages/backend/src/engine/adapters/rugpt.adapter.ts && git commit -m "feat(folders): adapter commands for folders + folder_id in upload/list"
```

### Task 15: NestJS folder module (service + controller + module)

**Files:**
- Create: `/root/webclient_rugpt/packages/backend/src/folder/folder.service.ts`
- Create: `/root/webclient_rugpt/packages/backend/src/folder/folder.controller.ts`
- Create: `/root/webclient_rugpt/packages/backend/src/folder/folder.module.ts`
- Modify: `/root/webclient_rugpt/packages/backend/src/app.module.ts`

- [ ] **Step 1: Write `folder.service.ts`**

```typescript
import { Injectable, NotFoundException, BadRequestException, ForbiddenException, ConflictException, Inject, Logger } from '@nestjs/common';
import { ENGINE_ADAPTER, IEngineAdapter } from '../engine/engine-adapter.interface';

interface CurrentUser {
  id: string;
  email: string;
  orgId: string;
  isAdmin: boolean;
  engineToken?: string;
}

@Injectable()
export class FolderService {
  private readonly logger = new Logger(FolderService.name);

  constructor(@Inject(ENGINE_ADAPTER) private readonly engineAdapter: IEngineAdapter) {}

  async create(name: string, parentFolderId: string | null, currentUser: CurrentUser, targetUserId?: string): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('create_folder', {
      name,
      parent_folder_id: parentFolderId,
      user_id: targetUserId,
      token: currentUser.engineToken,
    });
    if (!ok) this.throwForEngineError(data);
    return this.mapFolder(data);
  }

  async findAll(currentUser: CurrentUser, targetUserId?: string): Promise<any[]> {
    const [ok, data] = await this.engineAdapter.execute('list_folders', {
      user_id: targetUserId,
      token: currentUser.engineToken,
    });
    if (!ok) return [];
    return (data || []).map((f: any) => this.mapFolder(f));
  }

  async getTree(currentUser: CurrentUser, targetUserId?: string): Promise<any[]> {
    const [ok, data] = await this.engineAdapter.execute('get_folder_tree', {
      user_id: targetUserId,
      token: currentUser.engineToken,
    });
    if (!ok) return [];
    return (data || []).map((n: any) => this.mapFolderTreeNode(n));
  }

  async findById(id: string, currentUser: CurrentUser): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('get_folder', {
      id,
      token: currentUser.engineToken,
    });
    if (!ok || !data) throw new NotFoundException('Folder not found');
    return this.mapFolder(data);
  }

  async update(id: string, body: { name?: string; parentFolderId?: string | null }, currentUser: CurrentUser): Promise<any> {
    const payload: any = { id, token: currentUser.engineToken };
    if (body.name !== undefined) payload.name = body.name;
    if (body.parentFolderId !== undefined) payload.parent_folder_id = body.parentFolderId;
    const [ok, data] = await this.engineAdapter.execute('update_folder', payload);
    if (!ok) this.throwForEngineError(data);
    return this.mapFolder(data);
  }

  async remove(id: string, currentUser: CurrentUser): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('delete_folder', {
      id,
      token: currentUser.engineToken,
    });
    if (!ok) this.throwForEngineError(data);
    return {
      success: data.success,
      deletedFolders: data.deleted_folders,
      deletedFiles: data.deleted_files,
    };
  }

  async listFiles(id: string, currentUser: CurrentUser): Promise<any[]> {
    const [ok, data] = await this.engineAdapter.execute('list_folder_files', {
      id,
      token: currentUser.engineToken,
    });
    if (!ok) return [];
    return (data || []).map((f: any) => this.mapFile(f));
  }

  private mapFolder(data: any): any {
    return {
      id: data.id,
      userId: data.user_id,
      orgId: data.org_id,
      parentFolderId: data.parent_folder_id,
      name: data.name,
      isActive: data.is_active,
      createdAt: data.created_at,
      updatedAt: data.updated_at,
    };
  }

  private mapFolderTreeNode(node: any): any {
    return {
      ...this.mapFolder(node),
      children: (node.children || []).map((c: any) => this.mapFolderTreeNode(c)),
    };
  }

  private mapFile(data: any): any {
    return {
      id: data.id,
      userId: data.user_id,
      orgId: data.org_id,
      uploadedByUserId: data.uploaded_by_user_id,
      storageKey: data.storage_key,
      originalFilename: data.original_filename,
      fileType: data.file_type,
      fileSize: data.file_size,
      ragStatus: data.rag_status,
      ragError: data.rag_error,
      indexedAt: data.indexed_at,
      isTable: data.is_table,
      isPublic: data.is_public ?? false,
      isActive: data.is_active,
      folderId: data.folder_id ?? null,
      createdAt: data.created_at,
      updatedAt: data.updated_at,
    };
  }

  private throwForEngineError(data: any): never {
    const detail = (data && (data.detail ?? data)) || null;
    const code = detail?.code;
    const message = detail?.message || (typeof data === 'string' ? data : 'Folder operation failed');
    if (code === 'DUPLICATE_NAME') throw new ConflictException({ code, message });
    if (code === 'FORBIDDEN') throw new ForbiddenException({ code, message });
    if (code === 'FOLDER_NOT_FOUND' || code === 'PARENT_NOT_FOUND') throw new NotFoundException({ code, message });
    throw new BadRequestException(code ? { code, message } : message);
  }
}
```

- [ ] **Step 2: Write `folder.controller.ts`**

```typescript
import {
  Controller, Get, Post, Patch, Delete, Param, Query, Body, UseGuards, Request,
} from '@nestjs/common';
import { FolderService } from './folder.service';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { ApiTags, ApiBearerAuth } from '@nestjs/swagger';
import { SkipSignature } from '../common/guards/signature.guard';

@ApiTags('folders')
@Controller('folders')
@UseGuards(JwtAuthGuard)
@ApiBearerAuth()
export class FolderController {
  constructor(private readonly folderService: FolderService) {}

  @Post()
  async create(
    @Body() body: { name: string; parentFolderId?: string | null },
    @Query('user_id') userId: string,
    @Request() req: any,
  ) {
    return this.folderService.create(body.name, body.parentFolderId ?? null, req.user, userId || undefined);
  }

  @SkipSignature()
  @Get()
  async findAll(@Query('user_id') userId: string, @Request() req: any) {
    return this.folderService.findAll(req.user, userId || undefined);
  }

  @SkipSignature()
  @Get('tree')
  async getTree(@Query('user_id') userId: string, @Request() req: any) {
    return this.folderService.getTree(req.user, userId || undefined);
  }

  @SkipSignature()
  @Get(':id')
  async findById(@Param('id') id: string, @Request() req: any) {
    return this.folderService.findById(id, req.user);
  }

  @Patch(':id')
  async update(
    @Param('id') id: string,
    @Body() body: { name?: string; parentFolderId?: string | null },
    @Request() req: any,
  ) {
    return this.folderService.update(id, body, req.user);
  }

  @Delete(':id')
  async remove(@Param('id') id: string, @Request() req: any) {
    return this.folderService.remove(id, req.user);
  }

  @SkipSignature()
  @Get(':id/files')
  async listFiles(@Param('id') id: string, @Request() req: any) {
    return this.folderService.listFiles(id, req.user);
  }
}
```

- [ ] **Step 3: Write `folder.module.ts`**

```typescript
import { Module } from '@nestjs/common';
import { FolderController } from './folder.controller';
import { FolderService } from './folder.service';
import { EngineModule } from '../engine/engine.module';

@Module({
  imports: [EngineModule],
  controllers: [FolderController],
  providers: [FolderService],
  exports: [FolderService],
})
export class FolderModule {}
```

- [ ] **Step 4: Register in `app.module.ts`**

Add import (alphabetically near FileModule):

```typescript
import { FolderModule } from './folder/folder.module';
```

Add to the `imports: [...]` array, near `FileModule`:

```typescript
    FolderModule,
```

- [ ] **Step 5: Build**

```bash
cd /root/webclient_rugpt/packages/backend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 6: Commit**

```bash
cd /root/webclient_rugpt && git add packages/backend/src/folder/ packages/backend/src/app.module.ts && git commit -m "feat(folders): NestJS folder module"
```

### Task 16: Extend `FileService` in webclient with folder support

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/file/file.service.ts`
- Modify: `/root/webclient_rugpt/packages/backend/src/file/file.controller.ts`

- [ ] **Step 1: Update `mapFile`**

In `file.service.ts`, find `private mapFile(data: any)`. After `isPublic: data.is_public ?? false,` add:

```typescript
      folderId: data.folder_id ?? null,
```

- [ ] **Step 2: Update `upload()` signature**

Change to:

```typescript
  async upload(file: Express.Multer.File, userId: string | undefined, folderId: string | undefined, currentUser: CurrentUser): Promise<any> {
```

Inside, after the `if (userId) { formData.append('user_id', userId); }` block, add:

```typescript
    if (folderId) {
      formData.append('folder_id', folderId);
    }
```

- [ ] **Step 3: Update `findAll`**

Replace with:

```typescript
  async findAll(folderId: string | undefined, currentUser: CurrentUser): Promise<any[]> {
    const [success, data] = await this.engineAdapter.execute('get_files', {
      folder_id: folderId,
      token: currentUser.engineToken,
    });
    if (!success) return [];
    return (data || []).map((f: any) => this.mapFile(f));
  }
```

- [ ] **Step 4: Add `moveToFolder`**

Append:

```typescript
  async moveToFolder(id: string, folderId: string | null, currentUser: CurrentUser): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('move_file_to_folder', {
      id,
      folder_id: folderId,
      token: currentUser.engineToken,
    });
    if (!ok) {
      const detail = (data && (data.detail ?? data)) || null;
      const message = detail?.message || (typeof data === 'string' ? data : 'Failed to move file');
      throw new Error(message);
    }
    return this.mapFile(data);
  }
```

- [ ] **Step 5: Update controller — upload handler**

Change to:

```typescript
  @SkipSignature()
  @Post('upload')
  @UseInterceptors(FileInterceptor('file', { limits: { fileSize: MAX_UPLOAD_BYTES } }))
  async upload(
    @UploadedFile() file: Express.Multer.File,
    @Query('user_id') userId: string,
    @Query('folder_id') folderId: string,
    @Request() req: any,
  ) {
    return this.fileService.upload(file, userId || undefined, folderId || undefined, req.user);
  }
```

- [ ] **Step 6: Update controller — findAll handler**

Change to:

```typescript
  @Get()
  async findAll(@Query('folder_id') folderId: string, @Request() req: any) {
    return this.fileService.findAll(folderId || undefined, req.user);
  }
```

- [ ] **Step 7: Add `moveToFolder` endpoint to controller**

After the existing `@Delete(':id')` method:

```typescript
  @Patch(':id/folder')
  async moveToFolder(
    @Param('id') id: string,
    @Body('folderId') folderId: string | null,
    @Request() req: any,
  ) {
    return this.fileService.moveToFolder(id, folderId ?? null, req.user);
  }
```

- [ ] **Step 8: Build**

```bash
cd /root/webclient_rugpt/packages/backend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 9: Commit**

```bash
cd /root/webclient_rugpt && git add packages/backend/src/file/ && git commit -m "feat(folders): FileService.upload/findAll accept folderId, add moveToFolder"
```

### Task 17: Shared types

**Files:**
- Modify: existing File type in `packages/common/src/types/`
- Create: `/root/webclient_rugpt/packages/common/src/types/folder.ts`

- [ ] **Step 1: Find File type location**

```bash
grep -rln "originalFilename" /root/webclient_rugpt/packages/common/src --include="*.ts" 2>&1 | head -3
```

Open the file containing the File interface.

- [ ] **Step 2: Add `folderId: string | null` to File interface**

Find the interface declaring `originalFilename`, `fileType`, etc. Add:

```typescript
  folderId: string | null;
```

- [ ] **Step 3: Create folder types**

Create `/root/webclient_rugpt/packages/common/src/types/folder.ts`:

```typescript
export interface Folder {
  id: string;
  userId: string;
  orgId: string;
  parentFolderId: string | null;
  name: string;
  isActive: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface FolderTreeNode extends Folder {
  children: FolderTreeNode[];
}
```

- [ ] **Step 4: Export from barrel**

If `/root/webclient_rugpt/packages/common/src/types/index.ts` exists, append:

```typescript
export * from './folder';
```

If no barrel — check `packages/common/src/index.ts` instead and add export from there.

- [ ] **Step 5: Build common**

```bash
cd /root/webclient_rugpt/packages/common && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 6: Commit**

```bash
cd /root/webclient_rugpt && git add packages/common/src && git commit -m "feat(folders): shared Folder + FolderTreeNode types + File.folderId"
```

---

## Phase 8: WebClient — Frontend (Next.js)

### Task 18: Probe frontend hooks/components paths

- [ ] **Step 1: Locate existing useFiles hook**

```bash
grep -rln "useFiles" /root/webclient_rugpt/packages/frontend/src --include="*.ts" --include="*.tsx" 2>&1 | grep -v node_modules | head -5
```

Note the directory. The new `useFolders` hook will go in the same directory.

- [ ] **Step 2: Locate components directory**

```bash
ls -d /root/webclient_rugpt/packages/frontend/src/app/components/ 2>&1 || find /root/webclient_rugpt/packages/frontend/src -type d -name "components" -not -path "*/node_modules/*" 2>&1 | head -3
```

Note the path. New components (FolderTree, modals) will go here.

- [ ] **Step 3: No commit (discovery only)**

### Task 19: `useFolders` hook

**Files:**
- Create: `<hooks_dir>/useFolders.ts` (path from Task 18 Step 1)

- [ ] **Step 1: Inspect apiClient API in existing useFiles**

```bash
grep -n "apiClient\." <path-from-task-18>/useFiles.ts | head -10
```

Note which methods exist (`get`, `signedPost`, `signedPatch`, `signedDelete`, etc.).

- [ ] **Step 2: Write useFolders.ts using same conventions**

Adapt the following template to match the apiClient method names found in Step 1. Place in the hooks dir:

```typescript
import { useCallback, useEffect, useState } from 'react';
import type { Folder, FolderTreeNode } from '@webchat/common';
// Adjust import path to match useFiles
import { apiClient } from '<same-path-as-useFiles>';

export function useFolders() {
  const [tree, setTree] = useState<FolderTreeNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchTree = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiClient.get<FolderTreeNode[]>('/folders/tree');
      setTree(data);
    } catch (e: any) {
      setError(e?.message || 'Failed to load folders');
    } finally {
      setLoading(false);
    }
  }, []);

  const createFolder = useCallback(async (name: string, parentFolderId: string | null) => {
    const created = await apiClient.signedPost<Folder>('/folders', { name, parentFolderId });
    await fetchTree();
    return created;
  }, [fetchTree]);

  const renameFolder = useCallback(async (id: string, name: string) => {
    const updated = await apiClient.signedPatch<Folder>(`/folders/${id}`, { name });
    await fetchTree();
    return updated;
  }, [fetchTree]);

  const moveFolder = useCallback(async (id: string, parentFolderId: string | null) => {
    const updated = await apiClient.signedPatch<Folder>(`/folders/${id}`, { parentFolderId });
    await fetchTree();
    return updated;
  }, [fetchTree]);

  const deleteFolder = useCallback(async (id: string) => {
    const out = await apiClient.signedDelete<{ success: boolean; deletedFolders: number; deletedFiles: number }>(`/folders/${id}`);
    await fetchTree();
    return out;
  }, [fetchTree]);

  useEffect(() => { void fetchTree(); }, [fetchTree]);

  return { tree, loading, error, fetchTree, createFolder, renameFolder, moveFolder, deleteFolder };
}
```

- [ ] **Step 3: Build frontend to verify imports**

```bash
cd /root/webclient_rugpt/packages/frontend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 4: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src && git commit -m "feat(folders): useFolders hook"
```

### Task 20: Extend `useFiles` with folder support

**Files:**
- Modify: `<hooks_dir>/useFiles.ts`

- [ ] **Step 1: Read existing useFiles signature**

```bash
cat <path-from-task-18>/useFiles.ts
```

- [ ] **Step 2: Add `folderId` to fetchFiles**

Change `fetchFiles` signature to accept an optional argument `{ folderId?: string | null }`. Pass `?folder_id=<value>` to the API call:
- `folderId === null` → `?folder_id=null`
- `folderId === string` → `?folder_id=<uuid>`
- `folderId === undefined` → no query (all files)

- [ ] **Step 3: Add `folderId` to `uploadFile`**

Add as last optional arg. When set, append to FormData:

```typescript
formData.append('folder_id', folderId);
```

- [ ] **Step 4: Add `moveFileToFolder` method**

```typescript
const moveFileToFolder = useCallback(async (fileId: string, folderId: string | null) => {
  const updated = await apiClient.signedPatch(`/files/${fileId}/folder`, { folderId });
  await fetchFiles();
  return updated;
}, [fetchFiles]);
```

Return it from the hook.

- [ ] **Step 5: Build**

```bash
cd /root/webclient_rugpt/packages/frontend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 6: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src && git commit -m "feat(folders): useFiles supports folderId + moveFileToFolder"
```

### Task 21: FolderTree component

**Files:**
- Create: `<components_dir>/FolderTree.tsx` (path from Task 18 Step 2)

- [ ] **Step 1: Write the component**

```tsx
'use client';

import { useState } from 'react';
import type { FolderTreeNode } from '@webchat/common';

interface Props {
  tree: FolderTreeNode[];
  selectedFolderId: string | null;
  onSelect: (folderId: string | null) => void;
  onContextMenu?: (folder: FolderTreeNode, e: React.MouseEvent) => void;
}

function TreeNode({ node, depth, selectedFolderId, onSelect, onContextMenu }: {
  node: FolderTreeNode;
  depth: number;
  selectedFolderId: string | null;
  onSelect: (folderId: string | null) => void;
  onContextMenu?: (folder: FolderTreeNode, e: React.MouseEvent) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const isSelected = selectedFolderId === node.id;
  return (
    <div>
      <div
        className={`flex items-center gap-1 py-1 px-2 cursor-pointer rounded transition-colors
                    ${isSelected ? 'bg-blue-100 dark:bg-blue-900' : 'hover:bg-gray-100 dark:hover:bg-gray-800'}`}
        style={{ paddingLeft: `${depth * 12 + 8}px` }}
        onClick={() => onSelect(node.id)}
        onContextMenu={(e) => {
          if (onContextMenu) {
            e.preventDefault();
            onContextMenu(node, e);
          }
        }}
      >
        {node.children.length > 0 ? (
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            className="w-4 h-4 flex-shrink-0"
          >
            {expanded ? '▼' : '▶'}
          </button>
        ) : (
          <span className="w-4 h-4 flex-shrink-0" />
        )}
        <span className="mr-1">📁</span>
        <span className="truncate">{node.name}</span>
      </div>
      {expanded && node.children.length > 0 && (
        <div>
          {node.children.map((c) => (
            <TreeNode
              key={c.id}
              node={c}
              depth={depth + 1}
              selectedFolderId={selectedFolderId}
              onSelect={onSelect}
              onContextMenu={onContextMenu}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function FolderTree({ tree, selectedFolderId, onSelect, onContextMenu }: Props) {
  return (
    <div className="text-sm">
      <div
        className={`flex items-center gap-1 py-1 px-2 cursor-pointer rounded transition-colors
                    ${selectedFolderId === null ? 'bg-blue-100 dark:bg-blue-900' : 'hover:bg-gray-100 dark:hover:bg-gray-800'}`}
        onClick={() => onSelect(null)}
      >
        <span className="w-4 h-4 flex-shrink-0" />
        <span className="mr-1">🏠</span>
        <span>Все файлы</span>
      </div>
      {tree.map((root) => (
        <TreeNode
          key={root.id}
          node={root}
          depth={0}
          selectedFolderId={selectedFolderId}
          onSelect={onSelect}
          onContextMenu={onContextMenu}
        />
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src && git commit -m "feat(folders): FolderTree component"
```

### Task 22: Modal components

**Files:**
- Create: `<components_dir>/CreateFolderModal.tsx`
- Create: `<components_dir>/RenameFolderModal.tsx`
- Create: `<components_dir>/MoveModal.tsx`

- [ ] **Step 1: Write `CreateFolderModal.tsx`**

```tsx
'use client';

import { useState } from 'react';

interface Props {
  parentFolderId: string | null;
  onSubmit: (name: string, parentFolderId: string | null) => Promise<void>;
  onClose: () => void;
}

export function CreateFolderModal({ parentFolderId, onSubmit, onClose }: Props) {
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    if (!name.trim()) {
      setError('Имя папки не может быть пустым');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(name.trim(), parentFolderId);
      onClose();
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.message || 'Ошибка создания папки';
      setError(typeof detail === 'string' ? detail : detail?.message || 'Ошибка');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-900 rounded-lg p-6 w-96 shadow-xl">
        <h3 className="text-lg font-semibold mb-4">Новая папка</h3>
        <input
          autoFocus
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void handleSubmit(); }}
          placeholder="Название папки"
          className="w-full border rounded px-3 py-2 dark:bg-gray-800 dark:border-gray-700"
        />
        {error && <p className="text-red-500 text-sm mt-2">{error}</p>}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-4 py-2 rounded hover:bg-gray-100 dark:hover:bg-gray-800">Отмена</button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-4 py-2 rounded"
          >
            Создать
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Write `RenameFolderModal.tsx`**

```tsx
'use client';

import { useState } from 'react';

interface Props {
  initialName: string;
  onSubmit: (name: string) => Promise<void>;
  onClose: () => void;
}

export function RenameFolderModal({ initialName, onSubmit, onClose }: Props) {
  const [name, setName] = useState(initialName);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    if (!name.trim()) {
      setError('Имя папки не может быть пустым');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(name.trim());
      onClose();
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.message || 'Ошибка переименования';
      setError(typeof detail === 'string' ? detail : detail?.message || 'Ошибка');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-900 rounded-lg p-6 w-96 shadow-xl">
        <h3 className="text-lg font-semibold mb-4">Переименовать папку</h3>
        <input
          autoFocus
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void handleSubmit(); }}
          className="w-full border rounded px-3 py-2 dark:bg-gray-800 dark:border-gray-700"
        />
        {error && <p className="text-red-500 text-sm mt-2">{error}</p>}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-4 py-2 rounded hover:bg-gray-100 dark:hover:bg-gray-800">Отмена</button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-4 py-2 rounded"
          >
            Сохранить
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Write `MoveModal.tsx`**

```tsx
'use client';

import { useState } from 'react';
import type { FolderTreeNode } from '@webchat/common';
import { FolderTree } from './FolderTree';

interface Props {
  tree: FolderTreeNode[];
  /** Folder being moved — its subtree must be excluded as invalid destinations. null if moving a file. */
  movingFolderId?: string | null;
  onSubmit: (newParentFolderId: string | null) => Promise<void>;
  onClose: () => void;
}

function collectDescendants(tree: FolderTreeNode[], targetId: string): Set<string> {
  const result = new Set<string>();
  function walk(nodes: FolderTreeNode[], inside: boolean) {
    for (const n of nodes) {
      const isTargetSubtree = inside || n.id === targetId;
      if (isTargetSubtree) result.add(n.id);
      walk(n.children, isTargetSubtree);
    }
  }
  walk(tree, false);
  return result;
}

function filterTree(tree: FolderTreeNode[], forbidden: Set<string>): FolderTreeNode[] {
  return tree
    .filter((n) => !forbidden.has(n.id))
    .map((n) => ({ ...n, children: filterTree(n.children, forbidden) }));
}

export function MoveModal({ tree, movingFolderId, onSubmit, onClose }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const forbidden = movingFolderId ? collectDescendants(tree, movingFolderId) : new Set<string>();
  const allowedTree = filterTree(tree, forbidden);

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(selected);
      onClose();
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      setError(detail?.message || e?.message || 'Ошибка перемещения');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-900 rounded-lg p-6 w-[500px] max-h-[80vh] flex flex-col shadow-xl">
        <h3 className="text-lg font-semibold mb-4">Выберите папку назначения</h3>
        <div className="flex-1 overflow-auto border rounded p-2 dark:border-gray-700">
          <FolderTree
            tree={allowedTree}
            selectedFolderId={selected}
            onSelect={setSelected}
          />
        </div>
        {error && <p className="text-red-500 text-sm mt-2">{error}</p>}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-4 py-2 rounded hover:bg-gray-100 dark:hover:bg-gray-800">Отмена</button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-4 py-2 rounded"
          >
            Переместить
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src && git commit -m "feat(folders): Create/Rename/Move modals"
```

### Task 23: Rewrite `/files` page

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/files/page.tsx`

- [ ] **Step 1: Read existing page**

```bash
cat /root/webclient_rugpt/packages/frontend/src/app/files/page.tsx
```

- [ ] **Step 2: Replace with folder-aware page**

Adapt imports to match actual paths discovered in Task 18. Replace the page contents with:

```tsx
'use client';

import { useState, useEffect, useRef, useMemo } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
// Adjust import paths to match where Tasks 18-22 placed files:
import { useFolders } from '@/<hooks-dir>/useFolders';
import { useFiles } from '@/<hooks-dir>/useFiles';
import { FolderTree } from '@/<components-dir>/FolderTree';
import { CreateFolderModal } from '@/<components-dir>/CreateFolderModal';
import { RenameFolderModal } from '@/<components-dir>/RenameFolderModal';
import { MoveModal } from '@/<components-dir>/MoveModal';
import type { FolderTreeNode } from '@webchat/common';

export default function FilesPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const currentFolderId = searchParams.get('folder');

  const { tree, createFolder, renameFolder, moveFolder, deleteFolder } = useFolders();
  const { files, fetchFiles, uploadFile, deleteFile, moveFileToFolder } = useFiles();

  const [showCreate, setShowCreate] = useState(false);
  const [renameTarget, setRenameTarget] = useState<FolderTreeNode | null>(null);
  const [moveTarget, setMoveTarget] = useState<{ type: 'folder' | 'file'; id: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void fetchFiles({ folderId: currentFolderId });
  }, [currentFolderId, fetchFiles]);

  const breadcrumb = useMemo<FolderTreeNode[]>(() => {
    if (!currentFolderId) return [];
    function find(nodes: FolderTreeNode[], path: FolderTreeNode[]): FolderTreeNode[] | null {
      for (const n of nodes) {
        const next = [...path, n];
        if (n.id === currentFolderId) return next;
        const r = find(n.children, next);
        if (r) return r;
      }
      return null;
    }
    return find(tree, []) || [];
  }, [tree, currentFolderId]);

  const setFolder = (id: string | null) => {
    if (id === null) router.push('/files');
    else router.push(`/files?folder=${id}`);
  };

  const subfoldersOfCurrent = useMemo<FolderTreeNode[]>(() => {
    if (!currentFolderId) return tree;
    function find(nodes: FolderTreeNode[]): FolderTreeNode | null {
      for (const n of nodes) {
        if (n.id === currentFolderId) return n;
        const r = find(n.children);
        if (r) return r;
      }
      return null;
    }
    return find(tree)?.children || [];
  }, [tree, currentFolderId]);

  const handleFolderContext = (folder: FolderTreeNode) => {
    // MVP: prompt-based menu. Replaced by proper popover in TD-FOLDERS-CTX.
    const action = window.prompt(`Папка "${folder.name}": введите rename, move или delete`, '');
    if (!action) return;
    if (action === 'rename') setRenameTarget(folder);
    else if (action === 'move') setMoveTarget({ type: 'folder', id: folder.id });
    else if (action === 'delete') {
      if (window.confirm(`Удалить папку "${folder.name}" со всем содержимым?`)) {
        void deleteFolder(folder.id);
      }
    }
  };

  return (
    <div className="flex h-screen">
      <aside className="w-64 border-r dark:border-gray-700 overflow-auto p-2">
        <div className="flex items-center justify-between mb-2">
          <h2 className="font-semibold">Папки</h2>
          <button
            onClick={() => setShowCreate(true)}
            className="text-blue-600 hover:underline text-sm"
          >
            + Папка
          </button>
        </div>
        <FolderTree
          tree={tree}
          selectedFolderId={currentFolderId}
          onSelect={setFolder}
          onContextMenu={(folder) => handleFolderContext(folder)}
        />
      </aside>

      <main className="flex-1 overflow-auto">
        <div className="border-b dark:border-gray-700 px-4 py-2 text-sm flex items-center gap-1">
          <button onClick={() => setFolder(null)} className="hover:underline">Все файлы</button>
          {breadcrumb.map((node) => (
            <span key={node.id} className="flex items-center gap-1">
              <span className="text-gray-400">/</span>
              <button onClick={() => setFolder(node.id)} className="hover:underline">{node.name}</button>
            </span>
          ))}
        </div>

        <div className="p-4 flex gap-2">
          <input
            ref={inputRef}
            type="file"
            className="hidden"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              await uploadFile(f, undefined, currentFolderId);
              e.target.value = '';
            }}
          />
          <button
            onClick={() => inputRef.current?.click()}
            className="bg-blue-600 hover:bg-blue-700 text-white px-3 py-1 rounded"
          >
            + Загрузить файл
          </button>
        </div>

        <div className="px-4 pb-8">
          {subfoldersOfCurrent.map((sub) => (
            <div
              key={sub.id}
              onClick={() => setFolder(sub.id)}
              onContextMenu={(e) => { e.preventDefault(); handleFolderContext(sub); }}
              className="flex items-center gap-2 p-2 hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer rounded"
            >
              <span>📁</span>
              <span>{sub.name}</span>
            </div>
          ))}
          {files.map((f: any) => (
            <div
              key={f.id}
              className="flex items-center justify-between p-2 hover:bg-gray-50 dark:hover:bg-gray-800 rounded"
            >
              <div className="flex items-center gap-2">
                <span>📄</span>
                <span>{f.originalFilename}</span>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => setMoveTarget({ type: 'file', id: f.id })}
                  className="text-sm text-blue-600 hover:underline"
                >
                  Переместить
                </button>
                <button
                  onClick={() => { if (window.confirm('Удалить файл?')) void deleteFile(f.id); }}
                  className="text-sm text-red-600 hover:underline"
                >
                  Удалить
                </button>
              </div>
            </div>
          ))}
          {files.length === 0 && subfoldersOfCurrent.length === 0 && (
            <p className="text-gray-500 text-center py-8">Пусто</p>
          )}
        </div>
      </main>

      {showCreate && (
        <CreateFolderModal
          parentFolderId={currentFolderId}
          onSubmit={async (name, parent) => { await createFolder(name, parent); }}
          onClose={() => setShowCreate(false)}
        />
      )}
      {renameTarget && (
        <RenameFolderModal
          initialName={renameTarget.name}
          onSubmit={async (name) => { await renameFolder(renameTarget.id, name); }}
          onClose={() => setRenameTarget(null)}
        />
      )}
      {moveTarget && (
        <MoveModal
          tree={tree}
          movingFolderId={moveTarget.type === 'folder' ? moveTarget.id : null}
          onSubmit={async (newParentFolderId) => {
            if (moveTarget.type === 'folder') {
              await moveFolder(moveTarget.id, newParentFolderId);
            } else {
              await moveFileToFolder(moveTarget.id, newParentFolderId);
            }
          }}
          onClose={() => setMoveTarget(null)}
        />
      )}
    </div>
  );
}
```

NOTE: the `<hooks-dir>` and `<components-dir>` placeholders in import paths must be replaced with actual values from Task 18.

- [ ] **Step 3: Build**

```bash
cd /root/webclient_rugpt/packages/frontend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 4: Tell user to run dev + manually verify**

Message: "Запусти `cd /root/webclient_rugpt && ./dev.sh` или `cd packages/frontend && npm run dev`. Открой `/files`, проверь golden path: создать папку → загрузить файл в неё → переместить файл в корень → удалить папку с подпапками."

- [ ] **Step 5: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src/app/files/page.tsx && git commit -m "feat(folders): rewrite /files page with folder tree + breadcrumb"
```

---

## Phase 9: Documentation

### Task 24: Tech debt entries

**Files:**
- Modify: `/root/rugpt/docs/tech-debt.md`
- Modify: `/root/webclient_rugpt/doc/TECH_DEBT.md`

- [ ] **Step 1: Append engine tech-debt section**

Read existing file first to understand format and append at the bottom of `/root/rugpt/docs/tech-debt.md`:

```markdown

## Файловые папки — отложенные улучшения (2026-05-12)

Базовая фича папок выкачена без интеграции с агентами. Идеи:

| ID | Идея | Зачем | Оценка |
|---|---|---|---|
| TD-FOLDERS-1 | folder path в `list_documents` (engine tool) | LLM видит `/Договоры/2024/НДА.pdf`. Лучший контекст | ~30 строк: batch path-resolve + format-строка |
| TD-FOLDERS-2 | Фильтр `list_documents` по `folder_id` | LLM сужает поиск «в папке X» | ~50 строк: optional param + `folder_storage.list_subtree_ids` |
| TD-FOLDERS-3 | Новый tool `get_directory_tree` | LLM получает ASCII-карту дерева юзера | ~50 строк: новый `tools/get_directory_tree.py` + ToolRegistry |
| TD-FOLDERS-4 | `rag_search` scoped to folder | Семантический поиск ограничен subtree. Риск регрессии RAG | ~80 строк pre-filter в Python, ~150 при SQL-уровне |
| TD-FOLDERS-7 | Восстановление soft-deleted папок и файлов | «Trash bin» с undo | Reactivate service-методы + UI `/trash` |
| TD-FOLDERS-8 | Periodic cleanup orphan storage bytes | Cron-job удаляет физические файлы `is_active=false older than 30d` | Scheduler-task |
| TD-FOLDERS-9 | Public/shared org folders | Папки видимые всем org. Требует отдельного дизайна | Полноценная фича — новый brainstorming |
| TD-FOLDERS-10 | Folder reordering | Кастомный порядок (`order_index`) | Migration + drag handle |

Spec: `docs/superpowers/specs/2026-05-12-file-folders-design.md`. Plan: `docs/superpowers/plans/2026-05-12-file-folders.md`.
```

- [ ] **Step 2: Append webclient tech-debt section**

Append to `/root/webclient_rugpt/doc/TECH_DEBT.md`:

```markdown

## Файловые папки — отложенные улучшения (2026-05-12)

WebClient-аспекты:

| ID | Идея | Зачем |
|---|---|---|
| TD-FOLDERS-5 | Drag-n-drop файла на папку в дереве | Лучше UX vs MoveModal |
| TD-FOLDERS-6 | Bulk operations | Multi-select + bulk move/delete |
| TD-FOLDERS-CTX | Полноценное контекстное меню для папки в дереве | Сейчас prompt() как заглушка |
| TD-FOLDERS-OPT | Оптимистичные апдейты useFolders | Сейчас refetch tree после каждой операции |

Engine-side доработки — в `/root/rugpt/docs/tech-debt.md` секция «Файловые папки — отложенные улучшения». Spec: `/root/rugpt/docs/superpowers/specs/2026-05-12-file-folders-design.md`.
```

- [ ] **Step 3: Commit both**

```bash
cd /root/rugpt && git add docs/tech-debt.md && git commit -m "docs(folders): tech-debt entries for deferred tools + features"
cd /root/webclient_rugpt && git add doc/TECH_DEBT.md && git commit -m "docs(folders): webclient tech-debt entries"
```

### Task 25: Update engine API docs

**Files:**
- Modify: `/root/rugpt/docs/api.md`

- [ ] **Step 1: Append `/folders/*` section**

Add to `docs/api.md` after the Files section (or in alphabetical order with other groups):

```markdown
### Folders (`/api/v1/folders`)
| Endpoint | Описание |
|----------|----------|
| `POST /` | Создать папку (body `{name, parent_folder_id?}`) |
| `GET /` | Плоский список папок юзера |
| `GET /tree` | Уже собранное дерево |
| `GET /{id}` | Метаданные папки |
| `PATCH /{id}` | Rename и/или move (body `{name?, parent_folder_id?}`; `parent_folder_id: null` → в корень) |
| `DELETE /{id}` | Каскадный soft-delete (response `{success, deleted_folders, deleted_files}`) |
| `GET /{id}/files` | Файлы непосредственно в папке |

### Files (`/api/v1/files`) — обновления

- `POST /upload` принимает form-поле `folder_id` (опционально)
- `GET /` принимает query `?folder_id=` (`null` для корня, UUID, либо опустить для всего)
- `PATCH /{file_id}/folder` — переместить файл (body `{folder_id: UUID | null}`)

### Folder errors

Все 4xx ошибки folder-операций возвращают `detail: {code, message}`:

| code | status |
|---|---|
| EMPTY_NAME | 400 |
| NAME_TOO_LONG | 400 |
| MAX_DEPTH_EXCEEDED | 400 |
| CYCLIC_MOVE | 400 |
| INVALID_PARENT_OWNER | 400 |
| FORBIDDEN | 403 |
| FOLDER_NOT_FOUND | 404 |
| PARENT_NOT_FOUND | 404 |
| DUPLICATE_NAME | 409 |
```

- [ ] **Step 2: Commit**

```bash
cd /root/rugpt && git add docs/api.md && git commit -m "docs(folders): document /folders endpoints + error codes"
```

---

## Self-Review Checklist

Before signalling completion:

- [ ] Migration 037 applied (`SELECT * FROM schema_migrations WHERE filename = '037_user_file_folders.sql'` returns row)
- [ ] All engine storage tests pass (`pytest tests/test_user_file_folder_storage.py tests/test_user_file_storage_folders.py -v`)
- [ ] All engine service tests pass (`pytest tests/test_folder_service.py -v`)
- [ ] All engine API tests pass (`pytest tests/test_folders_api.py -v`)
- [ ] Engine boots without errors (`./local_restart.sh` + `curl -s http://127.0.0.1:8100/api/v1/folders` returns 401 — auth required, route exists)
- [ ] WebClient backend builds (`cd packages/backend && npm run build`)
- [ ] WebClient frontend builds (`cd packages/frontend && npm run build`)
- [ ] Manual golden path in browser: create folder → upload file → move file → delete cascade
- [ ] All 8 error codes return expected status (probe via TestClient or curl)
- [ ] Tech-debt entries are in both repos
- [ ] API docs updated in `/root/rugpt/docs/api.md`
