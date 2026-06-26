from uuid import uuid4

import pytest

from src.engine.logging_context import bind_correlation_id, correlation_id_var
from src.engine.models.content_type import ContentType
from src.engine.services.content_type_service import ContentTypeService


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    async def aembed_query(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return [1.0, 2.0, 3.0]


class FakeContentTypeStorage:
    def __init__(self, current=None):
        self.current = current
        self.created = []
        self.updated = []
        self.get_calls = []

    async def create(self, ct, embedding=None):
        self.created.append((ct, embedding))
        self.current = ct
        return ct

    async def get_by_id(self, content_type_id):
        self.get_calls.append(content_type_id)
        return self.current

    async def update(self, content_type_id, **kwargs):
        self.updated.append((content_type_id, kwargs))
        if self.current is None:
            return None
        self.current = ContentType(
            id=content_type_id,
            org_id=self.current.org_id,
            name=kwargs.get("name") or self.current.name,
            description=kwargs.get("description") or self.current.description,
            is_active=kwargs.get("is_active")
            if kwargs.get("is_active") is not None
            else self.current.is_active,
        )
        return self.current


def _service(storage):
    service = ContentTypeService.__new__(ContentTypeService)
    service._storage = storage
    service._embeddings = FakeEmbeddings()
    service._vector_dim = 3
    return service


@pytest.mark.asyncio
async def test_create_embeds_before_persisting_content_type():
    storage = FakeContentTypeStorage()
    service = _service(storage)
    org_id = uuid4()

    token = bind_correlation_id("corr-content-type-1")
    try:
        created = await service.create(org_id, " Invoice ", "Payment documents")
    finally:
        correlation_id_var.reset(token)

    assert service._embeddings.calls == [
        (
            "Name: Invoice\nDescription: Payment documents",
            {
                "extra_body": {
                    "litellm_session_id": "corr-content-type-1",
                    "metadata": {
                        "agent_name": "content_type_embedding",
                        "chatid": "",
                    },
                }
            },
        )
    ]
    assert storage.created[0][0].name == "Invoice"
    assert storage.created[0][1] == [1.0, 2.0, 3.0]
    assert storage.updated == []


@pytest.mark.asyncio
async def test_update_reembeds_merged_name_and_description_before_persisting():
    content_type_id = uuid4()
    current = ContentType(
        id=content_type_id,
        org_id=uuid4(),
        name="Invoice",
        description="Old description",
    )
    storage = FakeContentTypeStorage(current=current)
    service = _service(storage)

    await service.update(content_type_id, description="New description")

    assert service._embeddings.calls[0][0] == "Name: Invoice\nDescription: New description"
    assert storage.updated == [
        (
            content_type_id,
            {
                "name": None,
                "description": "New description",
                "is_active": None,
                "embedding": [1.0, 2.0, 3.0],
            },
        )
    ]


@pytest.mark.asyncio
async def test_update_active_only_does_not_embed_or_fetch_current_row():
    content_type_id = uuid4()
    current = ContentType(id=content_type_id, org_id=uuid4(), name="Invoice")
    storage = FakeContentTypeStorage(current=current)
    service = _service(storage)

    await service.update(content_type_id, is_active=False)

    assert service._embeddings.calls == []
    assert storage.get_calls == []
    assert storage.updated == [
        (
            content_type_id,
            {
                "name": None,
                "description": None,
                "is_active": False,
                "embedding": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_without_embedding_configuration_fails_before_db_write():
    storage = FakeContentTypeStorage()
    service = _service(storage)
    service._embeddings = None

    with pytest.raises(RuntimeError, match="embedding_model is not configured"):
        await service.create(uuid4(), "Invoice", "Payment documents")

    assert storage.created == []
    assert storage.updated == []
