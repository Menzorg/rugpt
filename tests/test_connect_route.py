"""Zero Trust WS-handshake route: POST /api/v1/web/connect.

Mirrors tests/test_zt_state_handshake.py — real DB-backed device key, real
WebSignatureMiddleware path. Asserts a correctly signed request returns 200 with
the socket identity, and an unsigned request is rejected.
"""
import os, time, base64, uuid
import pytest, pytest_asyncio, asyncpg
from httpx import AsyncClient, ASGITransport
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.engine.app import app
from src.engine.services.engine_service import init_engine_service, get_engine_service
from src.engine.security.signature_payload import build_payload
from src.engine.routes.auth import create_token

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")

ROUTE_ID = "/api/v1/connect"


def _kp():
    p = ec.generate_private_key(ec.SECP256R1())
    return p, p.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _sign(p, payload):
    return base64.b64encode(p.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))).decode()


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service(); eng = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as c:
        org = await c.fetchval(
            "INSERT INTO organizations (id,name,slug) VALUES (gen_random_uuid(),'b',$1) RETURNING id",
            f"b_{uuid.uuid4().hex[:8]}")
        email = f"u_{uuid.uuid4()}@t.local"
        uid = await c.fetchval(
            "INSERT INTO users (id,org_id,username,name,password_hash,email,is_active,is_admin) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true,true) RETURNING id",
            org, f"u_{uuid.uuid4().hex[:6]}", email)
    priv, pem = _kp()
    await eng.device_storage.register_device(user_id=uid, device_public_key=pem, device_name="t")
    await pool.close()
    return {"uid": str(uid), "org": str(org), "email": email, "priv": priv,
            "token": create_token(uid, org)}


@pytest.mark.asyncio(loop_scope="module")
async def test_connect_signed_ok(env):
    """A correctly signed POST /web/connect returns 200 with the socket identity.

    For non-GET, WebSignatureMiddleware reads the signature fields from the JSON
    body; the signed payload's clean_body is the body minus those fields.
    """
    n = uuid.uuid4().hex; ts = int(time.time())
    body = {"user_id": env["uid"], "org_id": env["org"]}
    payload = build_payload("POST", ROUTE_ID, body, n, ts)
    body = {**body, "signature": _sign(env["priv"], payload),
            "nonce": n, "sig_timestamp": ts, "route_id": ROUTE_ID}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/web/connect", json=body,
            headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == env["uid"]
    assert body["org_id"] == env["org"]
    assert body["email"] == env["email"]
    assert body["is_admin"] is True


@pytest.mark.asyncio(loop_scope="module")
async def test_connect_unsigned_rejected(env):
    """An unsigned POST /web/connect is rejected (no zt_user_id → 401/403)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/api/v1/web/connect?user_id={env['uid']}&org_id={env['org']}",
            headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code in (401, 403), r.text
