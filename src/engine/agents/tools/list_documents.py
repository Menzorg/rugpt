"""
Document Tools

Two LangChain tools for listing documents:

  list_global_documents — org-wide visibility: all is_public files (excluding active owner's own).
  list_own_documents — documents owned by the direct caller or mentioned user.

Visibility modes
----------------
  "global" — user_id != active_owner_user_id AND (is_public OR is_admin)
  "own"    — direct: caller-owned; mention: called-user-owned, public only if caller differs

Service lifecycle: call init_document_service(storage) once during engine startup.

Summary budget system
---------------------
Each list_*_documents call may display document summaries.  To prevent the model
context from being overwhelmed we maintain a per-run token budget tracked in
ListDocumentsRuntimeData.spent_summary_tokens.

How it works:
1. Before list-mode formatting we check the remaining budget
   (SUMMARY_TOKENS_BUDGET - spent_so_far).  If it is already exhausted, the
   page switches to compact mode (id + name + is_table only).
2. Within a full-detail batch, the remaining summary budget is divided across
   documents in that batch that actually have indexed summaries. Each displayed
   summary is capped by the smaller of that per-document allowance and the
   current remaining budget. This prevents one huge document from starving the rest.
3. After formatting we count the tokens of every summary text that was
   displayed and add them to spent_summary_tokens.  Summaries replaced by
   [BUDGET EXHAUSTED] are not counted — they did not consume budget.
"""
import logging
from dataclasses import dataclass
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from ...constants import IMAGE_TYPES
from ...models.rag import RelatedDoc
from ...models.user_file import UserFile
from ..runtime import ListDocumentsRuntimeData, RuntimeContext
from ...models.user import User
from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage
from ...storage.user_storage import UserStorage
from ...utils.token_counter import count_tokens, cut_text_by_token_count

logger = logging.getLogger("rugpt.agents.tools.document")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_PAGE_SIZE = 30
_MAX_RESULTS = 180

# Total token budget for document summaries across the whole agent run.
_SUMMARY_TOKENS_BUDGET = 2000

_user_file_storage: Optional[UserFileStorage] = None
_user_storage: Optional[UserStorage] = None
_rag_service: Optional[RAGService] = None


@dataclass(frozen=True)
class DocumentToolScope:
    tool_name: str
    owner_user_id: UUID
    org_id: UUID
    public_only_owner: bool
    is_admin: bool
    own_only: bool


@dataclass(frozen=True)
class PageSlice:
    items: list[UserFile]
    page: int
    total_pages: int
    start: int
    end: int
    total: int


@dataclass(frozen=True)
class RuntimeDedupeState:
    seen_ids: set[str]
    deduplicated_across_runs: bool



class ListDocumentsInput(BaseModel):
    name_query: Optional[str] = Field(
        default=None,
        description=(
            "Keyword search query on document filenames and keywords in summaries. "
            "Use keywords from the expected filename. Omit to skip."
        ),
    )
    summary_query: Optional[str] = Field(
        default=None,
        description=(
            "Vector search query on document summaries. "
            "Write a description of the document content. "
            "Omit to skip."
        ),
    )
    file_id: Optional[str] = Field(
        default=None,
        description=(
            "UUID of a specific document. When provided, returns full info for that "
            "document only, bypassing pagination, deduplication, and summary budgets. "
            "Visibility checks still apply."
        ),
    )
    page: int = Field(
        default=1,
        description="Page number for paginated listing (default 1). Page size is 50. Ignored when file_id or search queries are provided.",
    )


def init_document_service(
    storage: UserFileStorage,
    rag_service: Optional[RAGService] = None,
    user_storage: Optional[UserStorage] = None,
) -> None:
    """Set the shared storage instances for all document tool calls."""
    global _user_file_storage, _rag_service, _user_storage
    _user_file_storage = storage
    _rag_service = rag_service
    _user_storage = user_storage
    logger.info("Document tool storage initialized")


def _is_image_file(f: UserFile) -> bool:
    file_type = (f.file_type or "").lower()
    if file_type in IMAGE_TYPES:
        return True
    filename = (f.original_filename or "").lower()
    return any(filename.endswith(f".{ext}") for ext in IMAGE_TYPES)


def _with_dedup_header(deduplicated_across_runs: bool, result: str) -> str:
    # Tell the model when the list is shorter because this run already saw some documents.
    if not deduplicated_across_runs:
        return result
    return "[Documents already shown in previous tool calls were omitted.]\n" + result


def _remember_seen_documents(runsession: object, files: list[UserFile]) -> None:
    # Persist ids only after the formatted result is committed to the tool output.
    if isinstance(runsession, ListDocumentsRuntimeData):
        runsession.seen_ids.update(str(f.id) for f in files)


def _format_created_date(f: UserFile) -> str:
    # Keep dates compact for tool output; time-of-day is noise for document discovery.
    return f.created_at.date().isoformat()


async def _resolve_owner_names(
    files: list[UserFile],
    caller_id: UUID,
) -> dict[UUID, str]:
    """Return a {user_id: display_name} cache for all file owners that differ from the caller."""
    cache: dict[UUID, str] = {}
    if _user_storage is None:
        return cache
    unknown_ids = {
        f.user_id for f in files
        if f.user_id != caller_id and f.user_id not in cache
    }
    for uid in unknown_ids:
        user: Optional[User] = await _user_storage.get_by_id(uid)
        cache[uid] = user.name if user and user.name else str(uid)
    return cache


def _owner_label(f: UserFile, caller_id: UUID, owner_cache: dict[UUID, str]) -> str:
    # Own listings are scoped to this owner, so owner is only useful for global results.
    if f.user_id == caller_id:
        return ""
    name = owner_cache.get(f.user_id, str(f.user_id))
    return f", owner={name}"


def _format_full_batch(
    files: list[UserFile],
    runtimedata: ListDocumentsRuntimeData,
    caller_id: UUID,
    owner_cache: dict[UUID, str],
) -> tuple[list[str], int]:
    """
    Format *files* with full detail (including summaries) while respecting the
    token budget stored in *runtimedata*.

    Returns (lines, tokens_spent_this_batch).

    Per-item cap: the remaining summary budget is divided between the documents
    with summaries in this batch, then each summary is limited to the smaller
    of that cap and the remaining summary budget at the time it is formatted.

    Budget tracking: every displayed summary text is added to
    tokens_spent_this_batch — that is the text that actually consumed budget.
    """
    # Start from the run-wide summary budget already consumed by previous calls.
    remaining = _SUMMARY_TOKENS_BUDGET - runtimedata.spent_summary_tokens

    # Only indexed documents with real summaries participate in per-item splitting.
    summary_docs_count = sum(1 for f in files if f.rag_status == "indexed" and f.summary)
    single_item_max_tokens = (
        max(1, remaining // summary_docs_count)
        if summary_docs_count > 0
        else remaining
    )

    lines = []
    total_tokens_spent = 0

    for f in files:
        # Summaries are the expensive part of document listing; metadata is always shown.
        if f.rag_status == "indexed" and f.summary:
            if remaining <= 0:
                # Budget already exhausted from earlier items or previous calls.
                summary_part = "summary: [BUDGET EXHAUSTED]"
            else:
                summary_text = f.summary
                raw_tokens = count_tokens(summary_text)

                # Cap by both the per-document share and the live remaining budget.
                effective_limit = min(single_item_max_tokens, remaining)

                if raw_tokens > effective_limit:
                    # Reserve room for the ellipsis so the displayed summary stays within cap.
                    ellipsis_tokens = count_tokens("...")
                    cut_limit = max(1, effective_limit - ellipsis_tokens)
                    summary_text = cut_text_by_token_count(summary_text, cut_limit).rstrip() + "..."

                tokens_for_this = count_tokens(summary_text)

                if tokens_for_this <= remaining:
                    summary_part = f'summary: "{summary_text}"'
                    # Track only summaries actually displayed; rejected summaries cost nothing.
                    total_tokens_spent += tokens_for_this
                    remaining -= tokens_for_this
                else:
                    # Defensive fallback in case tokenizer/cutter accounting drifts.
                    summary_part = "summary: [TOKEN BUDGET EXHAUSTED]"
        else:
            summary_part = "summary: —"

        # Each row is self-contained so the model can copy ids into later tool calls.
        owner = _owner_label(f, caller_id, owner_cache)
        lines.append(
            f"- {f.original_filename} (id={f.id}, created_at={_format_created_date(f)}, "
            f"rag={f.rag_status}, is_table={f.is_table}{owner}, "
            f"{summary_part})"
        )

    return lines, total_tokens_spent


def _format_compact_batch(
    files: list[UserFile],
    caller_id: UUID,
    owner_cache: dict[UUID, str],
) -> list[str]:
    """Format *files* without summary, size, status, or date fields."""
    lines = []
    for f in files:
        # Compact rows avoid summary-budget spending and keep large listings scannable.
        owner = _owner_label(f, caller_id, owner_cache)
        lines.append(f"- {f.original_filename} (id={f.id}, is_table={f.is_table}{owner})")
    return lines


def _format_single_doc(f: UserFile) -> str:
    """Full detail for a single document — full summary, size, no budget cap."""
    summary_part = f'summary: "{f.summary}"' if f.rag_status == "indexed" and f.summary else "summary: —"
    return (
        f"- {f.original_filename} (id={f.id}, created_at={_format_created_date(f)}, "
        f"rag={f.rag_status}, is_table={f.is_table}, "
        f"size={f.file_size / 1_000_000:.2f}MB, {summary_part})"
    )


def _apply_visibility(
    files: list[UserFile],
    owner_user_id: UUID,
    own_only: bool,
    public_only_owner: bool,
    is_admin: bool = False,
) -> list[UserFile]:
    """Filter *files* by visibility mode, excluding images in both cases."""
    if own_only:
        # When another user calls an owner by mention, only that owner's public docs are visible.
        return [
            f for f in files
            if f.user_id == owner_user_id
            and (not public_only_owner or f.is_public)
            and not _is_image_file(f)
        ]
    # Global mode is other owners' visible documents; admins may see non-public files.
    return [
        f for f in files
        if (f.user_id != owner_user_id and (f.is_public or is_admin))
        and not _is_image_file(f)
    ]


def _can_see_file(
    f: UserFile,
    owner_user_id: UUID,
    own_only: bool,
    public_only_owner: bool,
    is_admin: bool = False,
) -> bool:
    return f in _apply_visibility([f], owner_user_id, own_only, public_only_owner, is_admin)


def _resolve_tool_identity(configurable: dict) -> tuple[str, str, bool]:
    caller_user_id = configurable.get("caller_user_id", "")
    org_id = configurable.get("org_id", "")
    # callee_user_id == caller_user_id in direct calls (always set by executor).
    callee_user_id = configurable.get("callee_user_id", "")
    public_only_owner = bool(callee_user_id and callee_user_id != caller_user_id)
    return callee_user_id or caller_user_id, org_id, public_only_owner


def _build_scope(config: RunnableConfig, own_only: bool) -> DocumentToolScope:
    """Build the immutable visibility scope from injected RunnableConfig.

    Required fields:
    - configurable.org_id: organization whose documents may be listed
    - configurable.caller_user_id: direct caller identity
    - configurable.callee_user_id: document owner (equals caller in direct calls, set by executor)
    """
    configurable = config.get("configurable", {})
    owner_user_id_str, org_id_str, public_only_owner = _resolve_tool_identity(configurable)
    return DocumentToolScope(
        tool_name="list_own_documents" if own_only else "list_global_documents",
        owner_user_id=UUID(owner_user_id_str),
        org_id=UUID(org_id_str),
        public_only_owner=public_only_owner,
        is_admin=bool(configurable.get("is_admin", False)),
        own_only=own_only,
    )


async def _get_visible_files(scope: DocumentToolScope) -> list[UserFile]:
    all_files = await _user_file_storage.list_by_org(scope.org_id)
    return _apply_visibility(
        all_files,
        scope.owner_user_id,
        scope.own_only,
        scope.public_only_owner,
        is_admin=scope.is_admin,
    )


async def _get_single_document(file_id: str, scope: DocumentToolScope) -> str:
    f = await _user_file_storage.get_by_id(UUID(file_id.strip()))
    if f is None:
        return f"Document {file_id} not found."
    if not _can_see_file(
        f,
        scope.owner_user_id,
        scope.own_only,
        scope.public_only_owner,
        scope.is_admin,
    ):
        return f"Document {file_id} not found or not visible to you."
    return _format_single_doc(f)


def _has_search_queries(name_query: Optional[str], summary_query: Optional[str]) -> bool:
    return bool(name_query and name_query.strip()) or bool(summary_query and summary_query.strip())


async def _search_visible_files(
    scope: DocumentToolScope,
    visible: list[UserFile],
    name_query: Optional[str],
    summary_query: Optional[str],
) -> tuple[list[UserFile], Optional[str]]:
    if _rag_service is None:
        return [], f"{scope.tool_name}: search unavailable (RAG service not initialized)."

    matched_ids: set[str] = set()
    org_id = str(scope.org_id)
    owner_user_id = str(scope.owner_user_id)

    if name_query and name_query.strip():
        docs: list[RelatedDoc] = await _rag_service.find_docs(
            org_id=org_id,
            user_id=owner_user_id,
            query=name_query.strip(),
            top_k=_MAX_RESULTS,
        )
        matched_ids.update(d.file_id for d in docs)

    if summary_query and summary_query.strip():
        docs = await _rag_service.find_docs(
            org_id=org_id,
            user_id=owner_user_id,
            query=summary_query.strip(),
            top_k=_MAX_RESULTS,
        )
        matched_ids.update(d.file_id for d in docs)

    results = [f for f in visible if str(f.id) in matched_ids]
    return results[:_MAX_RESULTS], None


def _get_dedupe_state(runtimedata: object) -> RuntimeDedupeState:
    seen_ids = (
        runtimedata.seen_ids
        if isinstance(runtimedata, ListDocumentsRuntimeData)
        else set()
    )
    return RuntimeDedupeState(
        seen_ids=seen_ids,
        deduplicated_across_runs=bool(seen_ids),
    )


def _page_files(files: list[UserFile], page: int) -> PageSlice:
    total = len(files)
    page = max(1, page)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages)
    start = (page - 1) * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    return PageSlice(
        items=files[start:end],
        page=page,
        total_pages=total_pages,
        start=start,
        end=end,
        total=total,
    )


def _blocked_listing_message() -> str:
    return (
        "[DOCUMENT LISTING IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
        "USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO LIST DOCUMENTS]"
    )


async def _dedupe_and_page(
    runtime: ToolRuntime[RuntimeContext],
    files: list[UserFile],
    page: int,
    scope: DocumentToolScope,
    empty_message: str,
) -> tuple[RuntimeDedupeState, Optional[PageSlice], Optional[str]]:
    async with runtime.context.lock:
        runtimedata = runtime.context.list_documents_runtime_data
        dedupe_state = _get_dedupe_state(runtimedata)

        if dedupe_state.deduplicated_across_runs:
            files = [f for f in files if str(f.id) not in dedupe_state.seen_ids]

        if not files:
            return (
                dedupe_state,
                None,
                _with_dedup_header(dedupe_state.deduplicated_across_runs, empty_message),
            )

        if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
            logger.info(
                "%s: blocked — total_tokens_spent=%d >= %d",
                scope.tool_name,
                runtime.context.total_tokens_spent,
                runtime.context.critical_tokens_cap,
            )
            return dedupe_state, None, _blocked_listing_message()

        return dedupe_state, _page_files(files, page), None


def _format_page(
    page_slice: PageSlice,
    runtimedata: ListDocumentsRuntimeData,
    owner_user_id: UUID,
    owner_cache: dict[UUID, str],
    compact_on_budget_exhausted: bool,
) -> tuple[str, int]:
    if len(page_slice.items) == 1:
        return _format_single_doc(page_slice.items[0]), 0

    budget_exhausted = (
        compact_on_budget_exhausted
        and runtimedata.spent_summary_tokens >= _SUMMARY_TOKENS_BUDGET
    )
    if budget_exhausted:
        lines = _format_compact_batch(page_slice.items, owner_user_id, owner_cache)
        footer = "\n[SUMMARY BUDGET EXHAUSTED FROM PREVIOUS CALLS. USE FILTERS TO NARROW RESULTS AND SEE SUMMARIES.]"
        tokens_spent = 0
    else:
        lines, tokens_spent = _format_full_batch(
            page_slice.items,
            runtimedata,
            owner_user_id,
            owner_cache,
        )
        footer = ""

    span = f"{page_slice.start + 1}–{page_slice.end} of {page_slice.total}"
    header = f"Documents {span} (page {page_slice.page}/{page_slice.total_pages}):"
    return f"{header}\n" + "\n".join(lines) + footer, tokens_spent


async def _format_and_commit_page(
    runtime: ToolRuntime[RuntimeContext],
    page_slice: PageSlice,
    scope: DocumentToolScope,
    dedupe_state: RuntimeDedupeState,
    compact_on_budget_exhausted: bool,
) -> str:
    owner_cache = await _resolve_owner_names(page_slice.items, scope.owner_user_id)

    async with runtime.context.lock:
        runtimedata = runtime.context.list_documents_runtime_data
        _remember_seen_documents(runtimedata, page_slice.items)

        result_body, tokens_spent = _format_page(
            page_slice,
            runtimedata,
            scope.owner_user_id,
            owner_cache,
            compact_on_budget_exhausted,
        )
        if isinstance(runtimedata, ListDocumentsRuntimeData):
            runtimedata.spent_summary_tokens += tokens_spent

        result = _with_dedup_header(
            dedupe_state.deduplicated_across_runs,
            result_body,
        )
        rag_tokens = count_tokens(result)
        runtime.context.total_tokens_spent += rag_tokens
        logger.info(
            "%s page=%d: tokens=%d, total_tokens_spent=%d",
            scope.tool_name,
            page_slice.page,
            rag_tokens,
            runtime.context.total_tokens_spent,
        )
        return result


async def _list_documents_impl(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    own_only: bool,
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
    page: int = 1,
) -> str:
    tool_name = "list_own_documents" if own_only else "list_global_documents"
    logger.info(
        "tool %s: name_query=%r, summary_query=%r, file_id=%r, page=%d",
        tool_name, name_query, summary_query, file_id, page,
    )

    if _user_file_storage is None:
        logger.error("%s: storage not initialized, call init_document_service() at startup", tool_name)
        return f"{tool_name} unavailable: storage not initialized."

    try:
        scope = _build_scope(config, own_only)

        # --- Single-document lookup: bypasses pagination, deduplication, and budgets ---
        if file_id and file_id.strip():
            return await _get_single_document(file_id, scope)

        visible = await _get_visible_files(scope)

        if _has_search_queries(name_query, summary_query):
            files, error = await _search_visible_files(scope, visible, name_query, summary_query)
            if error:
                return error
            empty_message = "No documents matched your query."
            compact_on_budget_exhausted = False
        else:
            files = visible
            empty_message = "No documents in your scope."
            compact_on_budget_exhausted = True

        dedupe_state, page_slice, early_result = await _dedupe_and_page(
            runtime,
            files,
            page,
            scope,
            empty_message,
        )
        if early_result is not None:
            return early_result

        return await _format_and_commit_page(
            runtime,
            page_slice,
            scope,
            dedupe_state,
            compact_on_budget_exhausted,
        )

    except Exception as e:
        logger.error(f"{tool_name} failed: {e}", exc_info=True)
        if isinstance(e, ValueError) and "badly formed hexadecimal UUID string" in str(e):
            return f"Invalid UUID in input: {e}"
        return _TOOL_ERROR_RESULT


async def _list_global_documents_async(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
    page: int = 1,
) -> str:
    return await _list_documents_impl(config, runtime, own_only=False,
                                      name_query=name_query, summary_query=summary_query,
                                      file_id=file_id, page=page)


async def _list_own_documents_async(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
    page: int = 1,
) -> str:
    return await _list_documents_impl(config, runtime, own_only=True,
                                      name_query=name_query, summary_query=summary_query,
                                      file_id=file_id, page=page)


list_global_documents = StructuredTool.from_function(
    coroutine=_list_global_documents_async,
    name="list_global_documents",
    description=(
        "List documents visible org-wide: all public files in the organization (excluding caller's own). "
        "Use name_query or summary_query to search; omit both for paginated listing. "
        "Use file_id to fetch full info for a single document bypassing all budgets. "
        "Use page to navigate pages (50 items per page, ordered by creation date)."
    ),
    args_schema=ListDocumentsInput,
)

list_own_documents = StructuredTool.from_function(
    coroutine=_list_own_documents_async,
    name="list_own_documents",
    description=(
        "List documents owned by the active agent user. In direct chat this includes the caller's private and public own documents; "
        "when the agent is called by another user through a mention, this includes only the called user's public own documents. "
        "Use name_query or summary_query to search; omit both for paginated listing. "
        "Use file_id to fetch full info for a single document bypassing all budgets. "
        "Use page to navigate pages (50 items per page, ordered by creation date)."
    ),
    args_schema=ListDocumentsInput,
)
