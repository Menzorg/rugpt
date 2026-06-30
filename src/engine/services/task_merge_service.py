"""
TaskMergeService

Merges several tasks into one. Two phases:

  preview(actor, source_tasks):
      Gather the source tasks + their chat transcripts, ask the hidden
      `task_merger` system role to FORMULATE a merged title/description/summary,
      persist a TaskMergeRequest (with an as-is snapshot for recovery) and return
      it. No mutation of the source tasks happens here — the human verifies and
      edits the proposal first.

  apply(actor, merge_request, ...):
      Deterministically create the new task, post the summary into its chat,
      then close every source task (note in chat + 'merged' event + link +
      deactivate). The LLM is NOT involved here — all side effects are plain
      service code so a destructive close-N-create-1 stays predictable.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from src.engine.unified_logger import get_logger
from ..config import Config
from ..models.message import Message, SenderType
from ..models.task_merge_request import TaskMergeRequest

logger = get_logger("services")

_MAX_MESSAGES_PER_TASK = 100
_MAX_CONTENT_CHARS = 2000


class TaskMergeService:
    def __init__(
        self,
        *,
        task_storage,
        task_service,
        chat_service,
        message_storage,
        chat_storage,
        task_event_service,
        task_participant_storage,
        role_storage,
        agent_executor,
        merge_request_storage,
        user_storage,
        system_org_id: UUID = Config.SYSTEM_ORG_ID,
    ):
        self.task_storage = task_storage
        self.task_service = task_service
        self.chat_service = chat_service
        self.message_storage = message_storage
        self.chat_storage = chat_storage
        self.task_event_service = task_event_service
        self.task_participant_storage = task_participant_storage
        self.role_storage = role_storage
        self.agent_executor = agent_executor
        self.merge_request_storage = merge_request_storage
        self.user_storage = user_storage
        self.system_org_id = system_org_id

    # --- Phase 1: preview -----------------------------------------------------

    async def preview(self, actor, source_tasks: list) -> TaskMergeRequest:
        """Build context, ask the agent to formulate the merge, persist preview."""
        context_text, snapshot = await self._build_context_and_snapshot(source_tasks)

        role = await self.role_storage.get_by_code("task_merger", self.system_org_id)
        if role is None:
            raise RuntimeError(
                "task_merger role not found — migration 059 not applied?"
            )

        result, _ = await self.agent_executor.execute(
            role=role,
            messages=[{"role": "user", "content": context_text}],
            caller_user_id=actor.id,
            chat_id=None,
            agent_name="task_merger",
            temperature=0.3,
        )
        parsed = self._parse_agent_json(getattr(result, "content", "") or "")

        title = (parsed.get("title") or "").strip()
        description = (parsed.get("description") or "").strip()
        summary = (parsed.get("summary") or "").strip()
        if not title:
            # Mechanical fallback when the agent returns nothing usable, so the
            # human still gets an editable proposal instead of an error.
            title = ("Объединённая задача: "
                     + ", ".join(t.title for t in source_tasks))[:200]

        req = TaskMergeRequest(
            org_id=actor.org_id,
            actor_user_id=actor.id,
            source_task_ids=[t.id for t in source_tasks],
            source_snapshot=snapshot,
            proposed_title=title,
            proposed_description=description,
            proposed_summary=summary,
            status="previewed",
        )
        return await self.merge_request_storage.create(req)

    # --- Phase 2: apply -------------------------------------------------------

    async def apply(
        self,
        actor,
        merge_request: TaskMergeRequest,
        *,
        title: str,
        description: Optional[str],
        assignee_user_id: UUID,
        project_id: Optional[UUID] = None,
        deadline: Optional[datetime] = None,
        participant_user_ids: Optional[List[UUID]] = None,
    ):
        """Create the merged task and close the sources. Returns the new Task."""
        source_tasks = []
        for tid in merge_request.source_task_ids:
            t = await self.task_storage.get_by_id(tid)
            if t is not None:
                source_tasks.append(t)
        if len(source_tasks) < 2:
            raise ValueError("Need at least two active source tasks to merge")

        participants = await self._collect_participants(source_tasks)
        if participant_user_ids:
            participants = list({*participants, *participant_user_ids})

        new_task = await self.task_service.create(
            org_id=actor.org_id,
            title=title,
            assignee_user_id=assignee_user_id,
            description=(description or None),
            deadline=deadline,
            created_by_user_id=actor.id,
            project_id=project_id,
            participant_user_ids=participants or None,
        )

        # Summary message in the new task chat.
        new_chat = await self.chat_service.get_task_chat(new_task.id)
        if new_chat is not None and merge_request.proposed_summary:
            src_list = ", ".join(str(t.id) for t in source_tasks)
            await self._post_message(
                new_chat.id, actor.id,
                f"🔀 Объединено из задач: {src_list}\n\n{merge_request.proposed_summary}",
            )

        # Close each source task: note -> link -> event -> deactivate.
        for st in source_tasks:
            src_chat = await self.chat_service.get_task_chat(st.id)
            if src_chat is not None:
                await self._post_message(
                    src_chat.id, actor.id,
                    f"🔀 Задача объединена в «{title}» ({new_task.id}).",
                )
            await self.task_storage.set_merged_into(st.id, new_task.id)
            if self.task_event_service is not None:
                await self.task_event_service.record(
                    task_id=st.id,
                    actor_user_id=actor.id,
                    event_type="merged",
                    payload={"into_task_id": str(new_task.id)},
                )
            await self.task_service.deactivate(st.id, user=actor)

        await self.merge_request_storage.mark_applied(merge_request.id, new_task.id)
        return new_task

    # --- Helpers --------------------------------------------------------------

    async def _post_message(self, chat_id: UUID, sender_id: UUID, content: str) -> Message:
        msg = Message(
            chat_id=chat_id,
            sender_type=SenderType.USER,
            sender_id=sender_id,
            content=content,
        )
        created = await self.message_storage.create(msg)
        await self.chat_storage.update_last_message(chat_id)
        return created

    async def _collect_participants(self, source_tasks: list) -> List[UUID]:
        ids = set()
        for t in source_tasks:
            if t.created_by_user_id:
                ids.add(t.created_by_user_id)
            if t.assignee_user_id:
                ids.add(t.assignee_user_id)
            try:
                for uid in await self.task_participant_storage.list_user_ids(t.id):
                    ids.add(uid)
            except Exception as e:  # best-effort — don't fail merge on participant read
                logger.warning(f"merge: failed to read participants for task {t.id}: {e}")
        return list(ids)

    async def _build_context_and_snapshot(self, source_tasks: list):
        name_cache: dict = {}

        async def name_of(uid: Optional[UUID]) -> str:
            if uid is None:
                return "—"
            if uid in name_cache:
                return name_cache[uid]
            nm = str(uid)
            try:
                u = await self.user_storage.get_by_id(uid)
                if u is not None:
                    nm = u.name
            except Exception:
                pass
            name_cache[uid] = nm
            return nm

        lines: List[str] = []
        snap_tasks: List[dict] = []
        for idx, t in enumerate(source_tasks, 1):
            chat = await self.chat_service.get_task_chat(t.id)
            msgs = []
            if chat is not None:
                raw = await self.chat_service.list_messages(
                    chat.id, limit=_MAX_MESSAGES_PER_TASK
                )
                msgs = list(reversed(raw or []))  # storage returns DESC; want chronological

            lines.append(f"### Задача {idx}: {t.title}")
            lines.append(f"Статус: {t.status}")
            if t.description:
                lines.append(f"Описание: {t.description}")
            lines.append(
                f"Создатель: {await name_of(t.created_by_user_id)}; "
                f"Исполнитель: {await name_of(t.assignee_user_id)}"
            )
            if msgs:
                lines.append("Переписка:")
                for m in msgs:
                    who = await name_of(m.sender_id)
                    content = (m.content or "")[:_MAX_CONTENT_CHARS]
                    lines.append(f"  [{who}]: {content}")
            lines.append("")

            snap_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "description": t.description,
                "status": t.status,
                "created_by_user_id": str(t.created_by_user_id) if t.created_by_user_id else None,
                "assignee_user_id": str(t.assignee_user_id) if t.assignee_user_id else None,
                "deadline": t.deadline.isoformat() if t.deadline else None,
                "project_id": str(t.project_id) if t.project_id else None,
                "priority": t.priority,
                "chat_id": str(chat.id) if chat else None,
                "messages": [
                    {
                        "sender_id": str(m.sender_id),
                        "sender_type": getattr(m.sender_type, "value", str(m.sender_type)),
                        "content": m.content,
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                    }
                    for m in msgs
                ],
            })

        context_text = (
            "Объедини следующие задачи в одну. Верни строго JSON "
            '{"title":..., "description":..., "summary":...}.\n\n'
            + "\n".join(lines)
        )
        return context_text, {"tasks": snap_tasks}

    @staticmethod
    def _parse_agent_json(content: str) -> dict:
        """Best-effort extraction of the {title, description, summary} JSON the
        agent was asked to return — tolerant to markdown fences / surrounding text."""
        if not content:
            return {}
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
        return {}
