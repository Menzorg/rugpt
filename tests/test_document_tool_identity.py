import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.engine.models.user_file import UserFile

AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "engine" / "agents"
agents_pkg = types.ModuleType("src.engine.agents")
agents_pkg.__path__ = [str(AGENTS_DIR)]
tools_pkg = types.ModuleType("src.engine.agents.tools")
tools_pkg.__path__ = [str(AGENTS_DIR / "tools")]
sys.modules.setdefault("src.engine.agents", agents_pkg)
sys.modules.setdefault("src.engine.agents.tools", tools_pkg)

services_pkg = types.ModuleType("src.engine.services")
services_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "engine" / "services")]
rag_service_stub = types.ModuleType("src.engine.services.rag_service")
rag_service_stub.RAGService = type("RAGService", (), {})
sys.modules.setdefault("src.engine.services", services_pkg)
sys.modules.setdefault("src.engine.services.rag_service", rag_service_stub)

token_counter_stub = types.ModuleType("src.engine.utils.token_counter")
token_counter_stub.count_tokens = lambda text, tool_count=0: len(str(text).split()) + tool_count * 150
token_counter_stub.cut_text_by_token_count = lambda text, limit: " ".join(str(text).split()[:limit])
sys.modules.setdefault("src.engine.utils.token_counter", token_counter_stub)

runtime_module = importlib.import_module("src.engine.agents.runtime")
list_documents = importlib.import_module("src.engine.agents.tools.list_documents")
rag_tool = importlib.import_module("src.engine.agents.tools.rag_tool")

RuntimeContext = runtime_module.RuntimeContext
_list_documents_impl = list_documents._list_documents_impl
list_global_documents = list_documents.list_global_documents
list_own_documents = list_documents.list_own_documents
_can_access_file = rag_tool._can_access_file


class FakeFileStorage:
    def __init__(self, files):
        self.files = files

    async def list_by_org(self, org_id):
        return [f for f in self.files if f.org_id == org_id]

    async def get_by_id(self, file_id):
        return next((f for f in self.files if f.id == file_id and f.is_active), None)


def _file(owner_id, org_id, name, *, is_public=False):
    return UserFile(
        id=uuid4(),
        user_id=owner_id,
        org_id=org_id,
        uploaded_by_user_id=owner_id,
        original_filename=name,
        file_type="pdf",
        file_size=123,
        is_public=is_public,
        rag_status="indexed",
        summary=f"summary for {name}",
    )


def _config(caller_id, org_id, *, called_id=None):
    return {
        "configurable": {
            "org_id": str(org_id),
            "user_id": str(caller_id),
            "caller_user_id": str(caller_id),
            "called_user_id": str(called_id) if called_id else "",
            "invocation_kind": "mention" if called_id else "direct",
            "is_admin": False,
        }
    }


def _runtime():
    return SimpleNamespace(context=RuntimeContext())


@pytest.mark.asyncio
async def test_list_own_documents_direct_includes_caller_private_and_public(monkeypatch):
    org_id = uuid4()
    caller_id = uuid4()
    other_id = uuid4()
    files = [
        _file(caller_id, org_id, "a-private.pdf"),
        _file(caller_id, org_id, "a-public.pdf", is_public=True),
        _file(other_id, org_id, "b-public.pdf", is_public=True),
    ]
    monkeypatch.setattr(list_documents, "_user_file_storage", FakeFileStorage(files))

    result = await _list_documents_impl(
        _config(caller_id, org_id),
        _runtime(),
        own_only=True,
    )

    assert "a-private.pdf" in result
    assert "a-public.pdf" in result
    assert "b-public.pdf" not in result


@pytest.mark.asyncio
async def test_list_own_documents_mention_includes_only_called_user_public(monkeypatch):
    org_id = uuid4()
    caller_id = uuid4()
    called_id = uuid4()
    files = [
        _file(caller_id, org_id, "a-private.pdf"),
        _file(caller_id, org_id, "a-public.pdf", is_public=True),
        _file(called_id, org_id, "b-private.pdf"),
        _file(called_id, org_id, "b-public.pdf", is_public=True),
    ]
    monkeypatch.setattr(list_documents, "_user_file_storage", FakeFileStorage(files))

    result = await _list_documents_impl(
        _config(caller_id, org_id, called_id=called_id),
        _runtime(),
        own_only=True,
    )

    assert "b-public.pdf" in result
    assert "a-private.pdf" not in result
    assert "a-public.pdf" not in result
    assert "b-private.pdf" not in result


@pytest.mark.asyncio
async def test_file_id_lookup_uses_same_mention_visibility(monkeypatch):
    org_id = uuid4()
    caller_id = uuid4()
    called_id = uuid4()
    private_file = _file(called_id, org_id, "b-private.pdf")
    monkeypatch.setattr(list_documents, "_user_file_storage", FakeFileStorage([private_file]))

    result = await _list_documents_impl(
        _config(caller_id, org_id, called_id=called_id),
        _runtime(),
        own_only=True,
        file_id=str(private_file.id),
    )

    assert result == f"Document {private_file.id} not found or not visible to you."


@pytest.mark.asyncio
async def test_rag_search_access_denies_called_user_private_in_mention(monkeypatch):
    org_id = uuid4()
    caller_id = uuid4()
    called_id = uuid4()
    private_file = _file(called_id, org_id, "b-private.pdf")
    public_file = _file(called_id, org_id, "b-public.pdf", is_public=True)

    monkeypatch.setattr(rag_tool, "_user_file_storage", FakeFileStorage([private_file, public_file]))

    assert not await _can_access_file(
        str(private_file.id),
        str(org_id),
        str(called_id),
        public_only_owner=True,
        is_admin=False,
    )
    assert await _can_access_file(
        str(public_file.id),
        str(org_id),
        str(called_id),
        public_only_owner=True,
        is_admin=False,
    )
    assert await _can_access_file(
        str(private_file.id),
        str(org_id),
        str(called_id),
        public_only_owner=False,
        is_admin=False,
    )


def test_private_documents_tool_is_not_exposed():
    assert list_own_documents.name == "list_own_documents"
    assert list_global_documents.name == "list_global_documents"
    assert list_own_documents.name != "list_private_documents"
