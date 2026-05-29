"""
Folder Service

Business logic for personal file folders.
Validates ownership, cycles, depth, name uniqueness (via DB).
"""
import logging
from typing import List, Optional
from uuid import UUID

import asyncpg

from ..models.user import User
from ..models.user_file import UserFile
from ..models.user_file_folder import UserFileFolder
from ..storage.user_file_folder_storage import UserFileFolderStorage
from ..storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.services.folder")


class FolderError(Exception):
    """Base typed error for folder operations.

    code: machine-readable identifier (EMPTY_NAME, MAX_DEPTH_EXCEEDED, ...).
    message: human-readable description (Russian default).
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class FolderNotFound(FolderError):
    pass


class FolderForbidden(FolderError):
    pass


class FolderInvalidName(FolderError):
    pass


class FolderMaxDepthExceeded(FolderError):
    pass


class FolderCyclicMove(FolderError):
    pass


class FolderInvalidParentOwner(FolderError):
    pass


class FolderParentNotFound(FolderError):
    pass


class FolderNameConflict(FolderError):
    pass


class FolderService:
    MAX_DEPTH = 10
    MAX_NAME_LEN = 255

    def __init__(
        self,
        folder_storage: UserFileFolderStorage,
        file_storage: UserFileStorage,
        rag_service=None,
        storage_adapter=None,
    ):
        self.folder_storage = folder_storage
        self.file_storage = file_storage
        self.rag_service = rag_service
        self.storage_adapter = storage_adapter

    @staticmethod
    def _validate_name(name: str) -> str:
        if not name or not name.strip():
            raise FolderInvalidName("EMPTY_NAME", "Имя папки не может быть пустым")
        stripped = name.strip()
        if len(stripped) > FolderService.MAX_NAME_LEN:
            raise FolderInvalidName(
                "NAME_TOO_LONG",
                f"Имя папки слишком длинное (макс {FolderService.MAX_NAME_LEN} символов)",
            )
        return stripped

    def _check_actor_can_modify(self, folder: UserFileFolder, actor: User) -> None:
        if actor.id == folder.user_id:
            return
        if actor.is_admin and actor.org_id == folder.org_id:
            return
        raise FolderForbidden("FORBIDDEN", "Доступ запрещён")

    async def verify_folder_owner(
        self, *, folder_id: UUID, user_id: UUID, org_id: UUID,
    ) -> UserFileFolder:
        """Used by routes/file ops to confirm a folder is usable for a given user."""
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        if folder.user_id != user_id or folder.org_id != org_id:
            raise FolderInvalidParentOwner(
                "INVALID_PARENT_OWNER",
                "Папка принадлежит другому пользователю",
            )
        return folder

    async def create(
        self, *, user_id: UUID, org_id: UUID,
        parent_folder_id: Optional[UUID], name: str,
    ) -> UserFileFolder:
        clean_name = self._validate_name(name)

        if parent_folder_id is not None:
            parent = await self.folder_storage.get_by_id(parent_folder_id)
            if parent is None:
                raise FolderParentNotFound("PARENT_NOT_FOUND", "Родительская папка не найдена")
            if parent.user_id != user_id or parent.org_id != org_id:
                raise FolderInvalidParentOwner(
                    "INVALID_PARENT_OWNER",
                    "Родительская папка принадлежит другому пользователю",
                )
            parent_depth = await self.folder_storage.get_depth(parent_folder_id)
            if parent_depth is None:
                parent_depth = 0
            if parent_depth + 1 >= self.MAX_DEPTH:
                raise FolderMaxDepthExceeded(
                    "MAX_DEPTH_EXCEEDED",
                    f"Превышена максимальная глубина вложенности ({self.MAX_DEPTH} уровней)",
                )

        folder = UserFileFolder(
            user_id=user_id, org_id=org_id,
            parent_folder_id=parent_folder_id, name=clean_name,
        )
        try:
            return await self.folder_storage.create(folder)
        except asyncpg.UniqueViolationError:
            raise FolderNameConflict(
                "DUPLICATE_NAME",
                f"Папка с именем '{clean_name}' уже существует здесь",
            )

    async def rename(
        self, *, folder_id: UUID, new_name: str, actor: User,
    ) -> UserFileFolder:
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        self._check_actor_can_modify(folder, actor)
        folder.name = self._validate_name(new_name)
        try:
            return await self.folder_storage.update(folder)
        except asyncpg.UniqueViolationError:
            raise FolderNameConflict(
                "DUPLICATE_NAME",
                f"Папка с именем '{folder.name}' уже существует здесь",
            )

    async def move(
        self, *, folder_id: UUID, new_parent_id: Optional[UUID], actor: User,
    ) -> UserFileFolder:
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        self._check_actor_can_modify(folder, actor)

        if new_parent_id == folder_id:
            raise FolderCyclicMove(
                "CYCLIC_MOVE",
                "Нельзя переместить папку в саму себя",
            )

        if new_parent_id is not None:
            parent = await self.folder_storage.get_by_id(new_parent_id)
            if parent is None:
                raise FolderParentNotFound("PARENT_NOT_FOUND", "Родительская папка не найдена")
            if parent.user_id != folder.user_id or parent.org_id != folder.org_id:
                raise FolderInvalidParentOwner(
                    "INVALID_PARENT_OWNER",
                    "Родительская папка принадлежит другому пользователю",
                )
            subtree = await self.folder_storage.list_subtree_ids(folder_id)
            if new_parent_id in subtree:
                raise FolderCyclicMove(
                    "CYCLIC_MOVE",
                    "Нельзя переместить папку внутрь её собственной подпапки",
                )
            new_parent_depth = await self.folder_storage.get_depth(new_parent_id) or 0
            subtree_max = await self.folder_storage.get_subtree_max_depth(folder_id)
            if new_parent_depth + 1 + subtree_max >= self.MAX_DEPTH:
                raise FolderMaxDepthExceeded(
                    "MAX_DEPTH_EXCEEDED",
                    f"Превышена максимальная глубина вложенности ({self.MAX_DEPTH} уровней)",
                )

        folder.parent_folder_id = new_parent_id
        try:
            return await self.folder_storage.update(folder)
        except asyncpg.UniqueViolationError:
            raise FolderNameConflict(
                "DUPLICATE_NAME",
                f"Папка с именем '{folder.name}' уже существует в выбранной директории",
            )

    async def delete(
        self, *, folder_id: UUID, actor: User,
    ) -> dict:
        folder = await self.folder_storage.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound("FOLDER_NOT_FOUND", "Папка не найдена")
        self._check_actor_can_modify(folder, actor)

        subtree_ids = await self.folder_storage.list_subtree_ids(folder_id)
        if not subtree_ids:
            return {"deleted_folders": 0, "deleted_files": 0}

        files_before = await self.file_storage.list_by_folder_ids(list(subtree_ids))

        # Sequential — cross-pool tx not available in this codebase.
        # Idempotent on retry: repeat call is no-op (subtree empty after first run).
        deactivated_files = await self.file_storage.deactivate_by_folder_ids(list(subtree_ids))
        deactivated_folders = await self.folder_storage.deactivate_subtree(folder_id)

        # Best-effort RAG + disk cleanup. Failures don't roll back DB.
        for f in files_before:
            if self.rag_service is not None and f.rag_status == "indexed":
                try:
                    await self.rag_service.delete_document(f.id)
                except Exception as e:
                    logger.warning(f"RAG cleanup failed for file {f.id}: {e}")
            if self.storage_adapter is not None:
                try:
                    await self.storage_adapter.delete(f.storage_key)
                except Exception as e:
                    logger.warning(f"Storage adapter delete failed for {f.storage_key}: {e}")

        return {
            "deleted_folders": len(deactivated_folders),
            "deleted_files": len(deactivated_files),
        }

    async def get_tree(
        self, *, user_id: UUID, org_id: UUID,
    ) -> List[dict]:
        """Returns nested tree starting at root level."""
        all_folders = await self.folder_storage.list_by_user(user_id)
        all_folders = [f for f in all_folders if f.org_id == org_id]
        by_parent: dict = {}
        for f in all_folders:
            by_parent.setdefault(f.parent_folder_id, []).append(f)

        def build(parent_id: Optional[UUID]) -> List[dict]:
            nodes = by_parent.get(parent_id, [])
            result = []
            for f in nodes:
                d = f.to_dict()
                d["children"] = build(f.id)
                result.append(d)
            return result

        return build(None)

    async def list_children(
        self, *, user_id: UUID, parent_folder_id: Optional[UUID],
    ) -> List[UserFileFolder]:
        return await self.folder_storage.list_children(user_id, parent_folder_id)

    async def list_in_folder(
        self, *, user_id: UUID, folder_id: Optional[UUID],
    ) -> List[UserFile]:
        return await self.file_storage.list_by_user_in_folder(user_id, folder_id)
