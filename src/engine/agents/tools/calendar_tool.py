"""
Calendar Tools

LangChain tools for calendar event management.
Uses factory functions to inject CalendarService dependency.

Tools are async — invoked directly in the running event loop, so we just
`await` service calls. Avoids the event-loop gymnastics and the
`There is no current event loop` errors on worker-thread invocations.
"""
import logging
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, InjectedToolArg
from pydantic import BaseModel, Field

logger = logging.getLogger("rugpt.agents.tools.calendar")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"


# ============================================
# Tool input schemas
# ============================================

class CalendarCreateInput(BaseModel):
    title: str = Field(description="Event title")
    description: str = Field(default="", description="Event description")
    date: str = Field(default="", description="Date/time in ISO format (e.g. 2025-03-15T10:00:00)")


class CalendarQueryInput(BaseModel):
    query: str = Field(default="", description="Optional filter query")


# ============================================
# Factory: create tools wired to CalendarService
# ============================================

def create_calendar_tools(
    calendar_service,
    default_role_id: Optional[UUID] = None,
    default_org_id: Optional[UUID] = None,
):
    """
    Create calendar tools wired to a real CalendarService instance.

    Returns (calendar_create_tool, calendar_query_tool).
    """

    async def _calendar_create_async(
        title: str,
        description: str = "",
        date: str = "",
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """Create a calendar event. Use when user mentions dates, deadlines, or meetings.
        Args:
            title: Event title
            description: Event description
            date: Date/time in ISO format (e.g. 2025-03-15T10:00:00)
        """
        logger.info(f"tool calendar_create: title={title!r} date={date!r}")
        try:
            configurable = (config or {}).get("configurable", {})
            org_id_str = configurable.get("org_id", "")
            # Prefer initiator's org (set by executor). Fallback to defaults.
            scope_org_id = (
                UUID(org_id_str) if org_id_str
                else default_org_id
                or UUID('00000000-0000-0000-0000-000000000000')
            )
            event = await calendar_service.create_from_ai_detection(
                role_id=default_role_id or UUID('00000000-0000-0000-0000-000000000000'),
                org_id=scope_org_id,
                title=title,
                date_str=date,
                description=description,
            )
            return f"Calendar event '{title}' created (id={event.id})"
        except Exception as e:
            logger.error(f"calendar_create failed: {e}", exc_info=True)
            return _TOOL_ERROR_RESULT

    async def _calendar_query_async(
        query: str = "",
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """Query upcoming calendar events.
        Args:
            query: Optional filter query
        """
        logger.info(f"tool calendar_query: query={query!r}")
        try:
            configurable = (config or {}).get("configurable", {})
            org_id_str = configurable.get("org_id", "")
            scope_org_id = (
                UUID(org_id_str) if org_id_str
                else default_org_id
                or UUID('00000000-0000-0000-0000-000000000000')
            )
            events = await calendar_service.list_events(org_id=scope_org_id)
            if not events:
                return "No upcoming events found."
            lines = [f"- {e.title} (at {e.next_trigger_at})" for e in events[:10]]
            return "Upcoming events:\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"calendar_query failed: {e}", exc_info=True)
            return _TOOL_ERROR_RESULT

    create_tool = StructuredTool.from_function(
        coroutine=_calendar_create_async,
        name="calendar_create",
        description="Create a calendar event. Use when user mentions dates, deadlines, or meetings.",
        args_schema=CalendarCreateInput,
    )

    query_tool = StructuredTool.from_function(
        coroutine=_calendar_query_async,
        name="calendar_query",
        description="Query upcoming calendar events.",
        args_schema=CalendarQueryInput,
    )

    return create_tool, query_tool
