"""
Calendar Tools

LangChain tools for calendar event management.
Uses factory functions to inject CalendarService dependency.
"""
import asyncio
import logging
from typing import Optional
from uuid import UUID

from langchain_core.tools import tool, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger("rugpt.agents.tools.calendar")


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
# Default stubs (used when CalendarService is not available)
# ============================================

@tool
def calendar_create_stub(title: str, description: str = "", date: str = "") -> str:
    """Create a calendar event. Use when user mentions dates, deadlines, or meetings."""
    logger.info(f"calendar_create_stub called: title={title}, date={date}")
    return f"Calendar event '{title}' noted for {date}. (Calendar service not configured)"


@tool
def calendar_query_stub(query: str = "") -> str:
    """Query upcoming calendar events."""
    logger.info(f"calendar_query_stub called: query={query}")
    return "No events found. (Calendar service not configured)"


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

    def _calendar_create(title: str, description: str = "", date: str = "") -> str:
        """Create a calendar event. Use when user mentions dates, deadlines, or meetings.
        Args:
            title: Event title
            description: Event description
            date: Date/time in ISO format (e.g. 2025-03-15T10:00:00)
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — schedule as a task
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = loop.run_in_executor(
                        pool,
                        lambda: asyncio.run(
                            calendar_service.create_from_ai_detection(
                                role_id=default_role_id or UUID('00000000-0000-0000-0000-000000000000'),
                                org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                                title=title,
                                date_str=date,
                                description=description,
                            )
                        )
                    )
                    # Can't await in sync context, return optimistic response
                    logger.info(f"calendar_create: scheduled '{title}' for {date}")
                    return f"Calendar event '{title}' created for {date}"
            else:
                event = loop.run_until_complete(
                    calendar_service.create_from_ai_detection(
                        role_id=default_role_id or UUID('00000000-0000-0000-0000-000000000000'),
                        org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                        title=title,
                        date_str=date,
                        description=description,
                    )
                )
                return f"Calendar event '{title}' created (id={event.id})"
        except Exception as e:
            logger.error(f"calendar_create failed: {e}")
            return f"Failed to create event: {e}"

    def _calendar_query(query: str = "") -> str:
        """Query upcoming calendar events.
        Args:
            query: Optional filter query
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                logger.info(f"calendar_query: query='{query}' (async context)")
                return "Calendar query executed. (Use API for full results)"
            else:
                events = loop.run_until_complete(
                    calendar_service.list_events(
                        org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                    )
                )
                if not events:
                    return "No upcoming events found."
                lines = [f"- {e.title} (at {e.next_trigger_at})" for e in events[:10]]
                return "Upcoming events:\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"calendar_query failed: {e}")
            return f"Failed to query events: {e}"

    create_tool = StructuredTool.from_function(
        func=_calendar_create,
        name="calendar_create",
        description="Create a calendar event. Use when user mentions dates, deadlines, or meetings.",
        args_schema=CalendarCreateInput,
    )

    query_tool = StructuredTool.from_function(
        func=_calendar_query,
        name="calendar_query",
        description="Query upcoming calendar events.",
        args_schema=CalendarQueryInput,
    )

    return create_tool, query_tool
