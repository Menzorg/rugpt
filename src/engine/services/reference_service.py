"""
Reference Service

Parallel subsystem to @/@@ mentions for inline task/project references
inside message content: `!<uuid>` -> task, `!!<uuid>` -> project.

Nothing is stored structurally; parsing + resolution happens per read.
"""
import logging
import re
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from ..models.task import Task
    from ..models.user import User
    from ..storage.task_storage import TaskStorage
    from ..storage.project_storage import ProjectStorage

logger = logging.getLogger("rugpt.services.reference")


# Order matters: !!(...) BEFORE !(...) so '!!abc...' isn't chewed as single '!'.
REFERENCE_PATTERN = re.compile(
    r'!!([0-9a-fA-F-]{36})'   # group(1): project UUID
    r'|!([0-9a-fA-F-]{36})'   # group(2): task UUID
)


class ReferenceService:

    def __init__(
        self,
        task_storage: "TaskStorage",
        project_storage: "ProjectStorage",
    ):
        self.task_storage = task_storage
        self.project_storage = project_storage

    def parse(self, content: str) -> List[Tuple[str, UUID, int]]:
        """Return list of (ref_type, uuid, position). ref_type in {'task','project'}."""
        refs: List[Tuple[str, UUID, int]] = []
        if not content:
            return refs
        for m in REFERENCE_PATTERN.finditer(content):
            try:
                if m.group(1) is not None:
                    refs.append(("project", UUID(m.group(1)), m.start()))
                elif m.group(2) is not None:
                    refs.append(("task", UUID(m.group(2)), m.start()))
            except ValueError:
                # Non-canonical UUID shape -- silently skip.
                continue
        return refs

    @staticmethod
    def _can_see_task(task: "Task", actor: "User") -> bool:
        """Strict task visibility: creator, current assignee, or admin of same org."""
        if task.org_id != actor.org_id and not actor.is_admin:
            return False
        if actor.id == task.created_by_user_id:
            return True
        if actor.id == task.assignee_user_id:
            return True
        if actor.is_admin and task.org_id == actor.org_id:
            return True
        return False

    async def resolve_batch(
        self,
        contents: List[Tuple[UUID, str]],
        actor: "User",
    ) -> Dict[UUID, List[dict]]:
        """Batch-resolve references across many messages.

        Input: list of (message_id, content).
        Output: {message_id: [{type, id, title, accessible, position}, ...]}.
        One batch SQL per entity kind; per-viewer accessibility in memory.
        """
        task_ids: set = set()
        project_ids: set = set()
        per_msg: Dict[UUID, List[Tuple[str, UUID, int]]] = {}
        for msg_id, content in contents:
            parsed = self.parse(content)
            per_msg[msg_id] = parsed
            for ref_type, ref_id, _pos in parsed:
                if ref_type == "task":
                    task_ids.add(ref_id)
                else:
                    project_ids.add(ref_id)

        tasks_by_id = (
            await self.task_storage.get_many_by_ids(list(task_ids)) if task_ids else {}
        )
        projects_by_id = (
            await self.project_storage.get_many_by_ids(list(project_ids)) if project_ids else {}
        )

        result: Dict[UUID, List[dict]] = {}
        for msg_id, parsed in per_msg.items():
            resolved: List[dict] = []
            for ref_type, ref_id, pos in parsed:
                if ref_type == "task":
                    task = tasks_by_id.get(ref_id)
                    if task is None:
                        resolved.append({
                            "type": "task", "id": str(ref_id),
                            "title": None, "accessible": False, "position": pos,
                        })
                        continue
                    accessible = self._can_see_task(task, actor)
                    resolved.append({
                        "type": "task",
                        "id": str(ref_id),
                        "title": task.title if accessible else None,
                        "accessible": accessible,
                        "position": pos,
                    })
                else:  # project
                    project = projects_by_id.get(ref_id)
                    if project is None:
                        resolved.append({
                            "type": "project", "id": str(ref_id),
                            "title": None, "accessible": False, "position": pos,
                        })
                        continue
                    accessible = (
                        project.org_id == actor.org_id and project.is_active
                    )
                    resolved.append({
                        "type": "project",
                        "id": str(ref_id),
                        "title": project.name if accessible else None,
                        "accessible": accessible,
                        "position": pos,
                    })
            result[msg_id] = resolved
        return result
