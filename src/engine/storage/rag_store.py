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

    async def update_user_file_rag_data(
        self,
        file_id: str,
        summary: str,
        summary_embedding: list[float],
        is_table: bool,
    ) -> None:
        """
        Write RAG-computed fields back to user_files.

        Called after successful ingestion to persist summary, its embedding,
        and the is_table flag. These fields drive doc-level search (tsv + vector).
        """
        self._validate_embedding(summary_embedding)
        await self.init()
        sql = """
            UPDATE user_files
            SET
                summary           = $2,
                summary_embedding = $3::vector,
                is_table          = $4
            WHERE id = $1::uuid
        """
        await self.execute(
            sql,
            file_id,
            summary,
            _to_pgvector(summary_embedding),
            is_table,
        )

    def _build_chunk_rows(
        self, file_id: str, chunks: list[str], embeddings: list[list[float]]
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
                    file_id,
                    chunk_text,
                    _to_pgvector(emb),
                    json.dumps({"chunk_index": idx}),
                    idx,
                )
            )
        return rows

    async def insert_document_with_chunks(
        self,
        file_id: str,
        doc_title: str,
        summary: str,
        summary_embedding: list[float],
        is_table: bool,
        org_id: str,
        user_id: str | None,
        chunks: list[str],
        chunk_embeddings: list[list[float]],
    ) -> None:
        """
        Atomically ingest a text document:
          1. Update user_files with summary / embedding / is_table.
          2. Insert chunks referencing user_files.id via file_id.
        """
        self._validate_embedding(summary_embedding)
        rows = self._build_chunk_rows(file_id=file_id, chunks=chunks, embeddings=chunk_embeddings)
        await self.init()

        # 1. Persist RAG fields into user_files (primary document table)
        await self.update_user_file_rag_data(
            file_id=file_id,
            summary=summary,
            summary_embedding=summary_embedding,
            is_table=is_table,
        )

        # 2. Chunks reference user_files(id) directly
        chunk_sql = """
            INSERT INTO chunks (file_id, chunk_text, embedding, metadata, chunk_index)
            VALUES ($1::uuid, $2, $3::vector, $4::jsonb, $5)
        """
        for row in rows:
            await self.execute(chunk_sql, *row)

    def _build_table_rows(
        self, file_id: str, rows_text: list[str], row_embeddings: list[list[float]]
    ) -> list[tuple[Any, ...]]:
        if len(rows_text) != len(row_embeddings):
            raise ValueError("Table rows count does not match embeddings count.")
        rows: list[tuple[Any, ...]] = []
        for idx, (row_text, emb) in enumerate(zip(rows_text, row_embeddings)):
            self._validate_embedding(emb)
            rows.append(
                (
                    file_id,
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
        file_id: str,
        doc_title: str,
        summary: str,
        summary_embedding: list[float],
        org_id: str,
        user_id: str | None,
        rows_text: list[str],
        row_embeddings: list[list[float]],
    ) -> None:
        """
        Atomically ingest a table document:
          1. Update user_files with summary / embedding / is_table=True.
          2. Insert tables_rows_chunks referencing user_files.id via file_id.
        """
        self._validate_embedding(summary_embedding)
        table_rows = self._build_table_rows(
            file_id=file_id,
            rows_text=rows_text,
            row_embeddings=row_embeddings,
        )
        await self.init()

        # 1. Persist RAG fields; is_table=True for table documents
        await self.update_user_file_rag_data(
            file_id=file_id,
            summary=summary,
            summary_embedding=summary_embedding,
            is_table=True,
        )

        # 2. Table rows reference user_files(id) directly
        table_rows_sql = """
            INSERT INTO tables_rows_chunks
                (file_id, table_chunk_id, row_index, row_text, embedding, metadata)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5::vector, $6::jsonb)
        """
        for row in table_rows:
            await self.execute(table_rows_sql, *row)

    async def delete_document(self, file_id: str) -> bool:
        """Deindex a file: delete its chunks/rows and reset RAG fields in user_files."""
        await self.init()
        await self.execute("DELETE FROM chunks WHERE file_id = $1::uuid", file_id)
        await self.execute("DELETE FROM tables_rows_chunks WHERE file_id = $1::uuid", file_id)
        status = await self.execute(
            """
            UPDATE user_files
            SET summary='', summary_embedding=NULL, is_table=false, rag_status='uploaded'
            WHERE id = $1::uuid
            """,
            file_id,
        )
        try:
            return int(status.split()[-1]) > 0
        except Exception:
            return False

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
                doc_id::text AS file_id,
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
        file_id: str,
        query: str,
        query_embedding: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Call SQL function search_rag in abstract mode for doc-scoped retrieval."""
        self._validate_embedding(query_embedding)
        await self.init()
        sql = """
            SELECT
                item_id::text  AS chunk_id,
                doc_id::text   AS file_id,
                text_content   AS chunk_text,
                vec_dist,
                tsv_score,
                NULL::int      AS r_vec,
                NULL::int      AS r_tsv,
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
        rows = await self.fetch(sql, file_id, query, _to_pgvector(query_embedding), top_k)
        return [dict(row) for row in rows]

    async def call_search_concrete_chunks(
        self,
        file_id: str,
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
                item_id::text  AS chunk_id,
                doc_id::text   AS file_id,
                text_content   AS chunk_text,
                vec_dist,
                tsv_score,
                NULL::int      AS r_vec,
                NULL::int      AS r_tsv,
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
            file_id,
            query,
            _to_pgvector(query_embedding),
            top_k,
        )
        return [dict(row) for row in rows]
