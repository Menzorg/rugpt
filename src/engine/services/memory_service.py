"""
Memory Service

Manages chat memory snapshots: summarising message history via LLM and
deciding when a re-summarisation is needed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING
from uuid import UUID

from ..config import Config
from ..models.memory_snapshot import MemorySnapshot
from ..storage.chat_storage import ChatStorage
from ..storage.memory_snapshot_storage import MemorySnapshotStorage
from ..storage.message_storage import MessageStorage

if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor

logger = logging.getLogger("rugpt.services.memory")

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_SUMMARY_SYSTEM_PROMPT = (_PROMPTS_DIR / "memory_summary.md").read_text(encoding="utf-8").strip()

_SUMMARY_REQUEST = "Составь краткое резюме приведённого выше диалога на русском языке."

# How many of the most recent messages are checked when deciding re-summarisation
_RESUMMARY_CHECK_LIMIT = 15
# How many of those messages must share the chat's current mem_id to trigger re-summarisation
_RESUMMARY_THRESHOLD = 10


class MemoryService:
    """
    Service for generating and managing chat memory snapshots.

    Requires:
        agent_executor          – used to instantiate an LLM for summarisation.
        chat_storage            – reads and updates chat.mem_id.
        message_storage         – reads recent messages for the re-summarisation check.
        memory_snapshot_storage – persists MemorySnapshot rows.
    """

    def __init__(
        self,
        agent_executor: AgentExecutor,
        chat_storage: ChatStorage,
        message_storage: MessageStorage,
        memory_snapshot_storage: MemorySnapshotStorage,
    ):
        self.agent_executor = agent_executor
        self.chat_storage = chat_storage
        self.message_storage = message_storage
        self.memory_snapshot_storage = memory_snapshot_storage

    # ------------------------------------------------------------------
    # Summary retrieval
    # ------------------------------------------------------------------

    async def get_summary_for_chat(self, chat_id: UUID) -> Optional[str]:
        """
        Return the snapshot text for the chat's current mem_id, or None if
        the chat has no memory snapshot yet.
        """
        chat = await self.chat_storage.get_by_id(chat_id)
        if chat is None or chat.mem_id is None:
            return None
        snapshot = await self.memory_snapshot_storage.get_by_id(chat.mem_id)
        return snapshot.snapshot if snapshot else None

    # ------------------------------------------------------------------
    # Summary generation
    # ------------------------------------------------------------------

    async def generate_summary(
        self,
        messages: List[dict],
        previous_summary: Optional[str] = None,
    ) -> str:
        """
        Summarise a message history via LLM.

        Args:
            messages:         Conversation history as a list of
                              ``{"role": "user"|"assistant", "content": str}``
                              dicts (the format produced by
                              ``AIService._build_conversation``), ordered
                              oldest → newest.
            previous_summary: Existing summary covering history older than the
                              provided messages. When given, the prompt instructs
                              the model to take it into account.

        Returns:
            Generated summary text.
        """
        # Keep only role/content to avoid passing unexpected keys to the LLM
        safe_history = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if "role" in m and "content" in m
        ]

        # Inject the previous summary as a mock user message so the model treats
        # it as established context rather than a system-level instruction.
        history_with_context = safe_history
        if previous_summary:
            mock_summary_message = {
                "role": "user",
                "content": (
                    "Предыдущее резюме диалога:\n"
                    f"{previous_summary}"
                ),
            }
            history_with_context = [mock_summary_message] + safe_history

        llm_messages = (
            [{"role": "system", "content": _SUMMARY_SYSTEM_PROMPT}]
            + history_with_context
            + [{"role": "user", "content": _SUMMARY_REQUEST}]
        )

        llm = self.agent_executor._create_llm(model=Config.DEFAULT_MODEL, temperature=0.3)
        result = await llm.ainvoke(llm_messages)
        return result.content.strip()

    # ------------------------------------------------------------------
    # Re-summarisation check
    # ------------------------------------------------------------------

    async def check_resummary_needed(self, chat_id: UUID) -> bool:
        """
        Decide whether the chat needs a new memory snapshot.

        Returns True if:
        - The chat has no memory snapshot (mem_id is None), OR
        - At least _RESUMMARY_THRESHOLD of the last _RESUMMARY_CHECK_LIMIT
          messages already carry the chat's current mem_id (meaning enough
          new messages have accumulated since the last summarisation).
        """
        chat = await self.chat_storage.get_by_id(chat_id)
        if chat is None:
            logger.warning("check_resummary_needed: chat %s not found", chat_id)
            return False

        if chat.mem_id is None:
            return True

        recent = await self.message_storage.list_by_chat(chat_id, limit=_RESUMMARY_CHECK_LIMIT)
        matching = sum(1 for m in recent if m.mem_id == chat.mem_id)
        return matching >= _RESUMMARY_THRESHOLD

    # ------------------------------------------------------------------
    # Snapshot persistence
    # ------------------------------------------------------------------

    async def update_summary(
        self,
        chat_id: UUID,
        history: List[dict],
    ) -> None:
        """
        Generate a new summary, persist it as a MemorySnapshot, and update
        the chat's mem_id to point at the new snapshot.

        Intended to be launched without await (fire-and-forget via
        asyncio.create_task). All errors are caught and logged so the
        caller is never affected.

        Args:
            chat_id: ID of the chat to summarise.
            history: Conversation history in ``{"role", "content"}`` dict format.
        """
        try:
            previous_summary = await self.get_summary_for_chat(chat_id)
            summary_text = await self.generate_summary(history, previous_summary=previous_summary)
            snapshot = await self.memory_snapshot_storage.create(
                MemorySnapshot(snapshot=summary_text)
            )
            await self.chat_storage.update_mem_id(chat_id, snapshot.id)
            logger.info("memory: new snapshot %s for chat %s", snapshot.id, chat_id)
        except Exception:
            logger.exception("memory: update_summary failed for chat %s", chat_id)
