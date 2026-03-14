"""
File Service

Business logic for file upload/download/management.
Uses StorageAdapter for binary data, UserFileStorage for metadata.
"""
import hashlib
import logging
from typing import Optional, List
from uuid import UUID

from ..models.user_file import UserFile
from ..storage.user_file_storage import UserFileStorage
from ..storage.storage_adapter import StorageAdapter
from ..constants import ALLOWED_FILE_TYPES, CONTENT_TYPES, MAX_FILE_SIZE

logger = logging.getLogger("rugpt.services.file")


class FileService:

    def __init__(
        self,
        file_storage: UserFileStorage,
        storage_adapter: StorageAdapter,
        max_file_size: int = MAX_FILE_SIZE,
        allowed_types: set = None,
    ):
        self.file_storage = file_storage
        self.adapter = storage_adapter
        self.max_file_size = max_file_size
        self.allowed_types = allowed_types or ALLOWED_FILE_TYPES

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

        # Вычислить SHA-256 хеш содержимого файла
        content_hash = self._hash_bytes(data)

        # Проверить наличие дубликата для данного пользователя по хешу.
        # Дубликат определяется как активный файл того же владельца
        # с идентичным содержимым (одинаковый content_hash).
        duplicate = await self.file_storage.find_duplicate(user_id, content_hash)
        if duplicate:
            raise ValueError(
                f"Duplicate file: identical content already exists as '{duplicate.original_filename}' "
                f"(id={duplicate.id})"
            )

        # Create metadata record
        file_record = UserFile(
            user_id=user_id,
            org_id=org_id,
            uploaded_by_user_id=uploaded_by_user_id,
            original_filename=filename,
            file_type=ext,
            file_size=len(data),
            content_hash=content_hash,  # сохраняем хеш для последующей детекции дубликатов
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
