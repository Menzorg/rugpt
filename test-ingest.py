#!/usr/bin/env python3
import argparse
import asyncio
import logging
from pathlib import Path
from uuid import UUID

from src.engine.config import Config
from src.engine.services.file_service import FileService
from src.engine.storage.storage_adapter import LocalStorageAdapter
from src.engine.storage.user_file_storage import UserFileStorage
from src.engine.tasks.ingest_queue import IngestQueue


TEST_DOCS_DIR = Path(__file__).resolve().parent / "test-docs"


def _build_file_service() -> tuple[FileService, UserFileStorage]:
    if Config.STORAGE_BACKEND != "local":
        raise RuntimeError(
            f"Unsupported STORAGE_BACKEND={Config.STORAGE_BACKEND!r}: "
            "test-ingest.py currently supports only 'local'."
        )

    file_storage = UserFileStorage(postgres_dsn=Config.POSTGRES_DSN)
    storage_adapter = LocalStorageAdapter(base_dir=Config.STORAGE_LOCAL_DIR)
    file_service = FileService(
        file_storage=file_storage,
        storage_adapter=storage_adapter,
        max_file_size=Config.FILE_MAX_SIZE_MB * 1024 * 1024,
        allowed_types={ext.strip().lower() for ext in Config.FILE_ALLOWED_TYPES.split(",") if ext.strip()},
    )
    return file_service, file_storage


def _iter_test_docs() -> list[Path]:
    if not TEST_DOCS_DIR.exists():
        raise FileNotFoundError(f"Directory not found: {TEST_DOCS_DIR}")

    docs = sorted(path for path in TEST_DOCS_DIR.iterdir() if path.is_file())
    docs = [path for path in docs if path.name != ".gitkeep"]
    if not docs:
        raise FileNotFoundError(f"No files found in {TEST_DOCS_DIR}")
    return docs


async def _upload_and_enqueue(org_id: UUID, user_id: UUID) -> None:
    file_service, file_storage = _build_file_service()
    ingest_queue = IngestQueue()
    docs = _iter_test_docs()
    futures: list[asyncio.Future] = []

    await file_storage.init()
    try:
        for doc_path in docs:
            data = await asyncio.to_thread(doc_path.read_bytes)
            created = await file_service.upload(
                org_id=org_id,
                user_id=user_id,
                uploaded_by_user_id=user_id,
                filename=doc_path.name,
                data=data,
            )
            print(f"uploaded {doc_path.name}: file_id={created.id}")
            futures.append(
                ingest_queue.submit(
                    file_id=created.id,
                    org_id=str(created.org_id),
                    user_id=str(created.user_id),
                    filename=created.original_filename,
                    data=data,
                )
            )

        results = await asyncio.gather(*futures, return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise RuntimeError(
                "One or more ingest jobs failed: "
                + "; ".join(str(result) for result in failures)
            )
    finally:
        ingest_queue.shutdown(wait=True)
        await file_storage.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload all files from test-docs/ and enqueue RAG ingestion."
    )
    parser.add_argument("org_id", type=UUID, help="Organization UUID")
    parser.add_argument("user_id", type=UUID, help="User UUID")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = _parse_args()
    asyncio.run(_upload_and_enqueue(args.org_id, args.user_id))


if __name__ == "__main__":
    main()
