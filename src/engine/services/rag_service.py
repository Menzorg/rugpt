from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from uuid import UUID

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_DOC_SUMMARY_PROMPT: str = (_PROMPTS_DIR / "rag_doc_summary.md").read_text(encoding="utf-8")
_TABLE_SUMMARY_PROMPT: str = (_PROMPTS_DIR / "rag_table_summary.md").read_text(encoding="utf-8")

from bs4 import BeautifulSoup
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from tika import parser

from ..config import Config
from ..models.rag import ChunkRow, ChunkSearchResult, RelatedDoc
from ..storage.rag_store import RAG_store
from ..storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.services.rag")
_ABSTRACT_SEARCH_MIN_WORDS = 5
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _safe_tika_file_name(file_name: str | None) -> str:
    raw_name = (file_name or "uploaded_file").strip() or "uploaded_file"
    return quote(raw_name, safe="")


def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _query_word_count(query: str) -> int:
    return len(_WORD_RE.findall(query))


def _extract_tika_content(parsed: Any) -> str:
    payload: Any = parsed
    if isinstance(parsed, tuple) and len(parsed) >= 2:
        payload = parsed[1]
    if isinstance(payload, dict):
        content = payload.get("content", "")
        return str(content or "")
    return ""


def _format_row(headers: list[str], values: list[str], sheet_name: str | None = None) -> str:
    pairs = [f"{header}: {value}" for header, value in zip(headers, values)]
    if sheet_name:
        pairs.insert(0, f"Sheet: {sheet_name}")
    return ", ".join(pairs)


class RAGService:
    def __init__(
        self,
        store: RAG_store | None = None,
        *,
        embedding_model: str,
        llm_base_url: str,
        llm_api_key: str,
        chunk_size: int,
        chunk_overlap: int,
        summary_input_max_tokens: int,
        file_storage: UserFileStorage | None = None,
    ) -> None:
        self._store = store or RAG_store(
            dsn=Config.RAG_STORE_DSN,
            vector_dim=Config.RAG_VECTOR_DIM,
        )
        # UserFileStorage для обновления rag_status в процессе индексации.
        # Опциональный: если не передан, обновление статусов не производится.
        self._file_storage = file_storage
        self._embeddings = OpenAIEmbeddings(
            model=embedding_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            timeout=180
        )
        self._summary_llm = ChatOpenAI(
            model=Config.RAG_SUMMARY_MODEL,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0,
            max_tokens=4096
        )
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self._tika_server_endpoint = Config.RAG_TIKA_SERVER_ENDPOINT
        self._summary_input_max_tokens = summary_input_max_tokens

    def _embed_query(self, query: str) -> list[float]:
        return self._embeddings.embed_query(query)

    def _extract_text_with_tika(self, file_bytes: bytes, file_name: str) -> str:
        parsed = parser.from_buffer(
            file_bytes,
            serverEndpoint=self._tika_server_endpoint,
            headers = {
                "X-Tika-OCRLanguage": "rus+eng",
                "X-Tika-OCRTimeoutSeconds": "350",
                "X-File-Name": _safe_tika_file_name(file_name),
            },
            requestOptions={'timeout': 300}
        )
        content = _extract_tika_content(parsed)
        if not content:
            return ""
        return content.strip()

    def _parse_table_rows(
        self,
        file_bytes: bytes,
        file_name: str | None,
    ) -> tuple[list[str], list[str]]:
        parsed = parser.from_buffer(
            file_bytes,
            serverEndpoint=self._tika_server_endpoint,
            xmlContent=True,
            headers={"X-File-Name": _safe_tika_file_name(file_name)},
        )
        xhtml = _extract_tika_content(parsed)
        if not xhtml:
            return [], []

        soup = BeautifulSoup(xhtml, "html.parser")
        table_nodes = soup.find_all("table")
        if not table_nodes:
            return [], []

        summary_headers: list[str] = []
        formatted_rows: list[str] = []

        for table_idx, table in enumerate(table_nodes, start=1):
            tr_nodes = table.find_all("tr")
            if not tr_nodes:
                continue

            raw_rows: list[list[str]] = []
            for tr in tr_nodes:
                cell_nodes = tr.find_all(["th", "td"])
                cells = [_normalize_cell(cell.get_text(" ", strip=True)) for cell in cell_nodes]
                if any(cells):
                    raw_rows.append(cells)

            if not raw_rows:
                continue

            header_cells = raw_rows[0]
            header_has_text = any(header_cells)
            headers = [
                (value if value else f"Column{idx + 1}")
                for idx, value in enumerate(header_cells)
            ] if header_has_text else [f"Column{idx + 1}" for idx in range(len(header_cells))]
            summary_headers.extend([f"Table{table_idx}.{h}" for h in headers])

            start_idx = 1 if header_has_text else 0
            for row_cells in raw_rows[start_idx:]:
                padded = row_cells + [""] * (len(headers) - len(row_cells))
                values = padded[: len(headers)]
                formatted_rows.append(_format_row(headers, values, sheet_name=f"Table{table_idx}"))

        return summary_headers, formatted_rows

    def _build_table_summary_source(
        self,
        file_name: str | None,
        headers: list[str],
        rows: list[str],
    ) -> str:
        header_text = ", ".join(headers) if headers else "нет заголовков"
        rows_text = "\n".join(rows[:50]) if rows else "нет строк"
        return (
            f"Имя файла: {file_name or 'unknown'}\n"
            f"Заголовки: {header_text}\n"
            "Первые 50 строк:\n"
            f"{rows_text}"
        )

    def _generate_summary_with_llm(self, text: str, *, is_table: bool = False) -> str:
        from ..utils.token_counter import cut_text_by_token_count
        try:
            source_text = cut_text_by_token_count(text, self._summary_input_max_tokens)
        except Exception:
            logger.warning("cut_text_by_token_count failed, falling back to char limit")
            source_text = text[: self._summary_input_max_tokens * 3]
        template = _TABLE_SUMMARY_PROMPT if is_table else _DOC_SUMMARY_PROMPT
        system_prompt, _, _ = template.partition("{document}")
        messages = [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": source_text},
        ]
        result = self._summary_llm.invoke(messages)
        summary = str(result.content).strip()
        if not summary:
            raise ValueError("LLM returned empty summary.")
        return summary

    async def set_status(self, file_id: UUID, status: str):
        if file_id and self._file_storage:
            await self._file_storage.change_rag_status(file_id, status)

    async def ingest(
        self,
        *,
        org_id: str,
        user_id: str | None,
        filename: str | None,
        data: bytes,
        file_id: UUID | None = None,
    ) -> dict[str, str | int | bool]:
        """
        Проиндексировать файл в RAG-хранилище.

        Если передан file_id, обновляет rag_status записи в user_files:
          - 'indexing' — сразу при старте
          - 'indexed'  — при успешном завершении
          - 'failed'   — при любой ошибке

        Args:
            org_id:    UUID организации (строка)
            user_id:   UUID пользователя-владельца (строка или None)
            filename:  оригинальное имя файла
            data:      бинарное содержимое файла
            file_id:   UUID записи user_files для обновления rag_status
        """
        if not data:
            raise ValueError("Uploaded file is empty.")
        if file_id is None:
            raise ValueError("file_id is required for RAG ingest.")

        fid = str(file_id)
        logger.info(
            f"rag ingest: file_id={fid} filename={filename!r} size={len(data)}B org={org_id} user={user_id}"
        )
        size_kb = round(len(data) / 1024, 1)

        # Помечаем документ как «индексация начата»
        await self.set_status(file_id, "indexing")
        logger.info(f"[{fid}] ingest started — file={filename!r} size={size_kb}KB")

        # is_table is owned by FileService at upload time; we just read it from DB.
        is_table = False
        if self._file_storage:
            file_record = await self._file_storage.get_by_id(file_id)
            if file_record:
                is_table = file_record.is_table
        logger.info(f"[{fid}] is_table={is_table}")

        # Single try/except wraps all pipeline stages.
        # `stage` is updated before each step so the except block
        # can report exactly where the failure occurred.
        stage = "init"
        try:
            if is_table:
                stage = "table_parsing"
                logger.info(f"[{fid}] stage={stage}")
                headers, table_rows = self._parse_table_rows(data, filename)
                if not table_rows:
                    raise ValueError("No table rows extracted from file.")
                logger.info(f"[{fid}] parsed {len(table_rows)} rows, {len(headers)} headers")

                stage = "table_embedding"
                logger.info(f"[{fid}] stage={stage}")
                row_embeddings = self._embeddings.embed_documents(table_rows)
                logger.info(f"[{fid}] embedded {len(row_embeddings)} row vectors")

                stage = "summary_generation"
                logger.info(f"[{fid}] stage={stage}")
                summary_source = self._build_table_summary_source(filename, headers, table_rows)
                summary = self._generate_summary_with_llm(summary_source, is_table=True)
                summary = f"Количество строк в таблице: {len(table_rows)}\n" + summary
                logger.info(f"[{fid}] summary generated ({len(summary)} chars)")

                stage = "summary_embedding"
                logger.info(f"[{fid}] stage={stage}")
                summary_embedding = self._embeddings.embed_query(summary)

                stage = "db_write"
                logger.info(f"[{fid}] stage={stage}")
                await self._store.insert_table_document_with_rows(
                    file_id=str(file_id),
                    doc_title=filename or str(file_id),
                    summary=summary,
                    summary_embedding=summary_embedding,
                    org_id=org_id,
                    user_id=user_id,
                    rows_text=table_rows,
                    row_embeddings=row_embeddings,
                )

                await self.set_status(file_id, "indexed")
                logger.info(f"[{fid}] ingest completed (table) — rows_ingested={len(table_rows)}")
                return {"file_id": fid, "chunks_ingested": len(table_rows)}

            stage = "text_extraction"
            logger.info(f"[{fid}] stage={stage}")
            full_text = self._extract_text_with_tika(data, filename or "uploaded_file")
            full_text.replace("....", "") # Remove noise like .... in ToC
            if not full_text:
                raise ValueError("No text content extracted from file.")
            logger.info(f"[{fid}] extracted {len(full_text)} chars")

            stage = "text_splitting"
            logger.info(f"[{fid}] stage={stage}")
            chunks = self._splitter.split_text(full_text)
            if not chunks:
                raise ValueError("Text splitting produced no chunks.")
            logger.info(f"[{fid}] split into {len(chunks)} chunks")

            stage = "chunk_embedding"
            logger.info(f"[{fid}] stage={stage}")
            chunk_embeddings = self._embeddings.embed_documents(chunks)
            logger.info(f"[{fid}] embedded {len(chunk_embeddings)} chunk vectors")

            stage = "summary_generation"
            logger.info(f"[{fid}] stage={stage}")
            summary = self._generate_summary_with_llm(full_text)
            logger.info(f"[{fid}] summary generated ({len(summary)} chars)")

            stage = "summary_embedding"
            logger.info(f"[{fid}] stage={stage}")
            summary_embedding = self._embeddings.embed_query(summary)

            stage = "db_write"
            logger.info(f"[{fid}] stage={stage}")
            await self._store.insert_document_with_chunks(
                file_id=str(file_id),
                doc_title=filename or str(file_id),
                summary=summary,
                summary_embedding=summary_embedding,
                org_id=org_id,
                user_id=user_id,
                chunks=chunks,
                chunk_embeddings=chunk_embeddings,
            )

        except Exception as exc:
            logger.error(f"[{fid}] ingest failed at stage={stage}: {exc}")
            # Mark file as failed and surface the stage name in the error message
            await self.set_status(file_id, "failed")
            raise ValueError(f"{stage}: {exc}") from exc

        await self.set_status(file_id, "indexed")
        logger.info(f"[{fid}] ingest completed (text) — chunks_ingested={len(chunks)}")
        return {"file_id": fid, "chunks_ingested": len(chunks)}

    async def try_ingest(
        self,
        *,
        org_id: str,
        user_id: str | None,
        filename: str | None,
        data: bytes,
        file_id: UUID | None = None,
        max_retries: int = 3,
    ) -> dict[str, str | int | bool]:
        """
        Wrapper around ingest with automatic retry.

        Attempts up to max_retries times on failure.
        Re-raises the last exception if all attempts are exhausted.
        """

        if Config.DEBUG:
            max_retries = 1  # No retries in debug mode to surface errors immediately

        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return await self.ingest(
                    org_id=org_id,
                    user_id=user_id,
                    filename=filename,
                    data=data,
                    file_id=file_id,
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"RAG ingest attempt {attempt}/{max_retries} failed for "
                    f"file_id={file_id}: {exc}"
                )
        raise last_exc  # type: ignore[misc]

    async def get_doc_by_id(self, file_id: str) -> RelatedDoc | None:
        """Return document metadata by file_id, or None if not found."""
        return await self._store.get_doc_by_id(file_id)

    async def find_docs(
        self,
        org_id: str,
        user_id: str | None,
        query: str,
        top_k: int,
    ) -> list[RelatedDoc]:
        """Return top-k related docs in org/user scope using SQL hybrid search."""
        logger.info(
            f"rag find_docs: query={query!r} top_k={top_k} org={org_id} user={user_id}"
        )
        SUMMARY_SEARCH_INSTRUCT = (
    "Instruct: Retrieve document summaries that are semantically relevant to the user's need, "
    "including the topic, purpose, task, or problem described, even without exact keyword overlap.\n"
)
        qwen_query = (SUMMARY_SEARCH_INSTRUCT + f"Query: {query}")
        
        query_embedding = self._embed_query(qwen_query)
        docs = await self._store.call_search_related_docs(
            org_id=org_id,
            user_id=user_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
        )
        logger.info(f"rag find_docs: returned {len(docs)} docs")
        return docs

    async def search_abstract_in_doc(
        self,
        file_id: str,
        query: str,
        top_k: int,
    ) -> list[ChunkSearchResult]:
        """Return top-k abstract matches inside one file."""
        query_embedding = self._embed_query(query)
        return await self._store.call_search_abstract_chunks(
            file_id=file_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
        )

    async def search_concrete_in_doc(
        self,
        file_id: str,
        query: str,
        top_k: int,
        tsv_weight: Optional[float] = 1,
    ) -> list[ChunkSearchResult]:
        """Return top-k concrete matches inside one file."""
        word_count = _query_word_count(query)
        if word_count >= _ABSTRACT_SEARCH_MIN_WORDS:
            logger.info(
                "rag search_concrete_in_doc: using abstract search for long query "
                "(words=%d, top_k=%d, file_id=%s)",
                word_count,
                top_k,
                file_id,
            )
            return await self.search_abstract_in_doc(
                file_id=file_id,
                query=query,
                top_k=top_k,
            )

        qwen_query = ("Instruct: Given a web search query, retrieve relevant passages that answer the query"
                f"Query: {query}")
        query_embedding = self._embed_query(qwen_query)
        return await self._store.call_search_concrete_chunks(
            file_id=file_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
            tsv_weight=tsv_weight,
        )

    async def get_expanded_context_by_index(
        self,
        file_id: str,
        chunk_index: int,
        distance: int = 1,
    ) -> list[ChunkRow]:
        """Return chunks around the selected chunk_index within a file."""
        return await self._store.get_expanded_context_by_index(
            file_id=file_id,
            chunk_index=chunk_index,
            distance=distance,
        )

    async def get_table_rows_by_range(
        self,
        file_id: str,
        row_start: int,
        row_end: int,
    ) -> list[ChunkSearchResult]:
        """Return table rows from a specific row_index range within a file."""
        return await self._store.get_table_rows_by_range(
            file_id=file_id,
            row_start=row_start,
            row_end=row_end,
        )
