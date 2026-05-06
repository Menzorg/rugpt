"""
Ingest Task Queue

Abstraction layer for RAG ingestion background jobs.

Current backend: bounded ThreadPoolExecutor (local, in-process).
Future backend:  Kafka producer — swap IngestQueue._submit internals without
                 touching any callers (routes stay identical).

Usage:
    from ..tasks.ingest_queue import ingest_queue
    ingest_queue.submit(file_id, org_id, user_id, filename, data)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from uuid import UUID

logger = logging.getLogger("rugpt.tasks.ingest_queue")

# Max concurrent Tika + RAG jobs. CPU-bound — raise when migrating to GPU workers.
_MAX_WORKERS = 5


def _run_ingest_sync(
    file_id: UUID,
    org_id: str,
    user_id: str,
    filename: str,
    data: bytes,
) -> None:
    """Run ingest in a worker thread with its own event loop and asyncpg pool.

    asyncpg pools are bound to the event loop they were created on.
    Reusing the main-loop pool from a new loop causes cross-loop errors, so
    a fresh RAGService stack is created here and closed in `finally`.

    This function is the unit of work — maps 1:1 to a future Kafka message payload.
    """
    from ..config import Config
    from ..storage.rag_store import RAG_store
    from ..storage.user_file_storage import UserFileStorage
    from ..services.rag_service import RAGService

    async def _run() -> None:
        rag_store = RAG_store(dsn=Config.RAG_STORE_DSN, vector_dim=Config.RAG_VECTOR_DIM)
        file_storage = UserFileStorage(postgres_dsn=Config.POSTGRES_DSN)
        rag_service = RAGService(
            store=rag_store,
            embedding_model=Config.EMBEDDING_MODEL,
            llm_base_url=Config.LLM_BASE_URL,
            llm_api_key=Config.LLM_API_KEY,
            chunk_size=Config.RAG_CHUNK_SIZE,
            chunk_overlap=Config.RAG_CHUNK_OVERLAP,
            summary_input_max_tokens=Config.RAG_SUMMARY_INPUT_MAX_TOKENS,
            file_storage=file_storage,
        )
        
        # Explicitly initialize pools on this thread's event loop before any DB call.
        # Without this pg_pool is None and the first fetchrow/execute crashes.
        await rag_store.init()
        await file_storage.init()
        
        try:
            logger.info(f"RAG ingest started for file_id={file_id}")
            await rag_service.try_ingest(
                org_id=org_id,
                user_id=user_id,
                filename=filename,
                data=data,
                file_id=file_id,
            )
        finally:
            # Always release — executor reuses threads across jobs
            await rag_store.close()
            await file_storage.close()

    asyncio.run(_run())


class IngestQueue:
    """Bounded task queue for RAG ingestion jobs.

    Interface is intentionally minimal so the internals can be replaced
    with a Kafka producer without changing callers:
      submit()   → producer.produce(topic, payload)
      shutdown() → producer.flush(); producer.close()
    """

    def __init__(self, max_workers: int = _MAX_WORKERS) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="rag_ingest",
        )

    def submit(
        self,
        file_id: UUID,
        org_id: str,
        user_id: str,
        filename: str,
        data: bytes,
    ) -> asyncio.Future:
        """Enqueue an ingest job. Returns immediately — waiting is optional.

        The done-callback ensures exceptions are logged even though the
        Future is intentionally not awaited by callers.

        When migrating to Kafka: replace body with producer.produce(topic, payload).
        """
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(
            self._executor,
            _run_ingest_sync,
            file_id,
            org_id,
            user_id,
            filename,
            data,
        )
        future.add_done_callback(self._on_done)
        return future

    @staticmethod
    def _on_done(future: asyncio.Future) -> None:
        """Log any unhandled exception from a finished worker thread."""
        if not future.cancelled() and future.exception():
            logger.error(
                f"Ingest job raised unhandled exception: {future.exception()}",
                exc_info=future.exception(),
            )

    def shutdown(self, wait: bool = False) -> None:
        """Drop queued jobs and stop accepting new ones.

        Running threads finish naturally (Tika/Ollama can't be interrupted).
        When using Kafka: replace with producer.flush(); producer.close().
        """
        self._executor.shutdown(wait=wait, cancel_futures=True)
        logger.info("IngestQueue shut down")


# Module-level singleton — imported by routes and app shutdown hook
ingest_queue = IngestQueue()
