from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .base import BaseStorage
from ..models.rag import ChunkRow, ChunkSearchResult, RelatedDoc


def _to_pgvector(values: list[float]) -> str:
    # asyncpg expects vector input as textual literal for pgvector casts.
    return "[" + ",".join(f"{v:.10f}" for v in values) + "]"


def _as_uuid(value: Any) -> UUID:
    """Normalize asyncpg UUID or text values to UUID."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_optional_uuid(value: Any) -> UUID | None:
    """Normalize nullable asyncpg UUID or text values to UUID."""
    if value is None:
        return None
    return _as_uuid(value)


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

    async def update_user_file_summary(
        self,
        file_id: str,
        summary: str,
        summary_embedding: list[float],
    ) -> None:
        """
        Write RAG-computed fields back to user_files.

        is_table is intentionally excluded — it is set at upload time by
        FileService based on file extension and must not be overwritten here.
        """
        self._validate_embedding(summary_embedding)
        await self.init()
        sql = """
            UPDATE user_files
            SET
                summary           = $2,
                summary_embedding = $3::vector
            WHERE id = $1::uuid
        """
        await self.execute(
            sql,
            file_id,
            summary,
            _to_pgvector(summary_embedding),
        )

    # -- CHUNK INGESTION --

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

    async def insert_chunks_and_update_document_summary(
        self,
        file_id: str,
        summary: str,
        summary_embedding: list[float],
        chunks: list[str],
        chunk_embeddings: list[list[float]],
    ) -> None:
        """
        Atomically ingest a text document:
          1. Update user_files with summary / embedding.
          2. Insert chunks referencing user_files.id via file_id.
        """
        self._validate_embedding(summary_embedding)
        rows = self._build_chunk_rows(file_id=file_id, chunks=chunks, embeddings=chunk_embeddings)
        await self.init()

        # 1. Persist RAG fields into user_files (primary document table)
        await self.update_user_file_summary(
            file_id=file_id,
            summary=summary,
            summary_embedding=summary_embedding,
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

    async def insert_rows_chunks_and_update_table_summary(
        self,
        file_id: str,
        summary: str,
        summary_embedding: list[float],
        rows_text: list[str],
        row_embeddings: list[list[float]],
    ) -> None:
        """
        Atomically ingest a table document:
          1. Update user_files with summary / embedding.
          2. Insert tables_rows_chunks referencing user_files.id via file_id.
        """
        self._validate_embedding(summary_embedding)
        table_rows = self._build_table_rows(
            file_id=file_id,
            rows_text=rows_text,
            row_embeddings=row_embeddings,
        )
        await self.init()

        # 1. Persist RAG fields; is_table already set during upload
        await self.update_user_file_summary(
            file_id=file_id,
            summary=summary,
            summary_embedding=summary_embedding,
        )

        # 2. Table rows reference user_files(id) directly
        table_rows_sql = """
            INSERT INTO tables_rows_chunks
                (file_id, table_chunk_id, row_index, row_text, embedding, metadata)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5::vector, $6::jsonb)
        """
        for row in table_rows:
            await self.execute(table_rows_sql, *row)

    async def delete_chunks(self, file_id: str) -> bool:
        """Deindex a file: delete its chunks/rows and reset RAG fields in user_files."""
        await self.init()
        await self.execute("DELETE FROM chunks WHERE file_id = $1::uuid", file_id)
        await self.execute("DELETE FROM tables_rows_chunks WHERE file_id = $1::uuid", file_id)
        status = await self.execute(
            """
            UPDATE user_files
            SET rag_status='unindexed'
            WHERE id = $1::uuid
            """,
            file_id,
        )
        try:
            return int(status.split()[-1]) > 0
        except Exception:
            return False

    async def get_doc_by_id(self, file_id: str) -> RelatedDoc | None:
        """Fetch document metadata row by file_id. Returns None if not found."""
        await self.init()
        rows = await self.fetch(
            """
            SELECT
                uf.id AS file_id,
                uf.org_id,
                uf.user_id,
                uf.original_filename AS doc_title,
                uf.summary,
                uf.comment,
                uf.content_type_id,
                ct.name AS content_type_name,
                ct.description AS content_type_description,
                uf.created_at AS uploaded_at,
                uf.created_at::date AS created_at
            FROM user_files uf
            LEFT JOIN content_types ct ON ct.id = uf.content_type_id
            WHERE uf.id = $1::uuid
              AND uf.is_active = true
            LIMIT 1
            """,
            file_id,
        )
        if not rows:
            return None
        row = rows[0]
        return RelatedDoc(
            file_id=_as_uuid(row["file_id"]),
            org_id=_as_uuid(row["org_id"]),
            user_id=_as_optional_uuid(row["user_id"]),
            doc_title=row["doc_title"],
            summary=row["summary"] or "",
            comment=row["comment"],
            content_type_id=_as_optional_uuid(row["content_type_id"]),
            content_type_name=row["content_type_name"],
            content_type_description=row["content_type_description"],
            uploaded_at=row["uploaded_at"],
            created_at=row["created_at"],
            vec_dist=None,
            tsv_score=None,
            mode_used=None,
        )

    async def call_search_related_docs(
        self,
        org_id: str,
        user_id: str | None,
        query: str,
        query_embedding: list[float],
        top_k: int,
        is_admin: bool = False,
        filter_user_id: str | None = None,
        exclude_images: bool = True,
        search_mode: str = "abstract",
    ) -> list[RelatedDoc]:
        """Call SQL function search_related_docs for doc-level retrieval."""
        self._validate_embedding(query_embedding)
        await self.init()
        sql = """
            SELECT
                doc_id AS file_id,
                org_id,
                user_id,
                doc_title,
                summary,
                comment,
                content_type_id,
                content_type_name,
                content_type_description,
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
                $5,
                $6::boolean,
                $7::uuid,
                $8::boolean,
                $9::text
            )
        """
        rows = await self.fetch(
            sql,
            org_id,
            user_id,
            query,
            _to_pgvector(query_embedding),
            top_k,
            is_admin,
            filter_user_id,
            exclude_images,
            search_mode,
        )
        return [
            RelatedDoc(
                file_id=_as_uuid(row["file_id"]),
                org_id=_as_uuid(row["org_id"]),
                user_id=_as_optional_uuid(row["user_id"]),
                doc_title=row["doc_title"],
                summary=row["summary"] or "",
                comment=row["comment"],
                content_type_id=_as_optional_uuid(row["content_type_id"]),
                content_type_name=row["content_type_name"],
                content_type_description=row["content_type_description"],
                uploaded_at=row["uploaded_at"],
                created_at=row["created_at"],
                vec_dist=row["vec_dist"],
                tsv_score=row["tsv_score"],
                mode_used=row["mode_used"],
            )
            for row in rows
        ]

    async def call_search_abstract_chunks(
        self,
        file_id: str,
        query: str,
        query_embedding: list[float],
        top_k: int,
    ) -> list[ChunkSearchResult]:
        """Call SQL function search_rag in abstract mode for doc-scoped retrieval."""
        self._validate_embedding(query_embedding)
        await self.init()
        sql = """
            SELECT
                item_id AS chunk_id,
                doc_id AS file_id,
                text_content   AS chunk_text,
                chunk_index,
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
        return [
            ChunkSearchResult(
                chunk_id=_as_uuid(row["chunk_id"]),
                file_id=_as_uuid(row["file_id"]),
                chunk_text=row["chunk_text"],
                chunk_index=row["chunk_index"],
                vec_dist=row["vec_dist"],
                tsv_score=row["tsv_score"],
                r_vec=row["r_vec"],
                r_tsv=row["r_tsv"],
                final_rank=row["final_rank"],
                source_type=row["source_type"],
            )
            for row in rows
        ]

    async def get_expanded_context_by_index(
        self,
        file_id: str,
        chunk_index: int,
        distance: int = 1,
    ) -> list[ChunkRow]:
        """Return neighboring chunks around a file_id + chunk_index anchor."""
        await self.init()
        rows = await self.fetch(
            """
            SELECT
                id,
                file_id,
                chunk_text,
                metadata,
                chunk_index
            FROM get_expanded_context_by_index(
                $1::uuid,
                $2,
                $3
            )
            """,
            file_id,
            chunk_index,
            distance,
        )
        return [
            ChunkRow(
                id=_as_uuid(row["id"]),
                file_id=_as_uuid(row["file_id"]),
                chunk_text=row["chunk_text"],
                metadata=row["metadata"] or {},
                chunk_index=row["chunk_index"],
            )
            for row in rows
        ]

    async def get_table_rows_by_range(
        self,
        file_id: str,
        row_start: int,
        row_end: int,
    ) -> list[ChunkSearchResult]:
        """Return table_rows_chunks rows where row_index BETWEEN row_start AND row_end."""
        await self.init()
        rows = await self.fetch(
            """
            SELECT
                id AS chunk_id,
                file_id,
                row_text   AS chunk_text,
                row_index
            FROM tables_rows_chunks
            WHERE file_id  = $1::uuid
              AND row_index BETWEEN $2 AND $3
            ORDER BY row_index
            """,
            str(file_id),
            row_start,
            row_end,
        )
        return [
            ChunkSearchResult(
                chunk_id=_as_uuid(row["chunk_id"]),
                file_id=_as_uuid(row["file_id"]),
                chunk_text=row["chunk_text"],
                chunk_index=None,
                vec_dist=None,
                tsv_score=None,
                r_vec=None,
                r_tsv=None,
                final_rank=None,
                source_type="table_row",
            )
            for row in rows
        ]

    async def call_search_concrete_chunks(
        self,
        file_id: str,
        query: str,
        query_embedding: list[float],
        top_k: int,
        tsv_weight: float = 1,
    ) -> list[ChunkSearchResult]:
        """Call SQL function search_rag in concrete mode for doc-scoped retrieval."""
        self._validate_embedding(query_embedding)
        await self.init()
        sql = """
            SELECT
                item_id AS chunk_id,
                doc_id AS file_id,
                text_content   AS chunk_text,
                chunk_index,
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
        return [
            ChunkSearchResult(
                chunk_id=_as_uuid(row["chunk_id"]),
                file_id=_as_uuid(row["file_id"]),
                chunk_text=row["chunk_text"],
                chunk_index=row["chunk_index"],
                vec_dist=row["vec_dist"],
                tsv_score=row["tsv_score"],
                r_vec=row["r_vec"],
                r_tsv=row["r_tsv"],
                final_rank=row["final_rank"],
                source_type=row["source_type"],
            )
            for row in rows
        ]
