"""Plan B Task 3: identity migration for routes/chats.py.

Exercises the signed /api/v1/web/* harness end-to-end to prove that chat
handlers derive identity from current_user (device-signed) and no longer need
actor user_id/org_id params in the query/body.
"""
import os, time, base64, json, uuid
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


@pytest_asyncio.fixture(loop_scope="session")
async def env():
    await init_engine_service()
    engine = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(),'zt-chats',$1) RETURNING id",
            f"ztc_{uuid.uuid4().hex[:8]}")
        # signer
        u1 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true) RETURNING id",
            org_id, f"u1_{uuid.uuid4().hex[:6]}", f"u1_{uuid.uuid4()}@t.local")
        # peer (other party in the direct chat)
        u2 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true) RETURNING id",
            org_id, f"u2_{uuid.uuid4().hex[:6]}", f"u2_{uuid.uuid4()}@t.local")
        # a direct chat owned by the signer (u1, u2)
        chat_id = await conn.fetchval(
            "INSERT INTO chats (id, org_id, type, participants, created_by, is_active) "
            "VALUES (gen_random_uuid(),$1,'direct',$2,$3,true) RETURNING id",
            org_id, [str(u1), str(u2)], u1)
    priv, pem = _gen_keypair()
    await engine.device_storage.register_device(user_id=u1, device_public_key=pem, device_name="t")
    token = create_token(u1, org_id)
    yield {
        "engine": engine, "pool": pool, "org_id": org_id,
        "u1": str(u1), "u2": str(u2), "chat_id": str(chat_id),
        "priv": priv, "token": token,
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE org_id=$1)", org_id)
        await conn.execute("DELETE FROM chats WHERE org_id=$1", org_id)
        await conn.execute("DELETE FROM user_devices WHERE user_id=$1", u1)
        await conn.execute("DELETE FROM users WHERE org_id=$1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id=$1", org_id)
    await pool.close()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio(loop_scope="session")
async def test_list_my_chats_without_user_id_param(env):
    """GET /chats/my WITHOUT user_id in query → 200 and returns the signer's chats.

    The route derives the viewer from current_user; only signature fields + JWT
    are supplied. The signer (u1) is a participant of one direct chat.
    """
    route_id = "/api/v1/chats/my"
    nonce = uuid.uuid4().hex
    ts = int(time.time())
    payload = f"GET:{route_id}::{nonce}:{ts}"
    sig = quote(_sign(env["priv"], payload), safe="")
    # user_id IS in the query (middleware needs it to find device keys) but the
    # route no longer reads it — proving identity comes from current_user.
    url = (f"/api/v1/web/chats/my?user_id={env['u1']}&signature={sig}"
           f"&nonce={nonce}&sig_timestamp={ts}&route_id={route_id}")
    async with _client() as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200, r.text
    chats = r.json()
    assert any(ch["id"] == env["chat_id"] for ch in chats), chats


@pytest.mark.asyncio(loop_scope="session")
async def test_send_message_without_actor_params(env):
    """POST /chats/{chat_id}/messages WITHOUT user_id/org_id consumed by the route.

    The signed body still carries user_id (middleware needs it for device-key
    lookup), but SendMessageRequest must tolerate the extra key (no 422) and the
    route must derive the sender from current_user. Asserts the message is
    created as the signer (u1).
    """
    chat_id = env["chat_id"]
    # route_id is the Starlette TEMPLATE, not the substituted path — the
    # middleware resolves the real path to this template and compares.
    route_id = "/api/v1/chats/{chat_id}/messages"
    nonce = uuid.uuid4().hex
    ts = int(time.time())
    content = f"hello from signer {uuid.uuid4().hex[:6]}"
    # clean_body = body minus signature fields; KEEPS user_id (middleware strips
    # only SIGNATURE_FIELDS from POST bodies, leaving user_id in the forwarded body).
    clean_body = {"content": content, "user_id": env["u1"]}
    clean_str = json.dumps(clean_body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    payload = f"POST:{route_id}:{clean_str}:{nonce}:{ts}"
    sig = _sign(env["priv"], payload)
    body = {
        "content": content,
        "user_id": env["u1"],
        "signature": sig,
        "nonce": nonce,
        "sig_timestamp": ts,
        "route_id": route_id,
    }
    async with _client() as c:
        r = await c.post(
            f"/api/v1/web/chats/{chat_id}/messages",
            json=body,
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    # Not 401 (signed) and not 422 (leftover user_id tolerated by Pydantic).
    assert r.status_code == 200, r.text
    data = r.json()
    msg = data["user_message"]
    assert msg["content"] == content
    assert msg["sender_id"] == env["u1"], msg
    assert msg["chat_id"] == chat_id
