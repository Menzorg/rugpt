"""
Base Storage

Base class for PostgreSQL storage with connection pooling.
"""
import asyncpg
import asyncio
import logging
import os
import time
from typing import Optional, Any

logger = logging.getLogger("rugpt.storage")


class BaseStorage:
    """Base storage class with PostgreSQL connection pool"""

    def __init__(self, postgres_dsn: str = "postgresql://postgres@localhost/rugpt"):
        """
        Initialize base storage.

        Args:
            postgres_dsn: PostgreSQL connection DSN
        """
        self.pg_pool: Optional[asyncpg.Pool] = None
        self.pg_dsn = postgres_dsn
        self.process_id = os.getpid()
        self._initialized = False

    async def init(self):
        """Initialize storage - connect to PostgreSQL"""
        if self._initialized:
            return

        start_time = time.time()
        logger.info("Initializing BaseStorage...")

        try:
            await self._init_postgres()
            self._initialized = True

            duration_ms = round((time.time() - start_time) * 1000, 2)
            logger.info(f"BaseStorage initialized in {duration_ms}ms")
        except Exception as e:
            logger.error(f"Failed to initialize BaseStorage: {e}")
            raise

    async def _init_postgres(self):
        """Initialize PostgreSQL connection pool with retries"""
        max_retries = 3
        retry_delay = 1

        current_pid = os.getpid()

        # Handle process fork - need new pool
        if self.pg_pool is not None and self.process_id != current_pid:
            logger.info(f"New process detected (old: {self.process_id}, new: {current_pid}), creating new pool")
            self.pg_pool = None

        self.process_id = current_pid

        for attempt in range(1, max_retries + 1):
            try:
                self.pg_pool = await asyncpg.create_pool(
                    self.pg_dsn,
                    min_size=2,
                    max_size=10,
                    command_timeout=60
                )

                # Test connection
                async with self.pg_pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")

                logger.info(f"PostgreSQL connected (attempt {attempt}/{max_retries})")
                return

            except Exception as e:
                logger.error(f"PostgreSQL connection failed (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    logger.info(f"Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)

        raise Exception("Failed to connect to PostgreSQL after all retries")

    async def close(self):
        """Close database connections"""
        if self.pg_pool:
            await self.pg_pool.close()
            self.pg_pool = None
            self._initialized = False
            logger.info("BaseStorage closed")

    async def execute(self, query: str, *args) -> str:
        """Execute a query and return status"""
        async with self.pg_pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> list:
        """Fetch multiple rows"""
        async with self.pg_pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Fetch single row"""
        async with self.pg_pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args) -> Any:
        """Fetch single value"""
        async with self.pg_pool.acquire() as conn:
            return await conn.fetchval(query, *args)
