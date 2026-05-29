"""
Memory Snapshot Storage

PostgreSQL storage for memory snapshots.
"""

from src.engine.unified_logger import get_logger
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.memory_snapshot import MemorySnapshot

logger = get_logger("storage")

class MemorySnapshotStorage(BaseStorage):
    """Storage for MemorySnapshot entities"""

    async def create(self, snapshot: MemorySnapshot) -> MemorySnapshot:
        """Create a new memory snapshot"""
        row = await self.fetchrow(
            """
            INSERT INTO memory_snapshots (id, snapshot, is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            snapshot.id, snapshot.snapshot, snapshot.is_active,
            snapshot.created_at, snapshot.updated_at,
        )
        return self._row_to_snapshot(row)

    async def get_by_id(self, snapshot_id: UUID) -> Optional[MemorySnapshot]:
        """Get memory snapshot by ID"""
        row = await self.fetchrow(
            "SELECT * FROM memory_snapshots WHERE id = $1", snapshot_id
        )
        return self._row_to_snapshot(row) if row else None

    async def list_active(self) -> List[MemorySnapshot]:
        """List all active memory snapshots"""
        rows = await self.fetch(
            "SELECT * FROM memory_snapshots WHERE is_active = true ORDER BY created_at DESC"
        )
        return [self._row_to_snapshot(row) for row in rows]

    async def update_snapshot(self, snapshot_id: UUID, snapshot_text: str) -> Optional[MemorySnapshot]:
        """Update the snapshot text (trigger updates updated_at)"""
        row = await self.fetchrow(
            """
            UPDATE memory_snapshots
            SET snapshot = $2
            WHERE id = $1
            RETURNING *
            """,
            snapshot_id, snapshot_text,
        )
        return self._row_to_snapshot(row) if row else None

    async def deactivate(self, snapshot_id: UUID) -> bool:
        """Deactivate a memory snapshot"""
        result = await self.execute(
            "UPDATE memory_snapshots SET is_active = false WHERE id = $1", snapshot_id
        )
        return "UPDATE 1" in result

    async def delete(self, snapshot_id: UUID) -> bool:
        """Hard delete a memory snapshot"""
        result = await self.execute(
            "DELETE FROM memory_snapshots WHERE id = $1", snapshot_id
        )
        return "DELETE 1" in result

    def _row_to_snapshot(self, row) -> MemorySnapshot:
        """Convert database row to MemorySnapshot"""
        return MemorySnapshot(
            id=row["id"],
            snapshot=row["snapshot"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
