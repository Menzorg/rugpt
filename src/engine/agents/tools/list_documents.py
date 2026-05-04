"""
Document Tools

LangChain tool for listing documents available to an agent's initiator.

Visibility model mirrors RAG / /files endpoint: caller sees their own files
plus `is_public` files within the same org.

Service lifecycle: call init_document_service(storage) once during engine startup.

Summary budget system
---------------------
Each list_documents call may display document summaries.  To prevent the model
context from being overwhelmed we maintain a per-run token budget tracked in
ListDocumentsRuntimeData.spent_summary_tokens.

How it works:
1. Before formatting a batch we check the remaining budget
   (SUMMARY_TOKENS_BUDGET - spent_so_far).  If it is already exhausted the
   whole batch switches to compact mode (id + name + is_table only).
2. Within a batch that fits the budget: if any single summary would consume
   more than 5 % of the total budget on its own, it is cut to the median
   character length of all summaries in the batch.  This prevents one huge
   document from starving the rest.
3. After formatting we count the tokens of every summary that was displayed
   untruncated (i.e. not cut by the budget check) and add them to
   spent_summary_tokens.  Summaries replaced by [BUDGET EXHAUSTED] are not
   counted — they did not consume budget.
"""
import logging
import statistics
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool, InjectedToolArg
from langgraph.prebuilt import ToolRuntime

from ...constants import IMAGE_TYPES
from ...config import Config
from ...models.rag import RelatedDoc
from ...models.user_file import UserFile
from ..runtime import ListDocumentsRuntimeData, RuntimeContext
from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage
from ...utils.token_counter import count_tokens

logger = logging.getLogger("rugpt.agents.tools.document")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_TRUNCATED_LIMIT = 500
_MAX_RESULTS = 30

# Total token budget for document summaries across the whole agent run.
_SUMMARY_TOKENS_BUDGET = 8000
# A single summary may not exceed this fraction of the total budget.
_SUMMARY_SINGLE_ITEM_MAX_FRACTION = 0.05

_user_file_storage: Optional[UserFileStorage] = None
_rag_service: Optional[RAGService] = None


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


def _median_summary_chars(files: list[UserFile]) -> Optional[int]:
    """Median character length of non-empty summaries in *files*, or None."""
    lengths = [len(f.summary) for f in files if f.rag_status == "indexed" and f.summary]
    if not lengths:
        return None
    return int(statistics.median(lengths))


def _format_full_batch(
    files: list[UserFile],
    runtimedata: ListDocumentsRuntimeData,
) -> tuple[list[str], int]:
    """
    Format *files* with full detail (including summaries) while respecting the
    token budget stored in *runtimedata*.

    Returns (lines, tokens_spent_this_batch).

    Per-item cap: if a summary would by itself exceed
    _SUMMARY_SINGLE_ITEM_MAX_FRACTION of the total budget, it is cut to the
    median summary length of the batch before token-counting.

    Budget tracking: only summaries that were shown without being cut by the
    budget check are added to tokens_spent_this_batch — those are the ones
    that actually consumed budget.
    """
    remaining = _SUMMARY_TOKENS_BUDGET - runtimedata.spent_summary_tokens
    single_item_token_limit = int(_SUMMARY_TOKENS_BUDGET * _SUMMARY_SINGLE_ITEM_MAX_FRACTION)
    median_chars = _median_summary_chars(files)
    model = Config.DEFAULT_MODEL

    lines = []
    total_tokens_spent = 0

    for f in files:
        if f.rag_status == "indexed" and f.summary:
            summary_text = f.summary

            # Cut oversized summaries to median chars so one doc cannot monopolise budget.
            if median_chars is not None:
                raw_tokens = count_tokens(model, summary_text)
                if raw_tokens > single_item_token_limit:
                    summary_text = summary_text[:median_chars]

            tokens_for_this = count_tokens(model, summary_text)

            if remaining <= 0:
                # Budget already exhausted from earlier items or previous calls.
                summary_part = "summary: [BUDGET EXHAUSTED]"
            elif tokens_for_this <= remaining:
                summary_part = f'summary: "{summary_text}"'
                # Track only summaries actually displayed — they consumed budget.
                total_tokens_spent += tokens_for_this
                remaining -= tokens_for_this
            else:
                summary_part = "summary: [BUDGET EXHAUSTED]"
        else:
            summary_part = "summary: —"

        lines.append(
            f"- {f.original_filename} (id={f.id}, created_at={f.created_at}, "
            f"rag={f.rag_status}, is_table={f.is_table}, "
            f"size={f.file_size / 1_000_000:.2f}MB, {summary_part})"
        )

    return lines, total_tokens_spent


def _format_compact_batch(files: list[UserFile]) -> list[str]:
    """Format *files* without heavy fields (no summary, size, or dates)."""
    return [
        f"- {f.original_filename} (id={f.id}, is_table={f.is_table})"
        for f in files
    ]


@tool(response_format="content")
async def list_documents(
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: ToolRuntime[RuntimeContext],
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
) -> str:
    """List documents visible to the caller in their organization.
    Use when the user asks what files are available, to browse the catalog,
    or before calling rag_search to check if the needed document exists.
    Args:
        name_query: Substring filter on filename, case-insensitive. Omit or pass empty to skip.
        summary_query: Vector search query on document summaries. Omit or pass empty to skip.
        If any query is provided, only search results are returned (merged and deduplicated).
        If both are omitted, the full document list is returned.
    """
    logger.info(f"tool list_documents: name_query={name_query!r}, summary_query={summary_query!r}")

    if _user_file_storage is None:
        logger.error("list_documents: storage not initialized, call init_document_service() at startup")
        return "list_documents unavailable: storage not initialized."

    try:
        configurable = config.get("configurable", {})
        user_id_str = configurable.get("user_id", "")
        org_id_str = configurable.get("org_id", "")
        if not user_id_str or not org_id_str:
            return "list_documents unavailable: missing context."

        user_id = UUID(user_id_str)
        org_id = UUID(org_id_str)

        has_name_query = bool(name_query and name_query.strip())
        has_summary_query = bool(summary_query and summary_query.strip())

        all_files = await _user_file_storage.list_by_org(org_id)
        visible: list[UserFile] = [
            f for f in all_files
            if (f.uploaded_by_user_id == user_id or f.is_public)
            and not _is_image_file(f)
        ]

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

        # --- Search mode: one or both queries provided ---
        if has_name_query or has_summary_query:
            if _rag_service is None:
                return _with_dedup_header(
                    deduplicated_across_runs,
                    "list_documents: search unavailable (RAG service not initialized).",
                )

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

            if not results:
                return _with_dedup_header(
                    deduplicated_across_runs,
                    "No documents matched your query.",
                )

            lines, tokens_spent = _format_full_batch(results, runtimedata)
            runtimedata.spent_summary_tokens += tokens_spent
            _remember_seen_documents(runtimedata, results)
            return _with_dedup_header(
                deduplicated_across_runs,
                f"Documents found ({len(lines)}):\n" + "\n".join(lines),
            )

        # --- List mode: no queries ---
        total = len(visible)

        # Switch to compact when the summary token budget for this run is used up.
        budget_exhausted = runtimedata.spent_summary_tokens >= _SUMMARY_TOKENS_BUDGET

        if total > _TRUNCATED_LIMIT or budget_exhausted:
            # Compact mode: minimal fields, list capped at _TRUNCATED_LIMIT.
            truncated = visible[:_TRUNCATED_LIMIT]
            lines = _format_compact_batch(truncated)
            omitted_fields = "created_at, rag_status, file_size, summary"
            footer = f"\n[FIELDS OMITTED TO REDUCE OUTPUT: {omitted_fields}. USE FILTERS TO GET FULL INFO ON SPECIFIC DOCS.]"
            if total > _TRUNCATED_LIMIT:
                footer += f"\nTOTAL COUNT OF DOCUMENTS IN ORGANIZATION IS {total} BUT OUTPUT IS TRUNCATED TO {_TRUNCATED_LIMIT}. USE FILTERS IF REQUIRED DOCUMENTS ARE NOT IN LIST"
            if budget_exhausted:
                footer += "\n[SUMMARY BUDGET EXHAUSTED FROM PREVIOUS CALLS. USE FILTERS TO NARROW RESULTS AND SEE SUMMARIES.]"
            _remember_seen_documents(runtimedata, truncated)
            return _with_dedup_header(
                deduplicated_across_runs,
                f"Documents found ({len(lines)}):\n" + "\n".join(lines) + footer,
            )

        # Full mode: summaries included, per-item cap and budget enforced inside helper.
        lines, tokens_spent = _format_full_batch(visible, runtimedata)
        runtimedata.spent_summary_tokens += tokens_spent
        _remember_seen_documents(runtimedata, visible)
        return _with_dedup_header(
            deduplicated_across_runs,
            f"Documents found ({total} total):\n" + "\n".join(lines),
        )

    except Exception as e:
        logger.error(f"list_documents failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
