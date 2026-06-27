"""
Document Tools

Two LangChain tools for listing documents:

  list_documents — org-wide visibility: visible files in the organization.
  list_own_documents — documents owned by the direct caller or mentioned user.

Visibility modes
----------------
  "list"   — is_public OR caller-owned OR is_admin; optional user_id narrows owner
  "own"    — direct: caller-owned; mention: called-user-owned, public only if caller differs

Service lifecycle: call init_document_service(storage) once during engine startup.
"""

from src.engine.unified_logger import get_logger
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from ...constants import IMAGE_TYPES
from ...models.rag import RelatedDoc
from ...models.user_file import UserFile
from ..runtime import RuntimeContext
from ...services.rag_service import RAGService
from ...storage.chat_storage import ChatStorage
from ...storage.content_type_storage import ContentTypeStorage
from ...storage.user_file_storage import UserFileStorage
from ...storage.user_storage import UserStorage
from ...utils.token_counter import count_tokens
from .util.list_documents_formatters import (
    PageSlice,
    format_page_with_owners,
    format_categories_line,
    format_related_docs_categories_line,
    format_single_doc,
    prefetch_category_catalog,
    page_files,
    resolve_owner_names,
)

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"
_LISTING_BLOCKED_MESSAGE = (
    "[DOCUMENT LISTING IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
    "USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO LIST DOCUMENTS]"
)

_PAGE_SIZE = 30
_MAX_RESULTS = 180

_user_file_storage: Optional[UserFileStorage] = None
_user_storage: Optional[UserStorage] = None
_rag_service: Optional[RAGService] = None
_chat_storage: Optional[ChatStorage] = None
_content_type_storage: Optional[ContentTypeStorage] = None


# =================================================================
# Scope / config helpers
# =================================================================

@dataclass(frozen=True)
class DocumentToolScope:
    # Scope is the resolved caller/callee/org context that determines which
    # documents a tool call may list or search.
    tool_name: str
    caller_user_id: UUID
    owner_user_id: UUID
    callee_user_id: UUID
    org_id: UUID
    mention_mode: bool
    is_admin: bool
    own_only: bool
    # Explicit owner filter: callee for own_only, LLM-supplied user_id for list_documents.
    owner_filter_user_id: Optional[UUID]
    chat_id: Optional[UUID] = None


class BaseListDocumentsInput(BaseModel):
    name_query: str = Field(
        default="",
        description=(
            "Keyword search query on document filenames and keywords in summaries. "
            "Use keywords from the expected filename. Omit to skip."
        ),
    )
    summary_query: str = Field(
        default="",
        description=(
            "Vector search query on document summaries. "
            "Write a description of the document content, expected user upload comment, "
            "or admin-defined category. Category meaning has higher priority than summary meaning. "
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
    category_id: Optional[str] = Field(
        default=None,
        description=(
            "UUID of a content type (category) to filter documents by. "
            "Use list_categories to look up category IDs. "
            "Ignored when file_id is provided."
        ),
    )
    page: int = Field(
        default=1,
        description="Page number for paginated listing or search results (default 1). Page size is 30. Ignored when file_id is provided.",
    )


class ListDocumentsInput(BaseListDocumentsInput):
    user_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional UUID of a document owner to filter visible organization documents. "
            "Admins can see private documents for that owner; non-admins see public docs "
            "plus their own private docs."
        ),
    )


class ListOwnDocumentsInput(BaseListDocumentsInput):
    pass


def init_document_service(
    storage: UserFileStorage,
    rag_service: Optional[RAGService] = None,
    user_storage: Optional[UserStorage] = None,
    chat_storage: Optional[ChatStorage] = None,
    content_type_storage: Optional[ContentTypeStorage] = None,
) -> None:
    """Set the shared storage instances for all document tool calls."""
    global _user_file_storage, _rag_service, _user_storage, _chat_storage, _content_type_storage
    _user_file_storage = storage
    _rag_service = rag_service
    _user_storage = user_storage
    _chat_storage = chat_storage
    _content_type_storage = content_type_storage
    logger.info("Document tool storage initialized")


def _is_image_file(f: UserFile) -> bool:
    """Return True when file metadata points to an image attachment."""
    file_type = (f.file_type or "").lower()
    if file_type in IMAGE_TYPES:
        return True
    filename = (f.original_filename or "").lower()
    return any(filename.endswith(f".{ext}") for ext in IMAGE_TYPES)



def _resolve_tool_identity(configurable: dict) -> tuple[str, str, bool]:
    """Resolve active owner, org, and mention visibility from runnable config."""
    caller_user_id = configurable.get("caller_user_id", "")
    org_id = configurable.get("org_id", "")
    # callee_user_id == caller_user_id in direct calls (always set by executor).
    callee_user_id = configurable.get("callee_user_id", "")
    mention_mode = bool(callee_user_id and callee_user_id != caller_user_id)
    return callee_user_id or caller_user_id, org_id, mention_mode


def _parse_scope_uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        raise ValueError(f"malformed UUID in scope field '{field}': {value!r}")


def _build_scope(
    config: RunnableConfig,
    own_only: bool,
    user_id: Optional[str] = None,
) -> DocumentToolScope:
    """Build the immutable visibility scope from injected RunnableConfig.

    Required fields:
    - configurable.org_id: organization whose documents may be listed
    - configurable.caller_user_id: direct caller identity
    - configurable.callee_user_id: document owner (equals caller in direct calls, set by executor)
    """
    configurable = config.get("configurable", {})
    caller_user_id_str = configurable.get("caller_user_id", "")
    owner_user_id_str, org_id_str, mention_mode = _resolve_tool_identity(configurable)
    callee_user_id_str = configurable.get("callee_user_id") or caller_user_id_str
    callee_uuid = _parse_scope_uuid(callee_user_id_str, "callee_user_id")
    if own_only:
        owner_filter_user_id: Optional[UUID] = callee_uuid
    else:
        owner_filter_user_id = _parse_scope_uuid(user_id.strip(), "user_id") if user_id and user_id.strip() else None
    chat_id_str = configurable.get("chat_id")
    chat_uuid: Optional[UUID] = None
    if chat_id_str:
        try:
            chat_uuid = UUID(chat_id_str)
        except ValueError:
            pass
    return DocumentToolScope(
        tool_name="list_own_documents" if own_only else "list_documents",
        caller_user_id=_parse_scope_uuid(caller_user_id_str, "caller_user_id"),
        owner_user_id=_parse_scope_uuid(owner_user_id_str, "owner_user_id"),
        callee_user_id=callee_uuid,
        org_id=_parse_scope_uuid(org_id_str, "org_id"),
        mention_mode=mention_mode,
        is_admin=bool(configurable.get("is_admin", False)),
        own_only=own_only,
        owner_filter_user_id=owner_filter_user_id,
        chat_id=chat_uuid,
    )


# =================================================================
# Search / list implementation helpers
# =================================================================


async def _get_chat_attachment_ids(chat_id: Optional[UUID]) -> frozenset[UUID]:
    """Return the set of file IDs attached to the given chat, or empty set."""
    if chat_id is None or _chat_storage is None:
        return frozenset()
    try:
        return frozenset(await _chat_storage.get_attachments(chat_id))
    except Exception:
        return frozenset()


async def _get_single_document(file_id: str, scope: DocumentToolScope, chat_attachment_ids: frozenset[UUID]) -> str:
    """Fetch one file directly and enforce single-document visibility locally."""
    f = await _user_file_storage.get_by_id(UUID(file_id.strip()))
    if f is None:
        return f"Document {file_id} not found."
    if f.id in chat_attachment_ids:
        visible = True
    elif scope.own_only:
        visible = (
            f.user_id == scope.owner_user_id
            and (not scope.mention_mode or f.is_public)
            and not _is_image_file(f)
        )
    else:
        visible = (
            (scope.owner_filter_user_id is None or f.user_id == scope.owner_filter_user_id)
            and (f.is_public or f.user_id == scope.caller_user_id or scope.is_admin)
            and not _is_image_file(f)
        )
    if not visible:
        return f"Document {file_id} not found or not visible to you."
    owner_cache = await resolve_owner_names([f], _user_storage)
    catalog: dict[UUID, str] = {}
    if f.content_type_id and _content_type_storage:
        ct = await _content_type_storage.get_by_id(f.content_type_id)
        if ct:
            entry = ct.name
            if ct.description:
                entry += f": {ct.description}"
            catalog[f.content_type_id] = entry
    return format_single_doc(f, owner_cache, catalog)


async def _search_scoped(
    scope: DocumentToolScope,
    name_query: str,
    summary_query: str,
    filter_content_type_id: Optional[str] = None,
) -> tuple[list[RelatedDoc], Optional[str]]:
    """Search visible docs in SQL and return ranked RelatedDoc rows as-is."""
    if _rag_service is None:
        return [], f"{scope.tool_name}: search unavailable (RAG service not initialized)."

    matched_docs: list[RelatedDoc] = []
    seen_ids: set[str] = set()
    org_id = str(scope.org_id)
    caller_user_id = str(scope.caller_user_id)
    filter_user_id = str(scope.owner_filter_user_id) if scope.owner_filter_user_id else None

    def remember_matches(docs: list[RelatedDoc]) -> None:
        for doc in docs:
            doc_id = str(doc.file_id)
            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)
            matched_docs.append(doc)

    async def search(query: str, method: str) -> list[RelatedDoc]:
        docs = await _rag_service.find_docs(
            org_id=org_id,
            user_id=caller_user_id,
            query=query,
            top_k=_MAX_RESULTS,
            is_admin=scope.is_admin,
            filter_user_id=filter_user_id,
            exclude_images=True,
            search_mode=method,
            filter_content_type_id=filter_content_type_id,
        )
        logger.info(
            "%s search filter: method=%s query=%r returned=%d filter_user=%s filter_content_type=%s",
            scope.tool_name, method, query, len(docs), filter_user_id, filter_content_type_id,
        )
        return docs

    if name_query:
        docs: list[RelatedDoc] = await search(name_query, "concrete")
        remember_matches(docs)

    if summary_query:
        docs: list[RelatedDoc] = await search(summary_query, "abstract")
        remember_matches(docs)

    return matched_docs[:_MAX_RESULTS], None


def _filter_listed_files(
    files: list[UserFile],
    scope: DocumentToolScope,
    chat_attachment_ids: frozenset[UUID] = frozenset(),
) -> tuple[list[UserFile], int, int]:
    """Apply no-search listing visibility rules to storage-loaded file rows.

    Returns (visible_files, hidden_private_count, not_indexed_count) where
    hidden_private_count is the number of files hidden due to private visibility
    and not_indexed_count is the number of files excluded because rag_status='not_indexed'.
    Files attached to the originating chat are always visible.
    """
    if scope.own_only:
        owner_matched = [
            f for f in files
            if (f.user_id == scope.owner_user_id or f.id in chat_attachment_ids)
            and not _is_image_file(f)
        ]
        not_indexed_count = sum(1 for f in owner_matched if f.rag_status == "not_indexed")
        indexed = [f for f in owner_matched if f.rag_status != "not_indexed"]
        visible = [
            f for f in indexed
            if f.id in chat_attachment_ids or not scope.mention_mode or f.is_public or scope.is_admin
        ]
        hidden = len(indexed) - len(visible)
        return visible, hidden, not_indexed_count

    # Organization mode includes public docs, caller-owned private docs, and admin-visible private docs.
    owner_matched = [
        f for f in files
        if (scope.owner_filter_user_id is None or f.user_id == scope.owner_filter_user_id or f.id in chat_attachment_ids)
        and not _is_image_file(f)
    ]
    not_indexed_count = sum(1 for f in owner_matched if f.rag_status == "not_indexed")
    indexed = [f for f in owner_matched if f.rag_status != "not_indexed"]
    visible = [
        f for f in indexed
        if f.id in chat_attachment_ids or f.is_public or f.user_id == scope.caller_user_id or scope.is_admin
    ]
    hidden = len(indexed) - len(visible)
    return visible, hidden, not_indexed_count


# =================================================================
# Tool implementation
# =================================================================

async def _list_documents_impl(
    config: RunnableConfig,
    runtime: ToolRuntime[RuntimeContext],
    own_only: bool,
    name_query: str = "",
    summary_query: str = "",
    file_id: Optional[str] = None,
    page: int = 1,
    user_id: Optional[str] = None,
    category_id: Optional[str] = None,
) -> str:
    """Shared implementation for own/global document listing tools."""
    tool_name = "list_own_documents" if own_only else "list_documents"
    raw_name_query = name_query
    raw_summary_query = summary_query
    logger.info("tool %s: name_query=%r, summary_query=%r, file_id=%r, page=%d, user_id=%r, category_id=%r",
                tool_name, name_query, summary_query, file_id, page, user_id, category_id)

    if _user_file_storage is None:
        logger.error("%s: storage not initialized, call init_document_service() at startup", tool_name)
        return f"{tool_name} unavailable: storage not initialized."

    try:
        # --- Stage 1: Build scope ---
        # Resolve caller/callee identity and visibility rules from injected config.
        scope = _build_scope(config, own_only, user_id)
        configurable = (config or {}).get("configurable", {})

        logger.info(
            "%s identity: caller_user_id=%s callee_user_id=%s org_id=%s is_admin=%s invocation=%s",
            tool_name, configurable.get("caller_user_id", ""), configurable.get("callee_user_id", ""),
            configurable.get("org_id", ""), bool(configurable.get("is_admin", False)), configurable.get("invocation_kind", ""))
        logger.info(
            "%s access: caller=%s callee=%s org=%s mention_mode=%s is_admin=%s chat_id=%s",
            tool_name,
            scope.caller_user_id,
            scope.callee_user_id,
            scope.org_id,
            scope.mention_mode,
            scope.is_admin,
            scope.chat_id,
        )
        chat_attachment_ids = await _get_chat_attachment_ids(scope.chat_id)
        name_query = name_query.strip()
        summary_query = summary_query.strip()

        # --- Stage 2: Snapshot token budget ---
        # Capture current budget state before doing any work so we can log the delta at the end.
        path = "single" if file_id and file_id.strip() else ("search" if name_query or summary_query else "list")
        async with runtime.context.lock:
            tokens_before = runtime.context.total_tokens_spent
            token_cap = runtime.context.critical_tokens_cap

        logger.info(
            "%s start: path=%s name_query=%r summary_query=%r file_id=%r page=%d owner_filter=%s caller=%s owner=%s public_only_owner=%s is_admin=%s total_tokens=%d/%d",
            tool_name,
            path,
            raw_name_query,
            raw_summary_query,
            file_id,
            page,
            scope.owner_filter_user_id,
            scope.caller_user_id,
            scope.owner_user_id,
            scope.mention_mode,
            scope.is_admin,
            tokens_before,
            token_cap,
        )

        # --- Stage 3: Single-document fast path ---
        # Bypasses pagination, deduplication, and summary budgets entirely.
        if file_id and file_id.strip():
            result = await _get_single_document(file_id, scope, chat_attachment_ids)
            logger.info(
                "%s done: path=single file_id=%s output_tokens=%d",
                tool_name, file_id, count_tokens(result))
            return result

        # --- Stage 4: Fetch candidates (search or list) ---
        # Search path: name_query hits SQL full-text, summary_query hits pgvector.
        # List path: loads all org files then filters by visibility rules locally.
        hidden_count = 0
        not_indexed_count = 0
        category_uuid: Optional[UUID] = None
        if category_id and category_id.strip():
            try:
                category_uuid = UUID(category_id.strip())
            except ValueError:
                return f"Invalid UUID for category_id: {category_id!r}"
        filter_content_type_str = str(category_uuid) if category_uuid else None
        if name_query or summary_query:
            files, error = await _search_scoped(
                scope,
                name_query,
                summary_query,
                filter_content_type_id=filter_content_type_str,
            )
            if error:
                return error
            empty_message = "No documents matched your query."
            raw_count = len(files)
            visible_count = len(files)
        else:
            # List all files, then apply visibility + indexed-only filter.
            all_files = await _user_file_storage.list_by_org(scope.org_id, content_type_id=category_uuid)
            files, hidden_count, not_indexed_count = _filter_listed_files(all_files, scope, chat_attachment_ids)
            empty_message = "No documents in your scope."
            raw_count = len(all_files)
            visible_count = len(files)

        logger.info(
            "%s candidates: path=%s raw=%d visible=%d",
            tool_name, path, raw_count, visible_count,
        )

        if not files:
            result = empty_message
            if not_indexed_count:
                result = f"{not_indexed_count} document(s) excluded (not indexed).\n" + result
            if hidden_count:
                result = f"Some document(s) are private and not accessible to caller.\n" + result
            logger.info("%s done: path=%s result=empty hidden=%d", tool_name, path, hidden_count)
            return result

        async with runtime.context.lock:
            tokens_now = runtime.context.total_tokens_spent
            cap = runtime.context.critical_tokens_cap
        if tokens_now >= cap:
            logger.info(
                "%s blocked: total_tokens_spent=%d cap=%d candidates=%d",
                tool_name, tokens_now, cap, len(files),
            )
            result = _LISTING_BLOCKED_MESSAGE
            if not_indexed_count:
                result = f"{not_indexed_count} document(s) excluded (not indexed).\n" + result
            if hidden_count:
                result = f"Some document(s) are private and not accessible to caller.\n" + result
            return result

        page_slice: PageSlice = page_files(files, page, _PAGE_SIZE)
        logger.info(
            "%s page: page=%d/%d span=%d-%d total=%d page_items=%d",
            tool_name,
            page_slice.page,
            page_slice.total_pages,
            page_slice.start + 1,
            page_slice.end,
            page_slice.total,
            len(page_slice.items),
        )
        catalog: dict[UUID, str] = {}
        if _content_type_storage:
            catalog = await prefetch_category_catalog(scope.org_id, _content_type_storage)
        result = await format_page_with_owners(
            page_slice,
            _user_storage,
            catalog,
        )
        # UserFile: prepend category legend for the page.
        user_file_items = [f for f in page_slice.items if isinstance(f, UserFile)]
        if catalog:
            categories_line = format_categories_line(user_file_items, catalog)
            if categories_line:
                result = categories_line + "\n" + result
        # RelatedDoc: content type name/description are JOINed inline by the SQL function, no storage needed.
        related_doc_items = [f for f in page_slice.items if isinstance(f, RelatedDoc)]
        if related_doc_items:
            categories_line = format_related_docs_categories_line(related_doc_items)
            if categories_line:
                result = categories_line + "\n" + result
        if not_indexed_count:
            result = f"{not_indexed_count} document(s) excluded (not indexed).\n" + result
        if hidden_count:
            result = f"Some document(s) are private and not accessible to caller.\n" + result

        # --- Stage 7: Commit output tokens to global budget ---
        # If the formatted result would exceed the agent's critical token cap,
        # discard it and return a compact blocked message instead.
        spent = count_tokens(result)
        blocked = await runtime.context.try_commit(spent, _LISTING_BLOCKED_MESSAGE)
        if blocked:
            logger.info(
                "%s blocked after format: total_tokens_spent=%d >= %d",
                tool_name, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,)
            return blocked
        async with runtime.context.lock:
            tokens_after = runtime.context.total_tokens_spent
        logger.info(
            "%s done: path=%s hidden=%d output_tokens=%d total_tokens=%d->%d",
            tool_name,
            path,
            hidden_count,
            count_tokens(result),
            tokens_before,
            tokens_after,
        )
        return result

    except Exception as e:
        logger.error(f"{tool_name} failed: {e}", exc_info=True)
        if isinstance(e, ValueError):
            return f"Invalid UUID: {e}"
        return _TOOL_ERROR_RESULT


async def _list_documents_async(
    config: RunnableConfig,
    runtime: ToolRuntime[RuntimeContext],
    name_query: str = "",
    summary_query: str = "",
    file_id: Optional[str] = None,
    page: int = 1,
    user_id: Optional[str] = None,
    category_id: Optional[str] = None,
) -> str:
    """LangChain coroutine wrapper for organization document listing."""
    return await _list_documents_impl(config, runtime, own_only=False,
                                      name_query=name_query, summary_query=summary_query,
                                      file_id=file_id, page=page, user_id=user_id,
                                      category_id=category_id)


async def _list_own_documents_async(
    config: RunnableConfig,
    runtime: ToolRuntime[RuntimeContext],
    name_query: str = "",
    summary_query: str = "",
    file_id: Optional[str] = None,
    page: int = 1,
    category_id: Optional[str] = None,
) -> str:
    """LangChain coroutine wrapper for own/mentioned-owner document listing."""
    return await _list_documents_impl(config, runtime, own_only=True,
                                      name_query=name_query, summary_query=summary_query,
                                      file_id=file_id, page=page, category_id=category_id)


# =================================================================
# Tool creation
# =================================================================

list_documents = StructuredTool.from_function(
    coroutine=_list_documents_async,
    name="list_documents",
    description=(
        "List visible organization documents. Optionally filter by owner user_id. "
        "Use name_query or summary_query to search; omit both for paginated listing. "
        "Use file_id to fetch full info for a single document bypassing all budgets. "
        "Use category_id to filter documents by content type; use list_categories to find category IDs. "
        "Use page to navigate pages (30 items per page, ordered by creation date). "
        "Search results include rank_score where lower is better and tsv_score where higher is better. "
        "Results may include comment, the user's direct upload description, and category, "
        "the admin-defined organization-wide document class. Category is optional but, "
        "when present, its meaning is a very high-priority signal and outranks summary meaning."
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
        "Use page to navigate pages (30 items per page, ordered by creation date). "
        "Search results include rank_score where lower is better and tsv_score where higher is better. "
        "Results may include comment, the user's direct upload description, and category, "
        "the admin-defined organization-wide document class. Category is optional but, "
        "when present, its meaning is a very high-priority signal and outranks summary meaning."
    ),
    args_schema=ListOwnDocumentsInput,
)
