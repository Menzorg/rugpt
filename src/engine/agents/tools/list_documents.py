"""
Document Tools

Two LangChain tools for listing documents:

  list_global_documents — org-wide visibility: all is_public files (excluding caller's own).
  list_private_documents — private visibility: only files uploaded by the caller.

Visibility modes
----------------
  "global"  — uploaded_by_user_id != user_id  AND  (is_public OR is_admin)
  "private" — uploaded_by_user_id == user_id  (no public files)

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
        f.uploaded_by_user_id for f in files
        if f.uploaded_by_user_id != caller_id and f.uploaded_by_user_id not in cache
    }
    for uid in unknown_ids:
        user: Optional[User] = await _user_storage.get_by_id(uid)
        cache[uid] = user.name if user and user.name else str(uid)
    return cache


def _owner_label(f: UserFile, caller_id: UUID, owner_cache: dict[UUID, str]) -> str:
    # Private listings are caller-owned, so owner is only useful for global results.
    if f.uploaded_by_user_id == caller_id:
        return ""
    name = owner_cache.get(f.uploaded_by_user_id, str(f.uploaded_by_user_id))
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


def _apply_visibility(
    files: list[UserFile],
    user_id: UUID,
    private_only: bool,
    is_admin: bool = False,
) -> list[UserFile]:
    """Filter *files* by visibility mode, excluding images in both cases."""
    if private_only:
        # Private mode is strictly caller-owned documents; public files do not leak in.
        return [
            f for f in files
            if f.uploaded_by_user_id == user_id
            and not _is_image_file(f)
        ]
    # Global mode is other users' visible documents; admins may see non-public files.
    return [
        f for f in files
        if (f.uploaded_by_user_id != user_id and (f.is_public or is_admin))
        and not _is_image_file(f)
    ]


async def _list_documents_impl(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    private_only: bool,
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
    page: int = 1,
) -> str:
    tool_name = "list_private_documents" if private_only else "list_global_documents"
    logger.info(
        "tool %s: name_query=%r, summary_query=%r, file_id=%r, page=%d",
        tool_name, name_query, summary_query, file_id, page,
    )

    if _user_file_storage is None:
        logger.error("%s: storage not initialized, call init_document_service() at startup", tool_name)
        return f"{tool_name} unavailable: storage not initialized."

    try:
        configurable = config.get("configurable", {})
        user_id_str = configurable.get("user_id", "")
        org_id_str = configurable.get("org_id", "")
        is_admin = bool(configurable.get("is_admin", False))

        if not user_id_str or not org_id_str:
            return f"{tool_name} unavailable: missing context."

        user_id = UUID(user_id_str)
        org_id = UUID(org_id_str)

        # --- Single-document lookup: bypasses pagination, deduplication, and budgets ---
        if file_id and file_id.strip():
            f = await _user_file_storage.get_by_id(UUID(file_id.strip()))
            if f is None:
                return f"Document {file_id} not found."
            if private_only and f.uploaded_by_user_id != user_id:
                return f"Document {file_id} not found or not visible to you."
            if (
                not private_only
                and f.uploaded_by_user_id != user_id
                and not (f.is_public or is_admin)
            ):
                return f"Document {file_id} not found or not visible to you."
            summary_part = f'summary: "{f.summary}"' if f.rag_status == "indexed" and f.summary else "summary: —"
            return (
                f"- {f.original_filename} (id={f.id}, created_at={_format_created_date(f)}, "
                f"rag={f.rag_status}, is_table={f.is_table}, "
                f"size={f.file_size / 1_000_000:.2f}MB, {summary_part})"
            )

        has_name_query = bool(name_query and name_query.strip())
        has_summary_query = bool(summary_query and summary_query.strip())

        all_files = await _user_file_storage.list_by_org(org_id)

        # Establish the visibility scope before applying search or pagination.
        visible: list[UserFile] = _apply_visibility(all_files, user_id, private_only, is_admin=False)

        if not visible and not private_only and is_admin:
            # Admin fallback: include non-public files of other users too.
            visible = _apply_visibility(all_files, user_id, private_only, is_admin=is_admin)

        # --- Search mode: one or both queries provided ---
        if has_name_query or has_summary_query:
            if _rag_service is None:
                return f"{tool_name}: search unavailable (RAG service not initialized)."

            # Search can combine filename/keyword and summary-vector matches.
            matched_ids: set[str] = set()

            if has_name_query:
                # Name search uses the same RAG index but the query is expected to be filename-like.
                docs: list[RelatedDoc] = await _rag_service.find_docs(
                    org_id=org_id_str,
                    user_id=user_id_str,
                    query=name_query.strip(),
                    top_k=_MAX_RESULTS
                )
                for d in docs:
                    matched_ids.add(d.file_id)

            if has_summary_query:
                # Summary search is content-oriented and unions with name search results.
                docs = await _rag_service.find_docs(
                    org_id=org_id_str,
                    user_id=user_id_str,
                    query=summary_query.strip(),
                    top_k=_MAX_RESULTS
                )
                for d in docs:
                    matched_ids.add(d.file_id)

            # Intersect raw search hits with tool visibility and cap the output set.
            results: list[UserFile] = [
                f for f in visible
                if str(f.id) in matched_ids
            ][:_MAX_RESULTS]

            async with runtime.context.lock:
                # Shared run state is read under lock so parallel tool calls dedupe consistently.
                runtimedata = runtime.context.list_documents_runtime_data
                seen_ids = (
                    runtimedata.seen_ids
                    if isinstance(runtimedata, ListDocumentsRuntimeData)
                    else set()
                )
                deduplicated_across_runs = bool(seen_ids)
                if deduplicated_across_runs:
                    # Omit documents already returned by earlier calls in this same run.
                    results = [f for f in results if str(f.id) not in seen_ids]

                if not results:
                    return _with_dedup_header(
                        deduplicated_across_runs,
                        "No documents matched your query.",
                    )

                # --- Retrieval-output budget guard ---
                if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
                    logger.info(
                        "%s: blocked — total_tokens_spent=%d >= %d",
                        tool_name, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,
                    )
                    return (
                        "[DOCUMENT LISTING IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                        "USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO LIST DOCUMENTS]"
                    )

                total = len(results)
                # Search results are still paginated because the union may be large.
                page = max(1, page)
                total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
                page = min(page, total_pages)
                start = (page - 1) * _PAGE_SIZE
                end = min(start + _PAGE_SIZE, total)
                page_results = results[start:end]

            # Owner lookup may hit storage, so keep it outside the runtime-state lock.
            owner_cache: dict[UUID, str] = await _resolve_owner_names(page_results, user_id)

            async with runtime.context.lock:
                # Re-enter the lock for summary-budget spending and seen-id mutations.
                runtimedata = runtime.context.list_documents_runtime_data
                lines, tokens_spent = _format_full_batch(page_results, runtimedata, user_id, owner_cache)
                runtimedata.spent_summary_tokens += tokens_spent
                _remember_seen_documents(runtimedata, page_results)

                # Build the final page after mutations so token accounting matches returned text.
                span = f"{start + 1}–{end} of {total}"
                header = f"Documents {span} (page {page}/{total_pages}):"
                result = _with_dedup_header(
                    deduplicated_across_runs,
                    f"{header}\n" + "\n".join(lines),
                )
                rag_tokens = count_tokens(result)
                # Track total retrieval text injected into the agent context.
                runtime.context.total_tokens_spent += rag_tokens
                logger.info("%s search page=%d: tokens=%d, total_tokens_spent=%d", tool_name, page, rag_tokens, runtime.context.total_tokens_spent)
                return result

        # --- List mode: paginated, ordered by creation date (storage already orders DESC) ---
        async with runtime.context.lock:
            # List mode has no search hits, so dedupe the whole visible scope before paging.
            runtimedata = runtime.context.list_documents_runtime_data
            seen_ids = (
                runtimedata.seen_ids
                if isinstance(runtimedata, ListDocumentsRuntimeData)
                else set()
            )
            deduplicated_across_runs = bool(seen_ids)
            if deduplicated_across_runs:
                # Page numbers apply to the not-yet-shown remainder of the listing.
                visible = [f for f in visible if str(f.id) not in seen_ids]

            if not visible:
                return _with_dedup_header(
                    deduplicated_across_runs,
                    "No documents in your scope.",
                )

            total = len(visible)
            # Clamp requested page into the available range after deduplication.
            page = max(1, page)
            total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
            page = min(page, total_pages)
            start = (page - 1) * _PAGE_SIZE
            end = min(start + _PAGE_SIZE, total)
            page_files = visible[start:end]

            # --- Retrieval-output budget guard ---
            if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
                logger.info(
                    "%s: blocked — total_tokens_spent=%d >= %d",
                    tool_name, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,
                )
                return (
                    "[DOCUMENT LISTING IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                    "USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO LIST DOCUMENTS]"
                )

        # Owner lookup is independent of runtime counters and does not need the lock.
        owner_cache = await _resolve_owner_names(page_files, user_id)

        async with runtime.context.lock:
            # Formatting decides whether this page can afford summaries.
            runtimedata = runtime.context.list_documents_runtime_data
            budget_exhausted = runtimedata.spent_summary_tokens >= _SUMMARY_TOKENS_BUDGET

            if budget_exhausted:
                # Once the summary budget is gone, keep listing ids/names without more summary text.
                lines = _format_compact_batch(page_files, user_id, owner_cache)
                footer = "\n[SUMMARY BUDGET EXHAUSTED FROM PREVIOUS CALLS. USE FILTERS TO NARROW RESULTS AND SEE SUMMARIES.]"
            else:
                lines, tokens_spent = _format_full_batch(page_files, runtimedata, user_id, owner_cache)
                runtimedata.spent_summary_tokens += tokens_spent
                footer = ""

            # Mark only documents that were actually returned to the model.
            _remember_seen_documents(runtimedata, page_files)
            span = f"{start + 1}–{end} of {total}"
            header = f"Documents {span} (page {page}/{total_pages}):"
            result = _with_dedup_header(
                deduplicated_across_runs,
                f"{header}\n" + "\n".join(lines) + footer,
            )
            rag_tokens = count_tokens(result)
            # Total output budget includes metadata and headers, not just summaries.
            runtime.context.total_tokens_spent += rag_tokens
            logger.info("%s list page=%d: tokens=%d, total_tokens_spent=%d", tool_name, page, rag_tokens, runtime.context.total_tokens_spent)
            return result

    except Exception as e:
        logger.error(f"{tool_name} failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT


async def _list_global_documents_async(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
    page: int = 1,
) -> str:
    return await _list_documents_impl(config, runtime, private_only=False,
                                      name_query=name_query, summary_query=summary_query,
                                      file_id=file_id, page=page)


async def _list_private_documents_async(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
    page: int = 1,
) -> str:
    return await _list_documents_impl(config, runtime, private_only=True,
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

list_private_documents = StructuredTool.from_function(
    coroutine=_list_private_documents_async,
    name="list_private_documents",
    description=(
        "List only documents uploaded by the caller — no public or other users' files are included. "
        "Use name_query or summary_query to search; omit both for paginated listing. "
        "Use file_id to fetch full info for a single document bypassing all budgets. "
        "Use page to navigate pages (50 items per page, ordered by creation date)."
    ),
    args_schema=ListDocumentsInput,
)
