"""
User Tools

LangChain tool for searching/listing users visible to the caller.

Visibility uses DepartmentService.get_visible_user_ids — same model the task
tools use. System users (is_system=true) are filtered out — they are
infrastructure, not people, and must never leak into any user-list surface.

Async `StructuredTool.from_function(coroutine=...)` — invoked directly in
the running event loop alongside asyncpg pool.
"""

from src.engine.unified_logger import get_logger
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, InjectedToolArg
from pydantic import BaseModel, Field

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_MAX_RESULTS = 60

class UserSearchInput(BaseModel):
    name_query: str = Field(
        default="",
        description="Substring filter on user's name or username (case-insensitive). Empty = no filter.",
    )
    role_code: str = Field(
        default="",
        description="Filter users by role code (e.g. 'lawyer', 'accountant'). Empty = no filter.",
    )

def create_user_tools(user_storage, role_storage, department_service):
    """Create user tools. Returns (user_search_tool,)."""

    async def _user_search_async(
        name_query: Optional[str] = "",
        role_code: Optional[str] = "",
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """Search/list users visible to the caller.

        Args:
            name_query: Substring filter on name or username, case-insensitive.
            role_code: Filter by role code.
        """
        logger.info(
            f"tool user_search: name_query={name_query!r} role_code={role_code!r}"
        )
        try:
            configurable = (config or {}).get("configurable", {})
            user_id_str = configurable.get("user_id", "")
            org_id_str = configurable.get("org_id", "")
            if not user_id_str or not org_id_str:
                return "user_search unavailable: missing context."

            viewer_id = UUID(user_id_str)
            org_id = UUID(org_id_str)

            # Filter by role_code if given — use list_by_role, then intersect with org.
            if role_code:
                role = await role_storage.get_by_code(role_code.strip(), org_id)
                if role is None:
                    return f"No role with code '{role_code}' in your organization."
                candidates = await user_storage.list_by_role(role.id)
                # list_by_role does not filter by org_id — keep only caller's org
                # and only active/non-system.
                candidates = [
                    u for u in candidates
                    if u.org_id == org_id and u.is_active and not u.is_system
                ]
            else:
                candidates = await user_storage.list_by_org(org_id, active_only=True)
                candidates = [u for u in candidates if not u.is_system]

            # Visibility filter (departments model).
            visible_ids = await department_service.get_visible_user_ids(viewer_id, org_id)
            candidates = [u for u in candidates if u.id in visible_ids]

            # Name substring filter.
            if name_query:
                q = name_query.lower()
                candidates = [
                    u for u in candidates
                    if q in (u.name or "").lower() or q in (u.username or "").lower()
                ]

            if not candidates:
                return "No users match filter."

            # Resolve role codes and department names for output — bulk to avoid N+1.
            role_ids = {u.role_id for u in candidates if u.role_id is not None}
            role_code_by_id = {}
            for rid in role_ids:
                r = await role_storage.get_by_id(rid)
                if r is not None:
                    role_code_by_id[rid] = r.code

            departments = await department_service.list_departments(org_id)
            dept_name_by_id = {d.id: d.name for d in departments}

            total = len(candidates)
            candidates = candidates[:_MAX_RESULTS]

            lines = []
            for u in candidates:
                rc = role_code_by_id.get(u.role_id, "—") if u.role_id else "—"
                dept_name = (
                    dept_name_by_id.get(u.department_id, "—")
                    if u.department_id else "—"
                )
                username = f"@{u.username}" if u.username else "(no username)"
                flags = []
                if u.is_admin:
                    flags.append("admin")
                if u.is_head:
                    flags.append("head")
                flag_suffix = f" [{', '.join(flags)}]" if flags else ""
                lines.append(
                    f"- {u.name} ({username}, id={u.id}, role={rc}, dept={dept_name}){flag_suffix}"
                )

            more = f" (showing first {_MAX_RESULTS})" if total > _MAX_RESULTS else ""
            return f"User search results ({total} total{more}):\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"user_search failed: {e}", exc_info=True)
            return _TOOL_ERROR_RESULT

    search_tool = StructuredTool.from_function(
        coroutine=_user_search_async,
        name="user_search",
        description=(
            "Search or list users in the caller's organization who are visible to them "
            "(department-based visibility). Supports optional substring name filter and "
            "role code filter. Use to find an employee's UUID before task_create, or "
            "to answer 'who does X?' questions."
        ),
        args_schema=UserSearchInput,
    )

    return (search_tool,)
