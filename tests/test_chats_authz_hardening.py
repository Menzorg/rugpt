"""Authz-hardening для незащищённых chat-роутов (ревью ZT, п.1).

Проверяет route-level гейты, добавленные в routes/chats.py:
- get_chat            → доступ только тем, кто проходит can_user_access_chat
- archive_chat        → только создатель или admin своей орги
- remove_participant  → только создатель или admin своей орги
- add_participant     → только создатель или admin своей орги (+ visibility)

Авторизация живёт в РОУТЕ, не в сервисе (сервисные методы переиспользуются
фоновыми задачами без пользовательского контекста), поэтому тестируем через
fake-engine + override_identity, без живой БД.
"""
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.routes.chats import get_engine
from src.engine.models.chat import Chat, ChatType
from src.engine.models.user import User
from tests.zt_helpers import override_identity, clear_identity


class _FakeUserStorage:
    def __init__(self, users):
        self._users = {u.id: u for u in users}

    async def get_by_id(self, user_id):
        return self._users.get(user_id)


class _FakeChatService:
    def __init__(self, chat):
        self._chat = chat
        self.archived = False
        self.removed = None
        self.added = None

    async def get_chat(self, chat_id):
        return self._chat if (self._chat and self._chat.id == chat_id) else None

    async def can_user_access_chat(self, user, chat):
        # Та же семантика, что в сервисе: orgship + participant (без SUPPORT-веток).
        if chat.org_id != user.org_id:
            return False
        return user.id in chat.participants

    async def archive_chat(self, chat_id):
        self.archived = True
        return True

    async def remove_participant(self, chat_id, user_id):
        self.removed = (chat_id, user_id)
        return True

    async def add_participant(self, chat_id, user_id):
        self.added = (chat_id, user_id)
        return True


class _FakeDeptService:
    async def check_visible(self, user_id, target_id, org_id):
        return True


class _FakeEngine:
    def __init__(self, chat, users):
        self.chat_service = _FakeChatService(chat)
        self.user_storage = _FakeUserStorage(users)
        self.department_service = _FakeDeptService()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.fixture
def world():
    org = uuid4()
    other_org = uuid4()
    creator = User(id=uuid4(), org_id=org, name="creator", username="creator",
                   email="c@t.local", is_admin=False)
    member = User(id=uuid4(), org_id=org, name="member", username="member",
                  email="m@t.local", is_admin=False)
    admin = User(id=uuid4(), org_id=org, name="admin", username="admin",
                 email="a@t.local", is_admin=True)
    outsider = User(id=uuid4(), org_id=other_org, name="out", username="out",
                    email="o@t.local", is_admin=False)
    chat = Chat(id=uuid4(), org_id=org, type=ChatType.DIRECT,
                participants=[creator.id, member.id], created_by=creator.id)
    engine = _FakeEngine(chat, [creator, member, admin, outsider])
    app.dependency_overrides[get_engine] = lambda: engine
    yield {"org": org, "creator": creator, "member": member, "admin": admin,
           "outsider": outsider, "chat": chat, "engine": engine}
    app.dependency_overrides.pop(get_engine, None)
    clear_identity(app)


def _as(world, who):
    u = world[who]
    override_identity(app, user_id=u.id, org_id=u.org_id, is_admin=u.is_admin)


# ── get_chat ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_chat_participant_ok(world):
    _as(world, "member")
    async with _client() as c:
        r = await c.get(f"/api/v1/chats/{world['chat'].id}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_get_chat_outsider_403(world):
    _as(world, "outsider")
    async with _client() as c:
        r = await c.get(f"/api/v1/chats/{world['chat'].id}")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_chat_missing_404(world):
    _as(world, "member")
    async with _client() as c:
        r = await c.get(f"/api/v1/chats/{uuid4()}")
    assert r.status_code == 404


# ── archive_chat ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_archive_creator_ok(world):
    _as(world, "creator")
    async with _client() as c:
        r = await c.delete(f"/api/v1/chats/{world['chat'].id}")
    assert r.status_code == 200
    assert world["engine"].chat_service.archived is True


@pytest.mark.asyncio
async def test_archive_admin_ok(world):
    _as(world, "admin")
    async with _client() as c:
        r = await c.delete(f"/api/v1/chats/{world['chat'].id}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_archive_plain_member_403(world):
    _as(world, "member")
    async with _client() as c:
        r = await c.delete(f"/api/v1/chats/{world['chat'].id}")
    assert r.status_code == 403
    assert world["engine"].chat_service.archived is False


# ── remove_participant ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_participant_member_403(world):
    _as(world, "member")
    async with _client() as c:
        r = await c.delete(
            f"/api/v1/chats/{world['chat'].id}/participants/{world['member'].id}")
    assert r.status_code == 403
    assert world["engine"].chat_service.removed is None


@pytest.mark.asyncio
async def test_remove_participant_creator_ok(world):
    _as(world, "creator")
    async with _client() as c:
        r = await c.delete(
            f"/api/v1/chats/{world['chat'].id}/participants/{world['member'].id}")
    assert r.status_code == 200
    assert world["engine"].chat_service.removed is not None


# ── add_participant ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_participant_member_403(world):
    _as(world, "member")
    async with _client() as c:
        r = await c.post(
            f"/api/v1/chats/{world['chat'].id}/participants/{world['admin'].id}")
    assert r.status_code == 403
    assert world["engine"].chat_service.added is None


@pytest.mark.asyncio
async def test_add_participant_creator_ok(world):
    _as(world, "creator")
    async with _client() as c:
        r = await c.post(
            f"/api/v1/chats/{world['chat'].id}/participants/{world['admin'].id}")
    assert r.status_code == 200
    assert world["engine"].chat_service.added is not None
