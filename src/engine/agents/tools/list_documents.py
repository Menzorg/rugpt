"""
Document Tools

Two LangChain tools for listing documents:

  list_global_documents — org-wide visibility: caller's own files + all is_public files.
  list_private_documents — private visibility: only files uploaded by the caller.

Visibility modes
----------------
  "global"  — uploaded_by_user_id == user_id  OR  is_public
  "private" — uploaded_by_user_id == user_id  (no public files)

Service lifecycle: call init_document_service(storage) once during engine startup.

Summary budget system
---------------------
Each list_*_documents call may display document summaries.  To prevent the model
context from being overwhelmed we maintain a per-run token budget tracked in
ListDocumentsRuntimeData.spent_summary_tokens.

How it works:
1. Before formatting a batch we check the remaining budget
   (SUMMARY_TOKENS_BUDGET - spent_so_far).  If it is already exhausted the
   whole batch switches to compact mode (id + name + is_table only).
2. Within a batch that fits the budget, every summary is capped by the smaller
   of the per-item token limit and the remaining summary budget. This prevents
   one huge document from starving the rest.
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
from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage
from ...utils.token_counter import count_tokens, cut_text_by_token_count

logger = logging.getLogger("rugpt.agents.tools.document")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_TRUNCATED_LIMIT = 500
_MAX_RESULTS = 500

# Total token budget for document summaries across the whole agent run.
_SUMMARY_TOKENS_BUDGET = 4000
# A single summary may not exceed this fraction of the total budget.
_SUMMARY_SINGLE_ITEM_MAX_FRACTION = 0.025

_user_file_storage: Optional[UserFileStorage] = None
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
            "document only, bypassing all token budgets and other filters."
        ),
    )


def init_document_service(
    storage: UserFileStorage,
    rag_service: Optional[RAGService] = None,
) -> None:
    """Set the shared UserFileStorage and RAGService for all document tool calls."""
    global _user_file_storage, _rag_service
    _user_file_storage = storage
    _rag_service = rag_service
    logger.info("Document tool storage initialized")


def _is_image_file(f: UserFile) -> bool:
    file_type = (f.file_type or "").lower()
    if file_type in IMAGE_TYPES:
        return True
    filename = (f.original_filename or "").lower()
    return any(filename.endswith(f".{ext}") for ext in IMAGE_TYPES)


def _with_dedup_header(deduplicated_across_runs: bool, result: str) -> str:
    if not deduplicated_across_runs:
        return result
    return "DOCS FOUND IN PREVIOUS TOOL CALLS WERE DEDUPLICATED\n" + result


def _remember_seen_documents(runsession: object, files: list[UserFile]) -> None:
    if isinstance(runsession, ListDocumentsRuntimeData):
        runsession.seen_ids.update(str(f.id) for f in files)


def _format_created_date(f: UserFile) -> str:
    return f.created_at.date().isoformat()


def _format_full_batch(
    files: list[UserFile],
    runtimedata: ListDocumentsRuntimeData,
) -> tuple[list[str], int]:
    """
    Format *files* with full detail (including summaries) while respecting the
    token budget stored in *runtimedata*.

    Returns (lines, tokens_spent_this_batch).

    Per-item cap: each summary is limited to the smaller of the configured
    per-item token cap and the remaining summary budget at the time it is
    formatted.

    Budget tracking: every displayed summary text is added to
    tokens_spent_this_batch — that is the text that actually consumed budget.
    """
    remaining = _SUMMARY_TOKENS_BUDGET - runtimedata.spent_summary_tokens
    single_item_token_limit = int(_SUMMARY_TOKENS_BUDGET * _SUMMARY_SINGLE_ITEM_MAX_FRACTION)

    lines = []
    total_tokens_spent = 0

    for f in files:
        if f.rag_status == "indexed" and f.summary:
            if remaining <= 0:
                # Budget already exhausted from earlier items or previous calls.
                summary_part = "summary: [BUDGET EXHAUSTED]"
            else:
                summary_text = f.summary
                raw_tokens = count_tokens(summary_text)
                effective_limit = min(single_item_token_limit, remaining)

                if raw_tokens > effective_limit:
                    ellipsis_tokens = count_tokens("...")
                    cut_limit = max(1, effective_limit - ellipsis_tokens)
                    summary_text = cut_text_by_token_count(summary_text, cut_limit).rstrip() + "..."

                tokens_for_this = count_tokens(summary_text)

                if tokens_for_this <= remaining:
                    summary_part = f'summary: "{summary_text}"'
                    # Track only summaries actually displayed — they consumed budget.
                    total_tokens_spent += tokens_for_this
                    remaining -= tokens_for_this
                else:
                    summary_part = "summary: [TOKEN BUDGET EXHAUSTED]"
        else:
            summary_part = "summary: —"

        lines.append(
            f"- {f.original_filename} (id={f.id}, created_at={_format_created_date(f)}, "
            f"rag={f.rag_status}, is_table={f.is_table}, "
            f"{summary_part})"
        )

    return lines, total_tokens_spent


def _format_compact_batch(files: list[UserFile]) -> list[str]:
    """Format *files* without heavy fields (no summary, size, or dates)."""
    return [
        f"- {f.original_filename} (id={f.id}, is_table={f.is_table})"
        for f in files
    ]


def _apply_visibility(
    files: list[UserFile],
    user_id: UUID,
    private_only: bool,
    is_admin: bool = False,
) -> list[UserFile]:
    """Filter *files* by visibility mode, excluding images in both cases."""
    if private_only:
        return [
            f for f in files
            if f.uploaded_by_user_id == user_id
            and not _is_image_file(f) # Only user's own files
        ]
    return [
        f for f in files
        if (f.uploaded_by_user_id != user_id and (f.is_public or is_admin)) # Any files but user's
        and not _is_image_file(f)
    ]


async def _list_documents_impl(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    private_only: bool,
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
) -> str:
    tool_name = "list_private_documents" if private_only else "list_global_documents"
    logger.info(
        "tool %s: name_query=%r, summary_query=%r, file_id=%r",
        tool_name, name_query, summary_query, file_id,
    )

    if _user_file_storage is None:
        logger.error("%s: storage not initialized, call init_document_service() at startup", tool_name)
        return f"{tool_name} unavailable: storage not initialized."

    try:
        configurable = config.get("configurable", {})
        user_id_str = configurable.get("user_id", "")
        org_id_str = configurable.get("org_id", "")
        is_admin = configurable.get("is_admin", False)
        
        if not user_id_str or not org_id_str:
            return f"{tool_name} unavailable: missing context."

        user_id = UUID(user_id_str)
        org_id = UUID(org_id_str)

        # --- Single-document lookup: bypasses all budgets and filters ---
        if file_id and file_id.strip():
            f = await _user_file_storage.get_by_id(UUID(file_id.strip()))
            if f is None:
                return f"Document {file_id} not found."
            if private_only and f.uploaded_by_user_id != user_id:
                return f"Document {file_id} not found or not visible to you."
            if not private_only and f.uploaded_by_user_id != user_id and not f.is_public:
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
        visible: list[UserFile] = _apply_visibility(all_files, user_id, private_only, is_admin=False)

        if not visible and not private_only and is_admin:
            # If nothing found, admin can search private files of other people as well since they have visibility on everything.
            visible = _apply_visibility(all_files, user_id, private_only, is_admin=is_admin)

        # --- Search mode: one or both queries provided ---
        if has_name_query or has_summary_query:
            if _rag_service is None:
                return f"{tool_name}: search unavailable (RAG service not initialized)."

            matched_ids: set[str] = set()

            if has_name_query:
                docs: list[RelatedDoc] = await _rag_service.find_docs(
                    org_id=org_id_str,
                    user_id=user_id_str,
                    query=name_query.strip(),
                    top_k=_MAX_RESULTS,
                )
                for d in docs:
                    matched_ids.add(d.file_id)

            if has_summary_query:
                docs = await _rag_service.find_docs(
                    org_id=org_id_str,
                    user_id=user_id_str,
                    query=summary_query.strip(),
                    top_k=_MAX_RESULTS,
                )
                for d in docs:
                    matched_ids.add(d.file_id)

            results: list[UserFile] = [
                f for f in visible
                if str(f.id) in matched_ids
            ][:_MAX_RESULTS]

            async with runtime.context.lock:
                runtimedata = runtime.context.list_documents_runtime_data
                seen_ids = (
                    runtimedata.seen_ids
                    if isinstance(runtimedata, ListDocumentsRuntimeData)
                    else set()
                )
                deduplicated_across_runs = bool(seen_ids)
                if deduplicated_across_runs:
                    results = [f for f in results if str(f.id) not in seen_ids]

                if not results:
                    return _with_dedup_header(
                        deduplicated_across_runs,
                        "No documents matched your query.",
                    )

                # --- RAG budget guard ---
                if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
                    logger.info(
                        "%s: blocked — total_tokens_spent=%d >= %d",
                        tool_name, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,
                    )
                    return (
                        "[RAG SEARCH IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                        "USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO LIST DOCUMENTS]"
                    )

                lines, tokens_spent = _format_full_batch(results, runtimedata)
                runtimedata.spent_summary_tokens += tokens_spent
                _remember_seen_documents(runtimedata, results)
                result = _with_dedup_header(
                    deduplicated_across_runs,
                    f"Documents found ({len(lines)}):\n" + "\n".join(lines),
                )
                rag_tokens = count_tokens(result)
                runtime.context.total_tokens_spent += rag_tokens
                logger.info("%s search: tokens=%d, total_tokens_spent=%d", tool_name, rag_tokens, runtime.context.total_tokens_spent)
                return result

        async with runtime.context.lock:
            # --- Deduplication across the whole agent run ---
            runtimedata = runtime.context.list_documents_runtime_data
            seen_ids = (
                runtimedata.seen_ids
                if isinstance(runtimedata, ListDocumentsRuntimeData)
                else set()
            )
            deduplicated_across_runs = bool(seen_ids)
            if deduplicated_across_runs:
                visible = [f for f in visible if str(f.id) not in seen_ids]

            if not visible:
                return _with_dedup_header(
                    deduplicated_across_runs,
                    "No documents in your scope.",
                )

            # --- RAG budget guard ---
            if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
                logger.info(
                    "%s: blocked — total_tokens_spent=%d >= %d",
                    tool_name, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,
                )
                return (
                    "[RAG SEARCH IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                    "USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO LIST DOCUMENTS]"
                )

            # --- List mode: no queries ---
            total = len(visible)

            # Switch to compact when the summary token budget for this run is used up.
            budget_exhausted = runtimedata.spent_summary_tokens >= _SUMMARY_TOKENS_BUDGET

            if total > _TRUNCATED_LIMIT or budget_exhausted:
                truncated = visible[:_TRUNCATED_LIMIT]
                lines = _format_compact_batch(truncated)
                omitted_fields = "created_at, rag_status, file_size, summary"
                footer = f"\n[FIELDS OMITTED TO REDUCE OUTPUT: {omitted_fields}. USE FILTERS TO GET FULL INFO ON SPECIFIC DOCS.]"
                if total > _TRUNCATED_LIMIT:
                    footer += f"\nTOTAL COUNT OF DOCUMENTS IN ORGANIZATION IS {total} BUT OUTPUT IS TRUNCATED TO {_TRUNCATED_LIMIT}. USE FILTERS IF REQUIRED DOCUMENTS ARE NOT IN LIST"
                if budget_exhausted:
                    footer += "\n[SUMMARY BUDGET EXHAUSTED FROM PREVIOUS CALLS. USE FILTERS TO NARROW RESULTS AND SEE SUMMARIES.]"
                _remember_seen_documents(runtimedata, truncated)
                result = _with_dedup_header(
                    deduplicated_across_runs,
                    f"Documents found ({len(lines)}):\n" + "\n".join(lines) + footer,
                )
                rag_tokens = count_tokens(result)
                runtime.context.total_tokens_spent += rag_tokens
                logger.info("%s compact: tokens=%d, total_tokens_spent=%d", tool_name, rag_tokens, runtime.context.total_tokens_spent)
                return result

            # Full mode: summaries included, per-item cap and budget enforced inside helper.
            lines, tokens_spent = _format_full_batch(visible, runtimedata)
            runtimedata.spent_summary_tokens += tokens_spent
            _remember_seen_documents(runtimedata, visible)
            result = _with_dedup_header(
                deduplicated_across_runs,
                f"Documents found ({total} total):\n" + "\n".join(lines),
            )
            rag_tokens = count_tokens(result)
            runtime.context.total_tokens_spent += rag_tokens
            logger.info("%s full: tokens=%d, total_tokens_spent=%d", tool_name, rag_tokens, runtime.context.total_tokens_spent)
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
) -> str:
    return await _list_documents_impl(config, runtime, private_only=False,
                                      name_query=name_query, summary_query=summary_query, file_id=file_id)


async def _list_private_documents_async(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: Annotated[ToolRuntime[RuntimeContext], InjectedToolArg],
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
    file_id: Optional[str] = None,
) -> str:
    return await _list_documents_impl(config, runtime, private_only=True,
                                      name_query=name_query, summary_query=summary_query, file_id=file_id)


list_global_documents = StructuredTool.from_function(
    coroutine=_list_global_documents_async,
    name="list_global_documents",
    description=(
        "List documents visible org-wide: caller's own files plus all public files in the organization. "
        "Use name_query or summary_query to search; omit both for the full list. "
        "Use file_id to fetch full info for a single document bypassing all budgets."
    ),
    args_schema=ListDocumentsInput,
)

list_private_documents = StructuredTool.from_function(
    coroutine=_list_private_documents_async,
    name="list_private_documents",
    description=(
        "List only documents uploaded by the caller — no public or other users' files are included. "
        "Use name_query or summary_query to search; omit both for the full list. "
        "Use file_id to fetch full info for a single document bypassing all budgets."
    ),
    args_schema=ListDocumentsInput,
)
