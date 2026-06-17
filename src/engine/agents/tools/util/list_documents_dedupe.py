import logging
from dataclasses import dataclass

from langgraph.prebuilt import ToolRuntime

from src.engine.unified_logger import get_logger

from ....models.rag import RelatedDoc
from ....models.user_file import UserFile
from ...runtime import ListDocumentsRuntimeData, RuntimeContext

logger = get_logger("agents")


@dataclass(frozen=True)
class PageSlice:
    items: list[UserFile | RelatedDoc]
    page: int
    total_pages: int
    start: int
    end: int
    total: int


@dataclass(frozen=True)
class RuntimeDedupeState:
    seen_ids: set[str]
    deduplicated_across_runs: bool


def document_id(f: UserFile | RelatedDoc) -> str:
    """Return a stable document id string for either listing result shape."""
    return str(f.id) if isinstance(f, UserFile) else str(f.file_id)


def with_dedup_header(deduplicated_across_runs: bool, result: str) -> str:
    """Prefix tool output when prior calls in the same run already showed docs."""
    if not deduplicated_across_runs:
        return result
    return "[Documents already shown in previous tool calls were omitted.]\n" + result


def remember_seen_documents(runsession: object, files: list[UserFile | RelatedDoc]) -> None:
    """Persist ids only after formatted output is committed."""
    if isinstance(runsession, ListDocumentsRuntimeData):
        runsession.seen_ids.update(document_id(f) for f in files)


def get_dedupe_state(runtimedata: object) -> RuntimeDedupeState:
    """Read document dedupe state from runtime data when available."""
    seen_ids = (
        runtimedata.seen_ids
        if isinstance(runtimedata, ListDocumentsRuntimeData)
        else set()
    )
    return RuntimeDedupeState(
        seen_ids=seen_ids,
        deduplicated_across_runs=bool(seen_ids),
    )


def page_files(files: list[UserFile | RelatedDoc], page: int, page_size: int) -> PageSlice:
    """Clamp page number and return the corresponding slice metadata."""
    total = len(files)
    page = max(1, page)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    end = min(start + page_size, total)
    return PageSlice(
        items=files[start:end],
        page=page,
        total_pages=total_pages,
        start=start,
        end=end,
        total=total,
    )


def blocked_listing_message() -> str:
    """Return the tool response used when the run token cap blocks listing."""
    return (
        "[DOCUMENT LISTING IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
        "USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO LIST DOCUMENTS]"
    )


async def dedupe_and_page(
    runtime: ToolRuntime[RuntimeContext],
    files: list[UserFile | RelatedDoc],
    page: int,
    page_size: int,
    tool_name: str,
    empty_message: str,
) -> tuple[RuntimeDedupeState, PageSlice | None, str | None]:
    """Apply runtime dedupe, token-cap blocking, and pagination."""
    async with runtime.context.lock:
        runtimedata = runtime.context.list_documents_runtime_data
        dedupe_state = get_dedupe_state(runtimedata)

        before_dedupe = len(files)
        if dedupe_state.deduplicated_across_runs:
            files = [f for f in files if document_id(f) not in dedupe_state.seen_ids]
            logger.info(
                "%s dedupe: before=%d omitted=%d after=%d seen_before=%d",
                tool_name,
                before_dedupe,
                before_dedupe - len(files),
                len(files),
                len(dedupe_state.seen_ids),
            )

        if not files:
            logger.info(
                "%s empty: before_dedupe=%d seen_before=%d deduped=%s",
                tool_name,
                before_dedupe,
                len(dedupe_state.seen_ids),
                dedupe_state.deduplicated_across_runs,
            )
            return (
                dedupe_state,
                None,
                with_dedup_header(dedupe_state.deduplicated_across_runs, empty_message),
            )

        if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
            logger.info(
                "%s blocked: total_tokens_spent=%d cap=%d candidates=%d",
                tool_name,
                runtime.context.total_tokens_spent,
                runtime.context.critical_tokens_cap,
                len(files),
            )
            return dedupe_state, None, blocked_listing_message()

        return dedupe_state, page_files(files, page, page_size), None
