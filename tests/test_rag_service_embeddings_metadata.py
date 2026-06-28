import pytest
from uuid import uuid4
from types import SimpleNamespace

from src.engine.logging_context import bind_correlation_id, correlation_id_var
from src.engine.models.user_file import UserFile
from src.engine.services.rag_service import RAGService


class FakeEmbeddings:
    def __init__(self):
        self.query_calls = []
        self.document_calls = []

    async def aembed_query(self, text, **kwargs):
        self.query_calls.append((text, kwargs))
        return [1.0, 2.0, 3.0]

    async def aembed_documents(self, documents, **kwargs):
        self.document_calls.append((documents, kwargs))
        return [[1.0, 2.0, 3.0] for _ in documents]


class FakeRagStore:
    def __init__(self):
        self.related_doc_calls = []

    async def call_search_related_docs(self, **kwargs):
        self.related_doc_calls.append(kwargs)
        return []


@pytest.mark.asyncio
async def test_rag_embed_query_passes_correlation_id_as_litellm_session_id():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()

    token = bind_correlation_id("corr-rag-1")
    try:
        assert await service._embed_query("query") == [1.0, 2.0, 3.0]
    finally:
        correlation_id_var.reset(token)

    assert service._embeddings.query_calls == [
        (
            "query",
            {
                "extra_body": {
                    "litellm_session_id": "corr-rag-1",
                    "metadata": {
                        "agent_name": "rag_embedding",
                        "chatid": "",
                    },
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_rag_embed_query_prefixes_optional_instruct():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()

    assert await service._embed_query("query", instruct="Use matching parameters") == [1.0, 2.0, 3.0]

    assert service._embeddings.query_calls[0][0] == (
        "Instruct: Use matching parameters\nQuery:query"
    )


@pytest.mark.asyncio
async def test_find_docs_embeds_query_with_parameters_and_categories_instruct():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()
    service._store = FakeRagStore()

    docs = await service.find_docs(
        org_id="org-1",
        user_id="user-1",
        query="invoice policy",
        top_k=3,
        is_admin=True,
        filter_user_id="owner-1",
        exclude_images=True,
        search_mode="abstract",
    )

    assert docs == []
    embedded_query = service._embeddings.query_calls[0][0]
    assert embedded_query.startswith("Instruct: ")
    assert "document parameters" in embedded_query
    assert "business categories" in embedded_query
    assert embedded_query.endswith("\nQuery:invoice policy")
    assert service._store.related_doc_calls[0]["query"] == "invoice policy"
    assert service._store.related_doc_calls[0]["query_embedding"] == [1.0, 2.0, 3.0]


def test_summary_embedding_text_is_summary_only():
    service = RAGService.__new__(RAGService)
    file_record = SimpleNamespace(comment="manual comment")

    assert service._build_embedding_text("short summary", file_record) == "short summary"


@pytest.mark.asyncio
async def test_manual_comment_embedding_skips_category_backed_files():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()

    file_record = UserFile(
        comment="category comment",
        content_type_id=uuid4(),
    )

    assert await service._build_manual_comment_embedding(file_record) is None
    assert service._embeddings.query_calls == []


@pytest.mark.asyncio
async def test_manual_comment_embedding_embeds_uncategorized_comment():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()

    file_record = UserFile(
        comment="manual comment",
        content_type_id=None,
    )

    assert await service._build_manual_comment_embedding(file_record) == [1.0, 2.0, 3.0]
    assert service._embeddings.query_calls[0][0] == "manual comment"


class FakeFileStorage:
    def __init__(self):
        self.update_comment_calls = []

    async def update_comment(self, **kwargs):
        self.update_comment_calls.append(kwargs)
        return None


@pytest.mark.asyncio
async def test_ingest_comment_embedding_is_persisted_separately_from_summary_store():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()
    service._file_storage = FakeFileStorage()
    file_id = uuid4()
    file_record = UserFile(comment="manual comment", content_type_id=None)

    await service._update_manual_comment_embedding(file_id, file_record)

    assert service._file_storage.update_comment_calls == [
        {
            "file_id": file_id,
            "comment": "manual comment",
            "content_type_id": None,
            "comment_embedding": [1.0, 2.0, 3.0],
        }
    ]


@pytest.mark.asyncio
async def test_rag_embed_documents_passes_correlation_id_as_litellm_session_id():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()

    token = bind_correlation_id("corr-rag-2")
    try:
        assert await service._embed_documents(["a", "b"]) == [
            [1.0, 2.0, 3.0],
            [1.0, 2.0, 3.0],
        ]
    finally:
        correlation_id_var.reset(token)

    assert service._embeddings.document_calls == [
        (
            ["a", "b"],
            {
                "extra_body": {
                    "litellm_session_id": "corr-rag-2",
                    "metadata": {
                        "agent_name": "rag_embedding",
                        "chatid": "",
                    },
                }
            },
        )
    ]
