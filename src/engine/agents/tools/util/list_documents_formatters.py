import logging
from dataclasses import dataclass
from uuid import UUID

from src.engine.unified_logger import get_logger

from ....models.rag import RelatedDoc
from ....models.user import User
from ....models.user_file import UserFile
from ....storage.content_type_storage import ContentTypeStorage
from ....storage.user_storage import UserStorage

logger = get_logger("agents")


async def prefetch_category_catalog(
    org_id: UUID,
    content_type_storage: ContentTypeStorage,
) -> dict[UUID, str]:
    """Fetch all active content types for the org in one query; returns {id: 'name: description'}."""
    cts = await content_type_storage.list_by_org(org_id, include_inactive=True)
    catalog: dict[UUID, str] = {}
    for ct in cts:
        entry = ct.name
        if ct.description:
            entry += f": {ct.description}"
        catalog[ct.id] = entry
    return catalog


def format_categories_line(
    files: list[UserFile],
    catalog: dict[UUID, str],
) -> str:
    """Return a single '<categories>…</categories>' line for all distinct content types in files."""
    seen: dict[UUID, str] = {}
    for f in files:
        if f.content_type_id and f.content_type_id not in seen:
            label = catalog.get(f.content_type_id)
            if label:
                seen[f.content_type_id] = label
    if not seen:
        return ""
    parts = ", ".join(f"{cid}={label}" for cid, label in seen.items())
    return f"<categories>{parts}</categories>"


def format_related_docs_categories_line(docs: list[RelatedDoc]) -> str:
    """Return a '<categories>…</categories>' line from inline content type data on RelatedDoc."""
    seen: dict[UUID, str] = {}
    for doc in docs:
        if doc.content_type_id and doc.content_type_id not in seen:
            entry = doc.content_type_name or str(doc.content_type_id)
            if doc.content_type_description:
                entry += f": {doc.content_type_description}"
            seen[doc.content_type_id] = entry
    if not seen:
        return ""
    parts = ", ".join(f"{cid}={label}" for cid, label in seen.items())
    return f"<categories>{parts}</categories>"


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
    catalog: dict[UUID, str] | None = None,
) -> list[str]:
    lines = []
    for f in files:
        if f.rag_status == "indexed" and f.summary:
            summary_part = f'summary: "{f.summary}"'
        else:
            summary_part = "summary: -"
        owner = owner_label(f, owner_cache)
        comment_part = f", comment={f.comment!r}" if f.comment else ""
        cat_name = (catalog or {}).get(f.content_type_id) if f.content_type_id else None
        category_part = f", category={cat_name!r}" if cat_name else ""
        lines.append(
            f"- {f.original_filename} (id={f.id}, created_at={format_created_date(f)}, "
            f"rag={f.rag_status}, is_table={f.is_table}{owner}"
            f"{comment_part}{category_part}, "
            f"{summary_part})"
        )
    return lines


def format_single_doc(
    f: UserFile,
    owner_cache: dict[UUID, str] | None = None,
    catalog: dict[UUID, str] | None = None,
) -> str:
    """Format one file row with full detail."""
    summary_part = f'summary: "{f.summary}"' if f.rag_status == "indexed" and f.summary else "summary: -"
    owner = owner_label(f, owner_cache or {})
    comment_part = f", comment={f.comment!r}" if f.comment else ""
    cat_name = (catalog or {}).get(f.content_type_id) if f.content_type_id else None
    category_part = f", category={cat_name!r}" if cat_name else ""
    return (
        f"- {f.original_filename} (id={f.id}, created_at={format_created_date(f)}, "
        f"rag={f.rag_status}, is_table={f.is_table}{owner}, "
        f"size={f.file_size / 1_000_000:.2f}MB{comment_part}{category_part}, {summary_part})"
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
    """Format one search result row."""
    summary_part = f'summary: "{doc.summary}"' if doc.summary else "summary: -"
    owner = related_doc_owner_label(doc, owner_cache or {})
    comment_part = f", comment={doc.comment!r}" if doc.comment else ""
    category_part = f", category={doc.content_type_name!r}" if doc.content_type_name else ""
    return (
        f"- {doc.doc_title} (id={doc.file_id}, created_at={format_related_doc_created_date(doc)}, "
        f"{format_search_score(doc)}{owner}{comment_part}{category_part}, {summary_part})"
    )


def format_related_docs_batch(
    docs: list[RelatedDoc],
    owner_cache: dict[UUID, str],
) -> list[str]:
    lines = []
    for doc in docs:
        if doc.summary:
            summary_part = f'summary: "{doc.summary}"'
        else:
            summary_part = "summary: -"
        owner = related_doc_owner_label(doc, owner_cache)
        comment_part = f", comment={doc.comment!r}" if doc.comment else ""
        category_part = f", category={doc.content_type_name!r}" if doc.content_type_name else ""
        lines.append(
            f"- {doc.doc_title} (id={doc.file_id}, created_at={format_related_doc_created_date(doc)}, "
            f"{format_search_score(doc)}{owner}{comment_part}{category_part}, {summary_part})"
        )
    return lines


def format_page(
    page_slice: PageSlice,
    owner_cache: dict[UUID, str],
    catalog: dict[UUID, str] | None = None,
) -> str:
    """Format one page of file rows or search rows."""
    if len(page_slice.items) == 1:
        item = page_slice.items[0]
        if isinstance(item, RelatedDoc):
            return format_single_related_doc(item, owner_cache)
        return format_single_doc(item, owner_cache, catalog)

    if isinstance(page_slice.items[0], RelatedDoc):
        related_docs = [doc for doc in page_slice.items if isinstance(doc, RelatedDoc)]
        lines = format_related_docs_batch(related_docs, owner_cache)
    else:
        user_files = [f for f in page_slice.items if isinstance(f, UserFile)]
        lines = format_full_batch(user_files, owner_cache, catalog)

    span = f"{page_slice.start + 1}-{page_slice.end} of {page_slice.total}"
    header = f"Documents {span} (page {page_slice.page}/{page_slice.total_pages}):"
    return f"{header}\n" + "\n".join(lines)


async def format_page_with_owners(
    page_slice: PageSlice,
    user_storage: UserStorage | None,
    catalog: dict[UUID, str] | None = None,
) -> str:
    owner_cache = await resolve_owner_names(page_slice.items, user_storage)
    return format_page(page_slice, owner_cache, catalog)
