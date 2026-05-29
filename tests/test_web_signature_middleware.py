import os, time, base64, uuid
from urllib.parse import quote
import pytest, pytest_asyncio, asyncpg
from httpx import AsyncClient, ASGITransport
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.engine.app import app
from src.engine.services.engine_service import init_engine_service, get_engine_service
from src.engine.routes.auth import create_token

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


def _gen_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pem


def _sign(priv, payload: str) -> str:
    der = priv.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(der).decode()


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service()
    engine = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(),'zt',$1) RETURNING id",
            f"zt_{uuid.uuid4().hex[:8]}")
        user_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true) RETURNING id",
            org_id, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    priv, pem = _gen_keypair()
    await engine.device_storage.register_device(user_id=user_id, device_public_key=pem, device_name="t")
    token = create_token(user_id, org_id)
    await pool.close()
    return {"engine": engine, "org_id": org_id, "user_id": str(user_id),
            "priv": priv, "token": token}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio(loop_scope="module")
async def test_valid_get_passes(env):
    route_id = "/api/v1/users"
    nonce = uuid.uuid4().hex; ts = int(time.time())
    payload = f"GET:{route_id}::{nonce}:{ts}"
    sig = quote(_sign(env["priv"], payload), safe="")
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?org_id={env['org_id']}&user_id={env['user_id']}"
                        f"&signature={sig}&nonce={nonce}&sig_timestamp={ts}&route_id={route_id}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200


@pytest.mark.asyncio(loop_scope="module")
async def test_missing_signature_401(env):
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?org_id={env['org_id']}&user_id={env['user_id']}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_expired_timestamp_401(env):
    route_id = "/api/v1/users"; nonce = uuid.uuid4().hex; ts = int(time.time()) - 600
    sig = _sign(env["priv"], f"GET:{route_id}::{nonce}:{ts}")
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?user_id={env['user_id']}&signature={sig}"
                        f"&nonce={nonce}&sig_timestamp={ts}&route_id={route_id}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_route_id_mismatch_401(env):
    nonce = uuid.uuid4().hex; ts = int(time.time())
    sig = _sign(env["priv"], f"GET:/api/v1/roles::{nonce}:{ts}")
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?user_id={env['user_id']}&signature={sig}"
                        f"&nonce={nonce}&sig_timestamp={ts}&route_id=/api/v1/roles",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_replay_nonce_rejected(env):
    route_id = "/api/v1/users"; nonce = uuid.uuid4().hex; ts = int(time.time())
    sig = quote(_sign(env["priv"], f"GET:{route_id}::{nonce}:{ts}"), safe="")
    url = (f"/api/v1/web/users?user_id={env['user_id']}&signature={sig}"
           f"&nonce={nonce}&sig_timestamp={ts}&route_id={route_id}")
    h = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as c:
        r1 = await c.get(url, headers=h)
        r2 = await c.get(url, headers=h)
    assert r1.status_code == 200
    assert r2.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_disallowed_route_404(env):
    async with _client() as c:
        r = await c.get("/api/v1/web/totally-unknown")
    assert r.status_code == 404


@pytest.mark.asyncio(loop_scope="module")
async def test_no_signature_route_passes(env):
    async with _client() as c:
        r = await c.get("/api/v1/web/config")
    assert r.status_code != 401
