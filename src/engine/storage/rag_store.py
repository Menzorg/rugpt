from __future__ import annotations

import json
from typing import Any

from .base import BaseStorage


def _to_pgvector(values: list[float]) -> str:
    # asyncpg expects vector input as textual literal for pgvector casts.
    return "[" + ",".join(f"{v:.10f}" for v in values) + "]"


class RAG_store(BaseStorage):
    def __init__(self, dsn: str, vector_dim: int) -> None:
        """Create storage bound to PostgreSQL DSN and expected embedding dimensionality."""
        super().__init__(postgres_dsn=dsn)
        self._vector_dim = vector_dim

    def _validate_embedding(self, embedding: list[float]) -> None:
        if len(embedding) != self._vector_dim:
            raise ValueError(
                f"Embedding size {len(embedding)} does not match VECTOR_DIM={self._vector_dim}."
            )

    async def insert_document(
        self,
        doc_id: str,
        doc_title: str,
        summary: str,
        summary_embedding: list[float],
        is_table: bool,
        org_id: str,
        user_id: str | None = None,
    ) -> None:
        """Insert a rag_docs row with metadata and summary embedding."""
        self._validate_embedding(summary_embedding)
        await self.init()
        sql = """
            INSERT INTO rag_docs (
                doc_id,
                org_id,
                user_id,
                summary,
                summary_embedding,
                is_table,
                doc_title
            )
            VALUES (
                $1::uuid,
                $2::uuid,
                $3::uuid,
                $4,
                $5::vector,
                $6,
                $7
            )
        """
        await self.execute(
            sql,
            doc_id,
            org_id,
            user_id,
            summary,
            _to_pgvector(summary_embedding),
            is_table,
            doc_title,
        )

    def _build_chunk_rows(
        self, doc_id: str, chunks: list[str], embeddings: list[list[float]]
    ) -> list[tuple[Any, ...]]:
        if len(chunks) != len(embeddings):
            raise ValueError("Chunks count does not match embeddings count.")
        if not chunks:
            return []

        rows: list[tuple[Any, ...]] = []
        for idx, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
            self._validate_embedding(emb)
            rows.append(
                (
                    doc_id,
                    chunk_text,
                    _to_pgvector(emb),
                    json.dumps({"chunk_index": idx}),
                    idx,
                )
            )
        return rows

    async def insert_document_with_chunks(
        self,
        doc_id: str,
        doc_title: str,
        summary: str,
        summary_embedding: list[float],
        is_table: bool,
        org_id: str,
        user_id: str | None,
        chunks: list[str],
        chunk_embeddings: list[list[float]],
    ) -> None:
        """Atomically insert a normal text doc and its chunk embeddings."""
        self._validate_embedding(summary_embedding)
        rows = self._build_chunk_rows(doc_id=doc_id, chunks=chunks, embeddings=chunk_embeddings)
        await self.init()

        doc_sql = """
            INSERT INTO rag_docs (
                doc_id,
                org_id,
                user_id,
                summary,
                summary_embedding,
                is_table,
                doc_title
            )
            VALUES (
                $1::uuid,
                $2::uuid,
                $3::uuid,
                $4,
                $5::vector,
                $6,
                $7
            )
        """
        chunk_sql = """
            INSERT INTO chunks (
                doc_id,
                chunk_text,
                embedding,
                metadata,
                chunk_index
            )
            VALUES ($1::uuid, $2, $3::vector, $4::jsonb, $5)
        """

        await self.execute(
            doc_sql,
            doc_id,
            org_id,
            user_id,
            summary,
            _to_pgvector(summary_embedding),
            is_table,
            doc_title,
        )
        for row in rows:
            await self.execute(chunk_sql, *row)

    def _build_table_rows(
        self, doc_id: str, rows_text: list[str], row_embeddings: list[list[float]]
    ) -> list[tuple[Any, ...]]:
        if len(rows_text) != len(row_embeddings):
            raise ValueError("Table rows count does not match embeddings count.")
        rows: list[tuple[Any, ...]] = []
        for idx, (row_text, emb) in enumerate(zip(rows_text, row_embeddings)):
            self._validate_embedding(emb)
            rows.append(
                (
                    doc_id,
                    None,  # No parent chunk for native table-row ingestion path.
                    idx,
                    row_text,
                    _to_pgvector(emb),
                    json.dumps({"row_index": idx}),
                )
            )
        return rows

    async def insert_table_document_with_rows(
        self,
        doc_id: str,
        doc_title: str,
        summary: str,
        summary_embedding: list[float],
        org_id: str,
        user_id: str | None,
        rows_text: list[str],
        row_embeddings: list[list[float]],
    ) -> None:
        """Atomically insert a table doc and per-row embeddings into tables_rows_chunks."""
        self._validate_embedding(summary_embedding)
        table_rows = self._build_table_rows(
            doc_id=doc_id,
            rows_text=rows_text,
            row_embeddings=row_embeddings,
        )
        await self.init()

        doc_sql = """
            INSERT INTO rag_docs (
                doc_id,
                org_id,
                user_id,
                summary,
                summary_embedding,
                is_table,
                doc_title
            )
            VALUES (
                $1::uuid,
                $2::uuid,
                $3::uuid,
                $4,
                $5::vector,
                $6,
                $7
            )
        """
        table_rows_sql = """
            INSERT INTO tables_rows_chunks (
                doc_id,
                table_chunk_id,
                row_index,
                row_text,
                embedding,
                metadata
            )
            VALUES ($1::uuid, $2::uuid, $3, $4, $5::vector, $6::jsonb)
        """

        await self.execute(
            doc_sql,
            doc_id,
            org_id,
            user_id,
            summary,
            _to_pgvector(summary_embedding),
            True,
            doc_title,
        )
        for row in table_rows:
            await self.execute(table_rows_sql, *row)

    async def delete_document(self, doc_id: str) -> bool:
        """Delete document by id (cascades to related chunks/rows via FK)."""
        await self.init()
        sql = "DELETE FROM rag_docs WHERE doc_id = $1::uuid"
        status = await self.execute(sql, doc_id)
        if not status.startswith("DELETE "):
            return False
        try:
            affected = int(status.split()[-1])
        except Exception:
            return False
        return affected > 0

    async def call_search_related_docs(
        self,
        org_id: str,
        user_id: str | None,
        query: str,
        query_embedding: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Call SQL function search_related_docs for doc-level retrieval."""
        self._validate_embedding(query_embedding)
        await self.init()
        sql = """
            SELECT
                doc_id::text,
                org_id::text,
                user_id::text,
                doc_title,
                summary,
                uploaded_at,
                created_at,
                vec_dist,
                tsv_score,
                mode_used
            FROM search_related_docs(
                $1::uuid,
                $2::uuid,
                $3,
                $4::vector,
                $5
            )
        """
        rows = await self.fetch(sql, org_id, user_id, query, _to_pgvector(query_embedding), top_k)
        return [dict(row) for row in rows]

    async def call_search_abstract_chunks(
        self,
        doc_id: str,
        query: str,
        query_embedding: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Call SQL function search_rag in abstract mode for doc-scoped retrieval."""
        self._validate_embedding(query_embedding)
        await self.init()
        sql = """
            SELECT
                item_id::text AS chunk_id,
                doc_id::text,
                text_content AS chunk_text,
                vec_dist,
                tsv_score,
                NULL::int AS r_vec,
                NULL::int AS r_tsv,
                NULL::double precision AS final_rank,
                source_type
            FROM search_rag(
                $1::uuid,
                $2,
                $3::vector,
                $4,
                'abstract'
            )
        """
        rows = await self.fetch(sql, doc_id, query, _to_pgvector(query_embedding), top_k)
        return [dict(row) for row in rows]

    async def call_search_concrete_chunks(
        self,
        doc_id: str,
        query: str,
        query_embedding: list[float],
        top_k: int,
        tsv_weight: float,
    ) -> list[dict[str, Any]]:
        """Call SQL function search_rag in concrete mode for doc-scoped retrieval."""
        self._validate_embedding(query_embedding)
        await self.init()
        sql = """
            SELECT
                item_id::text AS chunk_id,
                doc_id::text,
                text_content AS chunk_text,
                vec_dist,
                tsv_score,
                NULL::int AS r_vec,
                NULL::int AS r_tsv,
                NULL::double precision AS final_rank,
                source_type
            FROM search_rag(
                $1::uuid,
                $2,
                $3::vector,
                $4,
                'concrete'
            )
        """
        rows = await self.fetch(
            sql,
            doc_id,
            query,
            _to_pgvector(query_embedding),
            top_k,
        )
        return [dict(row) for row in rows]
