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
from ...storage.user_file_storage import UserFileStorage
from ...storage.user_storage import UserStorage
from ...utils.token_counter import count_tokens
from .util.list_documents_dedupe import dedupe_and_page
from .util.list_documents_formatters import (
    format_and_commit_page,
    format_single_doc,
    resolve_owner_names,
)

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_PAGE_SIZE = 30
_MAX_RESULTS = 180

# Total token budget for document summaries across the whole agent run.
_SUMMARY_TOKENS_BUDGET = 2000

_user_file_storage: Optional[UserFileStorage] = None
_user_storage: Optional[UserStorage] = None
_rag_service: Optional[RAGService] = None
_chat_storage: Optional[ChatStorage] = None


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
        description="Page number for paginated listing (default 1). Page size is 30. Ignored when file_id or search queries are provided.",
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
) -> None:
    """Set the shared storage instances for all document tool calls."""
    global _user_file_storage, _rag_service, _user_storage, _chat_storage
    _user_file_storage = storage
    _rag_service = rag_service
    _user_storage = user_storage
    _chat_storage = chat_storage
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
    return format_single_doc(f, owner_cache)


async def _search_scoped(
    scope: DocumentToolScope,
    name_query: str,
    summary_query: str,
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
        )
        logger.info(
            "%s search filter: method=%s query=%r returned=%d filter_user=%s",
            scope.tool_name, method, query, len(docs), filter_user_id,
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
) -> str:
    """Shared implementation for own/global document listing tools."""
    tool_name = "list_own_documents" if own_only else "list_documents"
    raw_name_query = name_query
    raw_summary_query = summary_query
    logger.info(
        "tool %s: name_query=%r, summary_query=%r, file_id=%r, page=%d, user_id=%r",
        tool_name, name_query, summary_query, file_id, page, user_id,
    )

    if _user_file_storage is None:
        logger.error("%s: storage not initialized, call init_document_service() at startup", tool_name)
        return f"{tool_name} unavailable: storage not initialized."

    try:
        scope = _build_scope(config, own_only, user_id)
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

        path = "single" if file_id and file_id.strip() else ("search" if name_query or summary_query else "list")
        async with runtime.context.lock:
            summary_before = runtime.context.list_documents_runtime_data.spent_summary_tokens
            tokens_before = runtime.context.total_tokens_spent
            token_cap = runtime.context.critical_tokens_cap
        logger.info(
            "%s start: path=%s name_query=%r summary_query=%r file_id=%r page=%d owner_filter=%s caller=%s owner=%s mention_mode=%s is_admin=%s summary_tokens=%d/%d total_tokens=%d/%d",
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
            summary_before,
            _SUMMARY_TOKENS_BUDGET,
            tokens_before,
            token_cap,
        )

        # --- Single-document lookup: bypasses pagination, deduplication, and budgets ---
        if file_id and file_id.strip():
            result = await _get_single_document(file_id, scope, chat_attachment_ids)
            logger.info(
                "%s done: path=single file_id=%s output_tokens=%d",
                tool_name, file_id, count_tokens(result),
            )
            return result

        # --- Search by queries ---
        hidden_count = 0
        not_indexed_count = 0
        if name_query or summary_query:
            files, error = await _search_scoped(
                scope,
                name_query,
                summary_query,
            )
            if error:
                return error
            empty_message = "No documents matched your query."
            compact_on_budget_exhausted = False
            raw_count = len(files)
            visible_count = len(files)
        else:
            # --- Or list all files without queries ---
            all_files = await _user_file_storage.list_by_org(scope.org_id)
            files, hidden_count, not_indexed_count = _filter_listed_files(all_files, scope, chat_attachment_ids)
            empty_message = "No documents in your scope."
            compact_on_budget_exhausted = True
            raw_count = len(all_files)
            visible_count = len(files)

        logger.info(
            "%s candidates: path=%s raw=%d visible=%d compact_on_budget=%s",
            tool_name, path, raw_count, visible_count, compact_on_budget_exhausted,
        )

        dedupe_state, page_slice, shortcut_result = await dedupe_and_page(
            runtime,
            files,
            page,
            _PAGE_SIZE,
            scope.tool_name,
            empty_message,
        )
        # shortcut_result is set when dedupe_and_page can answer without full formatting
        # (empty list, budget exhausted, or all items already seen in a previous call).
        if shortcut_result is not None:
            if not_indexed_count:
                shortcut_result = f"{not_indexed_count} document(s) excluded (not indexed).\n" + shortcut_result
            if hidden_count:
                shortcut_result = f"Some document(s) are private and not accessible to caller.\n" + shortcut_result
            logger.info(
                "%s done: path=%s result=shortcut hidden=%d output_tokens=%d deduped=%s",
                tool_name, path, hidden_count, count_tokens(shortcut_result), dedupe_state.deduplicated_across_runs,
            )
            return shortcut_result

        logger.info(
            "%s page: page=%d/%d span=%d-%d total=%d page_items=%d deduped=%s",
            tool_name,
            page_slice.page,
            page_slice.total_pages,
            page_slice.start + 1,
            page_slice.end,
            page_slice.total,
            len(page_slice.items),
            dedupe_state.deduplicated_across_runs,
        )
        result = await format_and_commit_page(
            runtime,
            page_slice,
            _user_storage,
            dedupe_state,
            compact_on_budget_exhausted,
            _SUMMARY_TOKENS_BUDGET,
        )
        if not_indexed_count:
            result = f"{not_indexed_count} document(s) excluded (not indexed).\n" + result
        if hidden_count:
            result = f"Some document(s) are private and not accessible to caller.\n" + result
        async with runtime.context.lock:
            summary_after = runtime.context.list_documents_runtime_data.spent_summary_tokens
            tokens_after = runtime.context.total_tokens_spent
        logger.info(
            "%s done: path=%s hidden=%d output_tokens=%d summary_tokens=%d->%d total_tokens=%d->%d",
            tool_name,
            path,
            hidden_count,
            count_tokens(result),
            summary_before,
            summary_after,
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
) -> str:
    """LangChain coroutine wrapper for organization document listing."""
    return await _list_documents_impl(config, runtime, own_only=False,
                                      name_query=name_query, summary_query=summary_query,
                                      file_id=file_id, page=page, user_id=user_id)


async def _list_own_documents_async(
    config: RunnableConfig,
    runtime: ToolRuntime[RuntimeContext],
    name_query: str = "",
    summary_query: str = "",
    file_id: Optional[str] = None,
    page: int = 1,
) -> str:
    """LangChain coroutine wrapper for own/mentioned-owner document listing."""
    return await _list_documents_impl(config, runtime, own_only=True,
                                      name_query=name_query, summary_query=summary_query,
                                      file_id=file_id, page=page)


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
        "Use page to navigate pages (30 items per page, ordered by creation date)."
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
        "Use page to navigate pages (30 items per page, ordered by creation date)."
    ),
    args_schema=ListOwnDocumentsInput,
)
