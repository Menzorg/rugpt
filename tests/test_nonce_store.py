import os
import uuid
import pytest
import pytest_asyncio
from src.engine.services.nonce_store import NonceStore, NonceStoreUnavailable

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@pytest_asyncio.fixture
async def store():
    s = NonceStore(REDIS_URL, ttl_seconds=2)
    await s.init()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_first_use_ok_replay_rejected(store):
    user_id = str(uuid.uuid4())
    nonce = uuid.uuid4().hex
    assert await store.check_and_store(user_id, nonce) is True   # первый раз
    assert await store.check_and_store(user_id, nonce) is False  # replay


@pytest.mark.asyncio
async def test_unavailable_raises():
    s = NonceStore("redis://localhost:1/0", ttl_seconds=2)  # битый порт
    await s.init()
    with pytest.raises(NonceStoreUnavailable):
        await s.check_and_store("u", "n")
    await s.close()
