import os, time, base64, uuid
import pytest, pytest_asyncio, asyncpg
from httpx import AsyncClient, ASGITransport
from urllib.parse import quote
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.engine.app import app
from src.engine.services.engine_service import init_engine_service, get_engine_service
from src.engine.routes.auth import create_token

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


def _kp():
    p = ec.generate_private_key(ec.SECP256R1())
    return p, p.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _sign(p, payload): return base64.b64encode(p.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))).decode()


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service(); eng = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as c:
        org = await c.fetchval("INSERT INTO organizations (id,name,slug) VALUES (gen_random_uuid(),'b',$1) RETURNING id", f"b_{uuid.uuid4().hex[:8]}")
        uid = await c.fetchval("INSERT INTO users (id,org_id,username,name,password_hash,email,is_active) VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true) RETURNING id", org, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    priv, pem = _kp()
    await eng.device_storage.register_device(user_id=uid, device_public_key=pem, device_name="t")
    await pool.close()
    return {"uid": str(uid), "org": str(org), "priv": priv, "token": create_token(uid, org)}


@pytest.mark.asyncio(loop_scope="module")
async def test_state_set_after_verify(env):
    rid = "/api/v1/users"; n = uuid.uuid4().hex; ts = int(time.time())
    sig = quote(_sign(env["priv"], f"GET:{rid}::{n}:{ts}"), safe="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/v1/web/users?user_id={env['uid']}&org_id={env['org']}&signature={sig}&nonce={n}&sig_timestamp={ts}&route_id={rid}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200
