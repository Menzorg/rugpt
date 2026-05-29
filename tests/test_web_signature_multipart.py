"""WebSignatureMiddleware — проверка подписи для multipart/form-data (file upload).

Контракт (Plan C, Task 5/5b): подписываемое тело multipart-запроса =
канонический JSON из { <текстовые form-поля минус signature-поля>, file_sha256 }.
payload = METHOD:route_id:<canonical>:nonce:timestamp (тот же build_payload).
signature/nonce/sig_timestamp/user_id/route_id приходят как FORM-поля.
"""
import os, time, base64, hashlib, uuid
import pytest, pytest_asyncio, asyncpg
from httpx import AsyncClient, ASGITransport
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.engine.app import app
from src.engine.services.engine_service import init_engine_service, get_engine_service
from src.engine.routes.auth import create_token
from src.engine.security.signature_payload import build_payload

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")

ROUTE_ID = "/api/v1/files/upload"


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
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(),'ztmp',$1) RETURNING id",
            f"ztmp_{uuid.uuid4().hex[:8]}")
        user_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active, is_admin) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true,true) RETURNING id",
            org_id, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    priv, pem = _gen_keypair()
    await engine.device_storage.register_device(user_id=user_id, device_public_key=pem, device_name="t")
    token = create_token(user_id, org_id)
    await pool.close()
    return {"engine": engine, "org_id": org_id, "user_id": str(user_id),
            "priv": priv, "token": token}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _signed_multipart(env, file_bytes: bytes, *, extra_fields=None,
                      tamper_file=None, signed_file_bytes=None, drop=None):
    """Собрать (files, data) для httpx multipart-запроса с валидной подписью.

    - signed_file_bytes: байты, по которым считается file_sha256 (по умолчанию = file_bytes).
    - tamper_file: если задан — реально отправляемые байты файла (для теста подмены контента).
    - drop: множество имён signature-полей, которые НЕ класть в форму (тест missing).
    """
    extra_fields = extra_fields or {}
    drop = drop or set()
    sha_src = signed_file_bytes if signed_file_bytes is not None else file_bytes
    file_sha256 = hashlib.sha256(sha_src).hexdigest()

    nonce = uuid.uuid4().hex
    ts = int(time.time())

    # signed_dict = текстовые form-поля (минус signature-поля) + file_sha256.
    # user_id остаётся среди подписываемых полей.
    signed_dict = {"user_id": env["user_id"], "file_sha256": file_sha256}
    signed_dict.update(extra_fields)

    payload = build_payload("POST", ROUTE_ID, signed_dict, nonce, ts)
    sig = _sign(env["priv"], payload)

    form = {
        "user_id": env["user_id"],
        "signature": sig,
        "nonce": nonce,
        "sig_timestamp": str(ts),
        "route_id": ROUTE_ID,
    }
    form.update({k: str(v) for k, v in extra_fields.items()})
    for k in drop:
        form.pop(k, None)

    sent_file = tamper_file if tamper_file is not None else file_bytes
    files = {"file": ("doc.txt", sent_file, "text/plain")}
    return files, form


@pytest.mark.asyncio(loop_scope="module")
async def test_valid_signed_multipart_passes_signature(env):
    files, form = _signed_multipart(env, b"hello world content")
    async with _client() as c:
        r = await c.post("/api/v1/web/files/upload", files=files, data=form,
                         headers={"Authorization": f"Bearer {env['token']}"})
    # Подпись должна пройти: НЕ 401 (signature) и НЕ 403 (signature).
    # Сам upload-роут может вернуть свой статус (200/400/500), но не подписной отказ.
    assert r.status_code != 401, r.text
    assert r.status_code != 403, r.text


@pytest.mark.asyncio(loop_scope="module")
async def test_valid_signed_multipart_reaches_downstream(env):
    """Файл доезжает до роута целым: успешный upload → 200 и метаданные файла."""
    content = b"reaches downstream intact bytes"
    files, form = _signed_multipart(env, content)
    async with _client() as c:
        r = await c.post("/api/v1/web/files/upload", files=files, data=form,
                         headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_size"] == len(content)
    assert body["original_filename"] == "doc.txt"


@pytest.mark.asyncio(loop_scope="module")
async def test_tampered_file_bytes_rejected(env):
    # Подписали sha от одних байт, отправили другие → file_sha256 не сойдётся → 401.
    files, form = _signed_multipart(
        env, b"original signed bytes", tamper_file=b"DIFFERENT actual bytes")
    async with _client() as c:
        r = await c.post("/api/v1/web/files/upload", files=files, data=form,
                         headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio(loop_scope="module")
async def test_tampered_form_field_rejected(env):
    # is_public подписан как "true", но в форму кладём "false" → payload не сойдётся → 401.
    content = b"form field tamper test"
    files, form = _signed_multipart(env, content, extra_fields={"is_public": "true"})
    form["is_public"] = "false"
    async with _client() as c:
        r = await c.post("/api/v1/web/files/upload", files=files, data=form,
                         headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio(loop_scope="module")
async def test_missing_signature_fields_rejected(env):
    files, form = _signed_multipart(env, b"missing sig fields", drop={"signature"})
    async with _client() as c:
        r = await c.post("/api/v1/web/files/upload", files=files, data=form,
                         headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio(loop_scope="module")
async def test_valid_signed_multipart_with_extra_field_passes(env):
    # Доп. подписанное form-поле (is_public) должно проходить, если согласовано.
    content = b"with is_public extra field"
    files, form = _signed_multipart(env, content, extra_fields={"is_public": "true"})
    async with _client() as c:
        r = await c.post("/api/v1/web/files/upload", files=files, data=form,
                         headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code != 401, r.text
    assert r.status_code != 403, r.text
