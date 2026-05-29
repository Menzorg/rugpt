import uuid, types, os
import pytest, pytest_asyncio, asyncpg
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.engine.routes.auth import get_current_user, create_token
from src.engine.services.engine_service import init_engine_service, get_engine_service

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


def _req(zt_user_id):
    r = types.SimpleNamespace()
    r.state = types.SimpleNamespace()
    if zt_user_id is not None:
        r.state.zt_user_id = zt_user_id
    return r


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service(); eng = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as c:
        org = await c.fetchval("INSERT INTO organizations (id,name,slug) VALUES (gen_random_uuid(),'b2',$1) RETURNING id", f"b2_{uuid.uuid4().hex[:8]}")
        uid = await c.fetchval("INSERT INTO users (id,org_id,username,name,password_hash,email,is_active,is_admin) VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true,false) RETURNING id", org, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    await pool.close()
    return {"uid": uid, "org": org, "token": create_token(uid, org)}


@pytest.mark.asyncio(loop_scope="module")
async def test_signed_matches_jwt_ok(env):
    out = await get_current_user(_req(str(env["uid"])), _creds(env["token"]))
    assert out["user_id"] == env["uid"]
    assert out["org_id"] == env["org"]   # из живой user-записи


@pytest.mark.asyncio(loop_scope="module")
async def test_signed_mismatch_jwt_401(env):
    with pytest.raises(HTTPException) as e:
        await get_current_user(_req(str(uuid.uuid4())), _creds(env["token"]))
    assert e.value.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_no_zt_state_rejected_401(env):
    # FAIL-CLOSED: нет доказанной подписью личности → 401 (никакого JWT-fallback)
    with pytest.raises(HTTPException) as e:
        await get_current_user(_req(None), _creds(env["token"]))
    assert e.value.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_invalid_jwt_401(env):
    with pytest.raises(HTTPException) as e:
        await get_current_user(_req(str(env["uid"])), _creds("garbage.token.xx"))
    assert e.value.status_code == 401
