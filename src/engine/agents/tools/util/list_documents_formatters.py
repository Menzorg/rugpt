import logging
from uuid import UUID

from langgraph.prebuilt import ToolRuntime

from ....models.rag import RelatedDoc
from ....models.user import User
from ....models.user_file import UserFile
from ....storage.user_storage import UserStorage
from ....utils.token_counter import count_tokens, cut_text_by_token_count
from ...runtime import ListDocumentsRuntimeData, RuntimeContext
from .list_documents_dedupe import (
    PageSlice,
    RuntimeDedupeState,
    remember_seen_documents,
    with_dedup_header,
)

logger = logging.getLogger("rugpt.agents.tools.document")


def document_owner_id(f: UserFile | RelatedDoc) -> UUID | None:
    """Return owner user id for either listing result shape."""
    return f.user_id


def format_created_date(f: UserFile) -> str:
    """Keep dates compact for tool output."""
    return f.created_at.date().isoformat()


async def resolve_owner_names(
    files: list[UserFile | RelatedDoc],
    user_storage: UserStorage | None,
) -> dict[UUID, str]:
    """Return a {user_id: display_name} cache for all owners in the page."""
    cache: dict[UUID, str] = {}
    if user_storage is None:
        return cache
    unknown_ids = {
        document_owner_id(f) for f in files
        if document_owner_id(f) is not None and document_owner_id(f) not in cache
    }
    for uid in unknown_ids:
        user: User | None = await user_storage.get_by_id(uid)
        cache[uid] = user.name if user and user.name else str(uid)
    return cache


def owner_label(f: UserFile, owner_cache: dict[UUID, str]) -> str:
    """Format the owner suffix for file rows."""
    name = owner_cache.get(f.user_id, str(f.user_id))
    return f", owner={name}"


def related_doc_owner_label(doc: RelatedDoc, owner_cache: dict[UUID, str]) -> str:
    """Format the owner suffix for search rows."""
    if doc.user_id is None:
        return ", owner=<unknown>"
    owner_id = doc.user_id
    name = owner_cache.get(owner_id, str(doc.user_id))
    return f", owner={name}"


def format_full_batch(
    files: list[UserFile],
    runtimedata: ListDocumentsRuntimeData,
    owner_cache: dict[UUID, str],
    summary_tokens_budget: int,
) -> tuple[list[str], int]:
    """Format file rows with summaries while respecting the summary budget."""
    remaining = summary_tokens_budget - runtimedata.spent_summary_tokens
    summary_docs_count = sum(1 for f in files if f.rag_status == "indexed" and f.summary)
    single_item_max_tokens = (
        max(1, remaining // summary_docs_count)
        if summary_docs_count > 0
        else remaining
    )

    lines = []
    total_tokens_spent = 0

    for f in files:
        if f.rag_status == "indexed" and f.summary:
            if remaining <= 0:
                summary_part = "summary: [BUDGET EXHAUSTED]"
            else:
                summary_text = f.summary
                raw_tokens = count_tokens(summary_text)
                effective_limit = min(single_item_max_tokens, remaining)

                if raw_tokens > effective_limit:
                    ellipsis_tokens = count_tokens("...")
                    cut_limit = max(1, effective_limit - ellipsis_tokens)
                    summary_text = cut_text_by_token_count(summary_text, cut_limit).rstrip() + "..."

                tokens_for_this = count_tokens(summary_text)

                if tokens_for_this <= remaining:
                    summary_part = f'summary: "{summary_text}"'
                    total_tokens_spent += tokens_for_this
                    remaining -= tokens_for_this
                else:
                    summary_part = "summary: [TOKEN BUDGET EXHAUSTED]"
        else:
            summary_part = "summary: -"

        owner = owner_label(f, owner_cache)
        lines.append(
            f"- {f.original_filename} (id={f.id}, created_at={format_created_date(f)}, "
            f"rag={f.rag_status}, is_table={f.is_table}{owner}, "
            f"{summary_part})"
        )

    return lines, total_tokens_spent


def format_compact_batch(
    files: list[UserFile],
    owner_cache: dict[UUID, str],
) -> list[str]:
    """Format file rows without summaries."""
    lines = []
    for f in files:
        owner = owner_label(f, owner_cache)
        lines.append(f"- {f.original_filename} (id={f.id}, is_table={f.is_table}{owner})")
    return lines


def format_single_doc(f: UserFile, owner_cache: dict[UUID, str] | None = None) -> str:
    """Format one file row with full detail."""
    summary_part = f'summary: "{f.summary}"' if f.rag_status == "indexed" and f.summary else "summary: -"
    owner = owner_label(f, owner_cache or {})
    return (
        f"- {f.original_filename} (id={f.id}, created_at={format_created_date(f)}, "
        f"rag={f.rag_status}, is_table={f.is_table}{owner}, "
        f"size={f.file_size / 1_000_000:.2f}MB, {summary_part})"
    )


def format_search_score(doc: RelatedDoc) -> str:
    """Format SQL search mode and scores."""
    parts = [f"mode={doc.mode_used or 'search'}"]
    if doc.vec_dist is not None:
        parts.append(f"vec_dist={doc.vec_dist:.4f}")
    if doc.tsv_score is not None:
        parts.append(f"tsv_score={doc.tsv_score:.4f}")
    return ", ".join(parts)


def format_related_doc_created_date(doc: RelatedDoc) -> str:
    """Return the best compact date available on a RelatedDoc."""
    if doc.created_at is not None:
        return doc.created_at.isoformat()
    if doc.uploaded_at is not None:
        return doc.uploaded_at.date().isoformat()
    return "-"


def format_single_related_doc(doc: RelatedDoc, owner_cache: dict[UUID, str] | None = None) -> str:
    """Format one search result without summary budget truncation."""
    summary_part = f'summary: "{doc.summary}"' if doc.summary else "summary: -"
    owner = related_doc_owner_label(doc, owner_cache or {})
    return (
        f"- {doc.doc_title} (id={doc.file_id}, created_at={format_related_doc_created_date(doc)}, "
        f"{format_search_score(doc)}{owner}, {summary_part})"
    )


def format_related_docs_batch(
    docs: list[RelatedDoc],
    runtimedata: ListDocumentsRuntimeData,
    owner_cache: dict[UUID, str],
    summary_tokens_budget: int,
) -> tuple[list[str], int]:
    """Format search results while preserving SQL order and tracking budget."""
    remaining = summary_tokens_budget - runtimedata.spent_summary_tokens
    summary_docs_count = sum(1 for doc in docs if doc.summary)
    single_item_max_tokens = (
        max(1, remaining // summary_docs_count)
        if summary_docs_count > 0
        else remaining
    )

    lines = []
    total_tokens_spent = 0

    for doc in docs:
        if doc.summary:
            if remaining <= 0:
                summary_part = "summary: [BUDGET EXHAUSTED]"
            else:
                summary_text = doc.summary
                raw_tokens = count_tokens(summary_text)
                effective_limit = min(single_item_max_tokens, remaining)

                if raw_tokens > effective_limit:
                    ellipsis_tokens = count_tokens("...")
                    cut_limit = max(1, effective_limit - ellipsis_tokens)
                    summary_text = cut_text_by_token_count(summary_text, cut_limit).rstrip() + "..."

                tokens_for_this = count_tokens(summary_text)

                if tokens_for_this <= remaining:
                    summary_part = f'summary: "{summary_text}"'
                    total_tokens_spent += tokens_for_this
                    remaining -= tokens_for_this
                else:
                    summary_part = "summary: [TOKEN BUDGET EXHAUSTED]"
        else:
            summary_part = "summary: -"

        owner = related_doc_owner_label(doc, owner_cache)
        lines.append(
            f"- {doc.doc_title} (id={doc.file_id}, created_at={format_related_doc_created_date(doc)}, "
            f"{format_search_score(doc)}{owner}, {summary_part})"
        )

    return lines, total_tokens_spent


def format_page(
    page_slice: PageSlice,
    runtimedata: ListDocumentsRuntimeData,
    owner_cache: dict[UUID, str],
    compact_on_budget_exhausted: bool,
    summary_tokens_budget: int,
) -> tuple[str, int]:
    """Format one page of file rows or search rows."""
    if len(page_slice.items) == 1:
        item = page_slice.items[0]
        if isinstance(item, RelatedDoc):
            return format_single_related_doc(item, owner_cache), 0
        return format_single_doc(item, owner_cache), 0

    budget_exhausted = (
        compact_on_budget_exhausted
        and runtimedata.spent_summary_tokens >= summary_tokens_budget
    )
    if budget_exhausted:
        user_files = [f for f in page_slice.items if isinstance(f, UserFile)]
        logger.info(
            "list_documents compact: items=%d summary_tokens=%d/%d",
            len(user_files),
            runtimedata.spent_summary_tokens,
            summary_tokens_budget,
        )
        lines = format_compact_batch(user_files, owner_cache)
        footer = "\n[SUMMARY BUDGET EXHAUSTED FROM PREVIOUS CALLS. USE FILTERS TO NARROW RESULTS AND SEE SUMMARIES.]"
        tokens_spent = 0
    elif isinstance(page_slice.items[0], RelatedDoc):
        related_docs = [doc for doc in page_slice.items if isinstance(doc, RelatedDoc)]
        lines, tokens_spent = format_related_docs_batch(
            related_docs,
            runtimedata,
            owner_cache,
            summary_tokens_budget,
        )
        footer = ""
    else:
        user_files = [f for f in page_slice.items if isinstance(f, UserFile)]
        lines, tokens_spent = format_full_batch(
            user_files,
            runtimedata,
            owner_cache,
            summary_tokens_budget,
        )
        footer = ""

    span = f"{page_slice.start + 1}-{page_slice.end} of {page_slice.total}"
    header = f"Documents {span} (page {page_slice.page}/{page_slice.total_pages}):"
    return f"{header}\n" + "\n".join(lines) + footer, tokens_spent


async def format_and_commit_page(
    runtime: ToolRuntime[RuntimeContext],
    page_slice: PageSlice,
    user_storage: UserStorage | None,
    dedupe_state: RuntimeDedupeState,
    compact_on_budget_exhausted: bool,
    summary_tokens_budget: int,
) -> str:
    """Format a page and update runtime seen ids/token accounting."""
    owner_cache = await resolve_owner_names(page_slice.items, user_storage)

    async with runtime.context.lock:
        runtimedata = runtime.context.list_documents_runtime_data
        summary_before = (
            runtimedata.spent_summary_tokens
            if isinstance(runtimedata, ListDocumentsRuntimeData)
            else 0
        )
        total_before = runtime.context.total_tokens_spent
        remember_seen_documents(runtimedata, page_slice.items)

        result_body, tokens_spent = format_page(
            page_slice,
            runtimedata,
            owner_cache,
            compact_on_budget_exhausted,
            summary_tokens_budget,
        )
        if isinstance(runtimedata, ListDocumentsRuntimeData):
            runtimedata.spent_summary_tokens += tokens_spent

        result = with_dedup_header(
            dedupe_state.deduplicated_across_runs,
            result_body,
        )
        rag_tokens = count_tokens(result)
        runtime.context.total_tokens_spent += rag_tokens
        summary_after = (
            runtimedata.spent_summary_tokens
            if isinstance(runtimedata, ListDocumentsRuntimeData)
            else summary_before
        )
        logger.info(
            "list_documents commit: page=%d items=%d summary_tokens=%d->%d output_tokens=%d total_tokens=%d->%d",
            page_slice.page,
            len(page_slice.items),
            summary_before,
            summary_after,
            rag_tokens,
            total_before,
            runtime.context.total_tokens_spent,
        )
        return result
