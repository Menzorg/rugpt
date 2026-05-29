"""Redis-хранилище nonce для защиты от replay. Fail-closed: нет Redis → исключение."""
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.engine.unified_logger import get_logger

logger = get_logger("services")


class NonceStoreUnavailable(Exception):
    """Redis недоступен — запрос нельзя безопасно пропустить (fail-closed)."""


class NonceStore:
    def __init__(self, redis_url: str, ttl_seconds: int):
        self._url = redis_url
        self._ttl = ttl_seconds
        self._redis: Redis | None = None

    async def init(self) -> None:
        self._redis = Redis.from_url(self._url, socket_connect_timeout=2)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def check_and_store(self, user_id: str, nonce: str) -> bool:
        """True если nonce новый. False если уже использован. Raise при недоступности."""
        if self._redis is None:
            raise NonceStoreUnavailable("NonceStore not initialized")
        key = f"nonce:{user_id}:{nonce}"
        try:
            result = await self._redis.set(key, "1", nx=True, ex=self._ttl)
        except (RedisError, OSError) as e:
            logger.error("nonce store unavailable", operation="check_nonce", error=str(e))
            raise NonceStoreUnavailable(str(e)) from e
        return result is not None
