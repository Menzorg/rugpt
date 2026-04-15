"""
Agent Run Storage

PostgreSQL CRUD for agent_runs table (item 10 / async agent execution idempotency).
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from .base import BaseStorage
from ..models.agent_run import AgentRun

logger = logging.getLogger("rugpt.storage.agent_run")


class AgentRunStorage(BaseStorage):

    async def create(self, run: AgentRun) -> AgentRun:
        query = """
            INSERT INTO agent_runs (
                request_id, chat_id, user_message_id, triggering_user_id,
                role_code, status, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            run.request_id, run.chat_id, run.user_message_id, run.triggering_user_id,
            run.role_code, run.status, run.created_at,
        )
        return self._row_to_run(row)

    async def get(self, request_id: UUID) -> Optional[AgentRun]:
        row = await self.fetchrow(
            "SELECT * FROM agent_runs WHERE request_id = $1", request_id,
        )
        return self._row_to_run(row) if row else None

    async def mark_running(self, request_id: UUID) -> bool:
        """
        Atomic CAS pending -> running. Returns True if this caller won the race
        and must actually execute the agent. Returns False if the run is already
        running/done/failed (Kafka redelivery of an already-handled message).
        """
        row = await self.fetchrow(
            """
            UPDATE agent_runs
               SET status = 'running', started_at = NOW()
             WHERE request_id = $1 AND status = 'pending'
            RETURNING request_id
            """,
            request_id,
        )
        return row is not None

    async def mark_done(self, request_id: UUID, result_message_id: UUID) -> None:
        await self.execute(
            """
            UPDATE agent_runs
               SET status = 'done',
                   result_message_id = $2,
                   finished_at = NOW()
             WHERE request_id = $1
            """,
            request_id, result_message_id,
        )

    async def mark_failed(self, request_id: UUID, error: str) -> None:
        await self.execute(
            """
            UPDATE agent_runs
               SET status = 'failed',
                   error_message = $2,
                   finished_at = NOW()
             WHERE request_id = $1
            """,
            request_id, error[:2000] if error else None,
        )

    def _row_to_run(self, row) -> AgentRun:
        return AgentRun(
            request_id=row["request_id"],
            chat_id=row["chat_id"],
            user_message_id=row["user_message_id"],
            triggering_user_id=row["triggering_user_id"],
            role_code=row["role_code"],
            status=row["status"],
            result_message_id=row["result_message_id"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
