import os, time, base64, uuid
import pytest, pytest_asyncio, asyncpg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.engine.services.signature_service import SignatureService
from src.engine.services.nonce_store import NonceStore, NonceStoreUnavailable
from src.engine.storage.device_storage import DeviceStorage

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pem


def _sign(priv, payload: str) -> str:
    return base64.b64encode(priv.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))).decode()


@pytest_asyncio.fixture(loop_scope="module")
async def svc_env():
    dev = DeviceStorage(DSN); await dev.init()
    nonce = NonceStore(REDIS_URL, ttl_seconds=5); await nonce.init()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id,name,slug) VALUES (gen_random_uuid(),'s',$1) RETURNING id",
            f"sig_{uuid.uuid4().hex[:8]}")
        user_id = await conn.fetchval(
            "INSERT INTO users (id,org_id,username,name,password_hash,email,is_active) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true) RETURNING id",
            org_id, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    priv, pem = _keypair()
    await dev.register_device(user_id=user_id, device_public_key=pem, device_name="t")
    svc = SignatureService(dev, nonce, timestamp_tolerance=300)
    yield {"svc": svc, "user_id": user_id, "priv": priv}
    await dev.close(); await nonce.close(); await pool.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_valid_signature_ok(svc_env):
    uid, priv, svc = svc_env["user_id"], svc_env["priv"], svc_env["svc"]
    nonce = uuid.uuid4().hex; ts = int(time.time())
    payload = f"GET:/api/v1/users::{nonce}:{ts}"
    ok, result = await svc.verify_request_signature(uid, payload, _sign(priv, payload), nonce, ts)
    assert ok is True


@pytest.mark.asyncio(loop_scope="module")
async def test_bad_signature_rejected(svc_env):
    uid, svc = svc_env["user_id"], svc_env["svc"]
    nonce = uuid.uuid4().hex; ts = int(time.time())
    ok, result = await svc.verify_request_signature(uid, "GET:/x::n:1", "AAAA", nonce, ts)
    assert ok is False
    assert "error" in result


@pytest.mark.asyncio(loop_scope="module")
async def test_expired_timestamp_rejected(svc_env):
    uid, priv, svc = svc_env["user_id"], svc_env["priv"], svc_env["svc"]
    nonce = uuid.uuid4().hex; ts = int(time.time()) - 600
    payload = f"GET:/api/v1/users::{nonce}:{ts}"
    ok, result = await svc.verify_request_signature(uid, payload, _sign(priv, payload), nonce, ts)
    assert ok is False


@pytest.mark.asyncio(loop_scope="module")
async def test_replay_rejected(svc_env):
    uid, priv, svc = svc_env["user_id"], svc_env["priv"], svc_env["svc"]
    nonce = uuid.uuid4().hex; ts = int(time.time())
    payload = f"GET:/api/v1/users::{nonce}:{ts}"
    sig = _sign(priv, payload)
    ok1, _ = await svc.verify_request_signature(uid, payload, sig, nonce, ts)
    ok2, result = await svc.verify_request_signature(uid, payload, sig, nonce, ts)
    assert ok1 is True
    assert ok2 is False  # nonce burned


@pytest.mark.asyncio(loop_scope="module")
async def test_no_device_keys_rejected(svc_env):
    svc = svc_env["svc"]
    unknown = uuid.uuid4()
    nonce = uuid.uuid4().hex; ts = int(time.time())
    ok, result = await svc.verify_request_signature(unknown, "GET:/x::n:1", "AAAA", nonce, ts)
    assert ok is False
