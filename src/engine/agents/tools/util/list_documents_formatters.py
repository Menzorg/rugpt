import logging
from dataclasses import dataclass
from uuid import UUID

from langgraph.prebuilt import ToolRuntime

from ....models.rag import RelatedDoc
from ....models.user import User
from ....models.user_file import UserFile
from ....storage.user_storage import UserStorage
from ....utils.token_counter import count_tokens, cut_text_by_token_count
from ...runtime import RuntimeContext

logger = logging.getLogger("rugpt.agents.tools.document")

_SUMMARY_TOKEN_LIMIT = 100


def _truncate_summary(summary: str) -> str:
    """Truncate summary to _SUMMARY_TOKEN_LIMIT tokens."""
    if count_tokens(summary) <= _SUMMARY_TOKEN_LIMIT:
        return summary
    return cut_text_by_token_count(summary, _SUMMARY_TOKEN_LIMIT).rstrip() + "..."


@dataclass(frozen=True)
class PageSlice:
    items: list[UserFile | RelatedDoc]
    page: int
    total_pages: int
    start: int
    end: int
    total: int


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
    owner_cache: dict[UUID, str],
) -> list[str]:
    """Format file rows with summaries truncated to _SUMMARY_TOKEN_LIMIT tokens each."""
    lines = []
    for f in files:
        if f.rag_status == "indexed" and f.summary:
            summary_part = f'summary: "{_truncate_summary(f.summary)}"'
        else:
            summary_part = "summary: -"
        owner = owner_label(f, owner_cache)
        lines.append(
            f"- {f.original_filename} (id={f.id}, created_at={format_created_date(f)}, "
            f"rag={f.rag_status}, is_table={f.is_table}{owner}, "
            f"{summary_part})"
        )
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
    owner_cache: dict[UUID, str],
) -> list[str]:
    """Format search results with summaries truncated to _SUMMARY_TOKEN_LIMIT tokens each."""
    lines = []
    for doc in docs:
        if doc.summary:
            summary_part = f'summary: "{_truncate_summary(doc.summary)}"'
        else:
            summary_part = "summary: -"
        owner = related_doc_owner_label(doc, owner_cache)
        lines.append(
            f"- {doc.doc_title} (id={doc.file_id}, created_at={format_related_doc_created_date(doc)}, "
            f"{format_search_score(doc)}{owner}, {summary_part})"
        )
    return lines


def format_page(
    page_slice: PageSlice,
    owner_cache: dict[UUID, str],
) -> str:
    """Format one page of file rows or search rows."""
    if len(page_slice.items) == 1:
        item = page_slice.items[0]
        if isinstance(item, RelatedDoc):
            return format_single_related_doc(item, owner_cache)
        return format_single_doc(item, owner_cache)

    if isinstance(page_slice.items[0], RelatedDoc):
        related_docs = [doc for doc in page_slice.items if isinstance(doc, RelatedDoc)]
        lines = format_related_docs_batch(related_docs, owner_cache)
    else:
        user_files = [f for f in page_slice.items if isinstance(f, UserFile)]
        lines = format_full_batch(user_files, owner_cache)

    span = f"{page_slice.start + 1}-{page_slice.end} of {page_slice.total}"
    header = f"Documents {span} (page {page_slice.page}/{page_slice.total_pages}):"
    return f"{header}\n" + "\n".join(lines)


async def format_and_commit_page(
    runtime: ToolRuntime[RuntimeContext],
    page_slice: PageSlice,
    user_storage: UserStorage | None,
) -> str:
    """Format a page and update total_tokens_spent in the runtime."""
    owner_cache = await resolve_owner_names(page_slice.items, user_storage)
    result = format_page(page_slice, owner_cache)

    async with runtime.context.lock:
        total_before = runtime.context.total_tokens_spent
        rag_tokens = count_tokens(result)
        runtime.context.total_tokens_spent += rag_tokens
        logger.info(
            "list_documents commit: page=%d items=%d output_tokens=%d total_tokens=%d->%d",
            page_slice.page,
            len(page_slice.items),
            rag_tokens,
            total_before,
            runtime.context.total_tokens_spent,
        )
    return result
