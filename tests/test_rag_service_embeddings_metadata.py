from src.engine.logging_context import bind_correlation_id, correlation_id_var
from src.engine.services.rag_service import RAGService


class FakeEmbeddings:
    def __init__(self):
        self.query_calls = []
        self.document_calls = []

    def embed_query(self, text, **kwargs):
        self.query_calls.append((text, kwargs))
        return [1.0, 2.0, 3.0]

    def embed_documents(self, documents, **kwargs):
        self.document_calls.append((documents, kwargs))
        return [[1.0, 2.0, 3.0] for _ in documents]


def test_rag_embed_query_passes_correlation_id_as_litellm_session_id():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()

    token = bind_correlation_id("corr-rag-1")
    try:
        assert service._embed_query("query") == [1.0, 2.0, 3.0]
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


def test_rag_embed_documents_passes_correlation_id_as_litellm_session_id():
    service = RAGService.__new__(RAGService)
    service._embeddings = FakeEmbeddings()

    token = bind_correlation_id("corr-rag-2")
    try:
        assert service._embed_documents(["a", "b"]) == [
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
