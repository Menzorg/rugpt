"""
Storage Adapter

Abstract interface for file binary storage.
Implementations: LocalStorageAdapter (filesystem), S3StorageAdapter (S3-compatible).
"""
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

import aiofiles

logger = logging.getLogger("rugpt.storage.adapter")


class StorageAdapter(ABC):
    """Abstract file storage backend"""

    @abstractmethod
    async def save(self, key: str, data: bytes, content_type: str) -> None:
        """Save file data by key"""

    @abstractmethod
    async def read(self, key: str) -> bytes:
        """Read file data by key"""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete file by key"""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if file exists"""


class LocalStorageAdapter(StorageAdapter):
    """Store files on local filesystem"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalStorageAdapter initialized: {self.base_dir}")

    def _full_path(self, key: str) -> Path:
        return self.base_dir / key

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        path = self._full_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)
        logger.debug(f"Saved file: {key} ({len(data)} bytes)")

    async def read(self, key: str) -> bytes:
        path = self._full_path(key)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {key}")
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def delete(self, key: str) -> None:
        path = self._full_path(key)
        if path.exists():
            os.remove(path)
            logger.debug(f"Deleted file: {key}")

    async def exists(self, key: str) -> bool:
        return self._full_path(key).exists()
