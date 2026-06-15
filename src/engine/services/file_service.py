"""
File Service

Business logic for file upload/download/management.
Uses StorageAdapter for binary data, UserFileStorage for metadata.
"""
import asyncio
import hashlib

from src.engine.unified_logger import get_logger
from typing import Optional, List
from uuid import UUID

from ..models.user_file import UserFile
from ..storage.user_file_storage import UserFileStorage
from ..storage.storage_adapter import StorageAdapter
from ..constants import (
    ALLOWED_FILE_TYPES,
    CONTENT_TYPES,
    IMAGE_TYPES,
    MAX_FILE_SIZE,
    RAG_COMPATIBLE_TYPES,
    TABLE_EXTENSIONS,
)

# Types that index_for_rag accepts. Images are allowed so invoice images can be
# summarized by the vision LLM during ingest (only when is_invoice=True); other
# RAG-compatible types are chunked/embedded as usual.
RAG_INDEXABLE_TYPES = RAG_COMPATIBLE_TYPES | IMAGE_TYPES

logger = get_logger("services")

class FileService:

    def __init__(
        self,
        file_storage: UserFileStorage,
        storage_adapter: StorageAdapter,
        max_file_size: int = MAX_FILE_SIZE,
        allowed_types: set = None,
        content_type_storage=None,
        org_storage=None,
    ):
        self.file_storage = file_storage
        self.adapter = storage_adapter
        self.max_file_size = max_file_size
        self.allowed_types = ALLOWED_FILE_TYPES or allowed_types
        # Optional: content-type catalog + org policy for the upload comment field (migration 051).
        self.content_type_storage = content_type_storage
        self.org_storage = org_storage

    async def _resolve_comment(
        self,
        org_id: UUID,
        comment: Optional[str],
        content_type_id: Optional[UUID],
    ) -> "tuple[Optional[str], Optional[UUID]]":
        """Apply the org content-type policy at upload time.

        - content_type_id set -> must be an active type of this org; the comment is
          forced to the type's description (server-canonical invariant).
        - content_type_id None -> "Вручную" (free text), allowed only when the org
          permits manual comments (organizations.file_manual_comment_allowed).
        Returns (comment, content_type_id) to persist. Raises ValueError on violation.
        """
        if content_type_id is not None:
            if self.content_type_storage is None:
                raise ValueError("Content types are not available")
            ct = await self.content_type_storage.get_by_id(content_type_id)
            if ct is None or not ct.is_active or ct.org_id != org_id:
                raise ValueError("Invalid or inactive content type")
            return ct.description, content_type_id

        manual_allowed = True
        if self.org_storage is not None:
            org = await self.org_storage.get_by_id(org_id)
            if org is not None:
                manual_allowed = org.file_manual_comment_allowed
        if not manual_allowed:
            raise ValueError(
                "A content type must be selected (free-form comments are disabled for this organization)"
            )
        return (comment or None), None

    def _hash_bytes(self, payload: bytes) -> str:
        """
        Вычислить SHA-256 хеш бинарного содержимого файла.

        Используется для детекции дубликатов: два файла с одинаковым
        хешем имеют идентичное содержимое.

        Args:
            payload: бинарное содержимое файла

        Returns:
            SHA-256 hex-дайджест (64 символа)
        """
        return hashlib.sha256(payload).hexdigest()

    async def upload(
        self,
        org_id: UUID,
        user_id: UUID,
        uploaded_by_user_id: UUID,
        filename: str,
        data: bytes,
        is_public: bool = False,
        folder_id: Optional[UUID] = None,
        comment: Optional[str] = None,
        content_type_id: Optional[UUID] = None,
    ) -> UserFile:
        """
        Upload a file for an employee.

        Args:
            org_id: organization
            user_id: employee who owns the file
            uploaded_by_user_id: manager who uploads
            filename: original filename
            data: file binary content
        """
        # Validate file type
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in self.allowed_types:
            raise ValueError(f"File type '{ext}' not allowed. Allowed: {self.allowed_types}")

        # Validate file size
        if len(data) > self.max_file_size:
            raise ValueError(f"File too large: {len(data)} bytes (max {self.max_file_size})")

        # Resolve comment + content type per org policy (raises ValueError on violation).
        resolved_comment, resolved_content_type_id = await self._resolve_comment(
            org_id, comment, content_type_id,
        )

        # Вычислить SHA-256 хеш содержимого файла
        content_hash = self._hash_bytes(data)

        # Проверить наличие дубликата для данного пользователя по хешу.
        # Дубликат определяется как активный файл того же владельца
        # с идентичным содержимым (одинаковый content_hash).
        duplicate = await self.file_storage.find_duplicate(user_id, content_hash)
        if duplicate:
            logger.info(
                f"Duplicate upload attempted for user {user_id}: "
                f"'{filename}' matches '{duplicate.original_filename}' "
                f"(id={duplicate.id}); returning existing file"
            )
            return duplicate

        # Detect tabular content by file extension
        is_table = ext in TABLE_EXTENSIONS

        # Create metadata record.
        # rag_status is explicitly "not_indexed": upload no longer auto-enqueues
        # RAG indexing — owner must opt in via FileService.index_for_rag.
        # folder_id is trusted: routes call folder_service.verify_folder_owner before upload.
        file_record = UserFile(
            user_id=user_id,
            org_id=org_id,
            uploaded_by_user_id=uploaded_by_user_id,
            original_filename=filename,
            file_type=ext,
            file_size=len(data),
            content_hash=content_hash,
            is_public=is_public,
            is_table=is_table,
            rag_status="not_indexed",
            folder_id=folder_id,
            comment=resolved_comment,
            content_type_id=resolved_content_type_id,
        )

        # Generate storage key: {org_id}/{user_id}/{file_id}.{ext}
        file_record.storage_key = f"{org_id}/{user_id}/{file_record.id}.{ext}"

        # Save binary data via adapter
        await self.adapter.save(file_record.storage_key, data, CONTENT_TYPES.get(ext, "application/octet-stream"))

        # Save metadata to DB
        created = await self.file_storage.create(file_record)
        logger.info(
            f"Uploaded file '{filename}' for user {user_id} "
            f"(key={created.storage_key}, size={len(data)})"
        )
        return created

    async def index_for_rag(
        self,
        file_id: UUID,
        requesting_user_id: UUID,
        requesting_org_id: UUID | None = None,
        requesting_is_admin: bool = False,
        is_invoice: bool = False,
    ) -> tuple[UserFile, "asyncio.Future | None"]:
        """Owner/admin-initiated: enqueue file for RAG indexing.

        Idempotent: if already pending/indexing/indexed, returns the file unchanged
        without re-enqueuing.

        Args:
            file_id: file to index
            requesting_user_id: file owner, or an org admin when indexing another user's file.
            requesting_org_id: required for admin cross-owner indexing; must match the file org.
            requesting_is_admin: whether the requesting user is an org admin.
            is_invoice: when True, an image file is summarized by the vision LLM during
                ingest (invoice images only). Non-invoice images are accepted but skip the
                summary to avoid wasting an LLM call.

        Raises:
            FileNotFoundError: file does not exist or is inactive.
            PermissionError: requester is neither the owner nor a same-org admin.
            ValueError: file type is not indexable.
        """
        file = await self.file_storage.get_by_id(file_id)
        if file is None or not file.is_active:
            raise FileNotFoundError(f"File {file_id} not found")

        is_owner = file.user_id == requesting_user_id
        is_same_org_admin = (
            requesting_is_admin
            and requesting_org_id is not None
            and file.org_id == requesting_org_id
        )
        if not (is_owner or is_same_org_admin):
            raise PermissionError("Only the file owner or an organization admin can index it for RAG")

        if file.file_type not in RAG_INDEXABLE_TYPES:
            raise ValueError(f"File type '{file.file_type}' is not supported by RAG")
        if file.rag_status == "indexed":
            logger.info(
                f"File {file.id} already ingested for RAG; skipping enqueue"
            )
            return file, None
        if file.rag_status in ("pending", "indexing"):
            return file, None  # idempotent — already in pipeline or done

        # Re-read bytes from storage to enqueue (upload didn't keep them in memory)
        data = await self.adapter.read(file.storage_key)

        # Persist status transition first so concurrent calls observe pending
        # and short-circuit via the idempotency guard above.
        updated = await self.file_storage.update_rag_status(
            file_id=file.id,
            rag_status="pending",
            rag_error=None,
            indexed_at=None,
        )
        if updated is not None:
            file = updated

        # Submit to ingest queue. Lazy import keeps the FileService unit-testable
        # without spinning up the ThreadPoolExecutor at import time.
        # If submit itself fails (e.g. event loop gone, executor shut down during
        # app shutdown), revert status to 'failed' so the file isn't permanently
        # stuck at 'pending' (idempotency guard would otherwise block retry).
        from ..tasks.ingest_queue import ingest_queue
        try:
            future: asyncio.Future = ingest_queue.submit(
                file_id=file.id,
                org_id=str(file.org_id),
                user_id=str(file.user_id),
                filename=file.original_filename,
                data=data,
                is_invoice=is_invoice,
            )
        except Exception as e:
            logger.error(
                f"ingest_queue.submit failed for file {file.id}: {e}",
                exc_info=True,
            )
            await self.file_storage.update_rag_status(
                file_id=file.id,
                rag_status="failed",
                rag_error=f"submit failed: {e}",
                indexed_at=None,
            )
            raise
        logger.info(
            f"Enqueued file {file.id} for RAG indexing "
            f"(requester={requesting_user_id}, owner={file.user_id}, admin={requesting_is_admin}, type={file.file_type})"
        )
        return file, future

    async def clone(
        self,
        source_file_id: UUID,
        requesting_user_id: UUID,
        org_id: UUID,
    ) -> UserFile:
        """«Add to my files»: create a metadata-only clone of source.

        The clone shares the underlying storage_key (no byte copy) and points
        back to the source via cloned_from_file_id. Each user owns their own
        rag_status / summary / is_public flags independently.

        Idempotent: if the requesting user already has an active clone of this
        source, return it instead of creating a duplicate.

        The caller MUST verify chat-access permission (i.e. that the requesting
        user has seen this file in a chat they belong to) BEFORE calling — that
        check is a route-layer concern.

        Storage bytes are SHARED with source: clone copies storage_key verbatim,
        no byte duplication. If source is hard-deleted from filesystem (out of
        scope for MVP — soft-delete via is_active=false is the normal path), all
        clones break: cloned_from_file_id ON DELETE SET NULL preserves the
        metadata link's nullability but storage_key points to nothing.

        Args:
            source_file_id: file to clone
            requesting_user_id: future owner of the clone
            org_id: must match source.org_id (cross-org clones forbidden)

        Raises:
            FileNotFoundError: source does not exist or is inactive.
            ValueError: source belongs to a different org.
        """
        source = await self.file_storage.get_by_id(source_file_id)
        if source is None or not source.is_active:
            raise FileNotFoundError(f"Source file {source_file_id} not found")
        if source.org_id != org_id:
            raise ValueError("Cross-org clone not permitted")

        # Idempotency: existing active clone short-circuits.
        existing = await self.file_storage.find_active_clone(
            requesting_user_id, source_file_id
        )
        if existing is not None:
            logger.info(
                f"Clone of {source_file_id} for user {requesting_user_id} "
                f"already exists ({existing.id}); returning existing"
            )
            return existing

        clone = UserFile(
            org_id=org_id,
            user_id=requesting_user_id,
            uploaded_by_user_id=requesting_user_id,
            storage_key=source.storage_key,  # SHARED — no byte copy
            original_filename=source.original_filename,
            file_type=source.file_type,
            file_size=source.file_size,
            content_hash=source.content_hash,
            summary="",                      # owner-specific; reset on clone
            is_table=source.is_table,
            is_public=False,                 # clones are never re-shared by default
            rag_status="not_indexed",        # owner opts in separately
            cloned_from_file_id=source.id,
        )
        created = await self.file_storage.create(clone)
        logger.info(
            f"Cloned file {source.id} -> {created.id} for user {requesting_user_id}"
        )
        return created

    async def get(self, file_id: UUID) -> Optional[UserFile]:
        """Get file metadata by ID"""
        return await self.file_storage.get_by_id(file_id)

    async def download(self, file_id: UUID) -> tuple:
        """
        Download file binary data.
        Returns (data: bytes, file_record: UserFile) or raises.
        """
        file_record = await self.file_storage.get_by_id(file_id)
        if not file_record:
            raise FileNotFoundError(f"File {file_id} not found")
        data = await self.adapter.read(file_record.storage_key)
        return data, file_record

    async def list_by_user(self, user_id: UUID) -> List[UserFile]:
        """List files belonging to an employee"""
        return await self.file_storage.list_by_user(user_id)

    async def list_by_org(self, org_id: UUID) -> List[UserFile]:
        """List all files in an organization"""
        return await self.file_storage.list_by_org(org_id)

    async def delete(self, file_id: UUID) -> bool:
        """Soft-delete file (deactivate metadata, remove binary)"""
        file_record = await self.file_storage.get_by_id(file_id)
        if not file_record:
            return False

        # Remove binary from storage
        try:
            await self.adapter.delete(file_record.storage_key)
        except Exception as e:
            logger.warning(f"Failed to delete file from storage: {e}")

        # Deactivate metadata
        result = await self.file_storage.deactivate(file_id)
        if result:
            logger.info(f"Deleted file {file_id} ({file_record.original_filename})")
        return result

    async def move_to_folder(
        self, file_id: UUID, new_folder_id: Optional[UUID],
        actor_user_id: UUID, actor_is_admin: bool, actor_org_id: UUID,
    ) -> Optional[UserFile]:
        """Move a file to a different folder (or to root if new_folder_id is None).

        Authorization: actor must be the file owner, or admin of the same org.
        Caller MUST verify new_folder_id (if not None) belongs to file owner via
        folder_service.verify_folder_owner before calling.
        """
        file_record = await self.file_storage.get_by_id(file_id)
        if file_record is None:
            return None
        is_owner = file_record.user_id == actor_user_id
        is_admin_same_org = actor_is_admin and file_record.org_id == actor_org_id
        if not (is_owner or is_admin_same_org):
            raise PermissionError("Only the file owner or admin can move this file")
        return await self.file_storage.move_to_folder(file_id, new_folder_id)

    async def change_public(self, file_id: UUID, is_public: bool) -> Optional[UserFile]:
        """Set the is_public visibility flag on a file."""
        return await self.file_storage.change_public(file_id, is_public)
