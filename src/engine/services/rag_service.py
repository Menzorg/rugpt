from __future__ import annotations

import mimetypes
import re
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from bs4 import BeautifulSoup
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from tika import parser

from ..config import Config
from ..storage.rag_store import RAG_store

TABLE_MIME_TYPES = {
    "text/csv",
    "text/tab-separated-values",
    "application/csv",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/x-vnd.oasis.opendocument.spreadsheet",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.ms-excel.sheet.binary.macroenabled.12",
    "application/wps-office.xlsx",
    "application/wps-office.xls",
    "application/x-wps-office.xlsx",
    "application/x-wps-office.xls",
    "application/kswps",
}


def _safe_tika_file_name(file_name: str | None) -> str:
    raw_name = (file_name or "uploaded_file").strip() or "uploaded_file"
    return quote(raw_name, safe="")


def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


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
        ollama_model: str,
        ollama_embeddings_base_url: str,
        ollama_base_url: str,
        chunk_size: int,
        chunk_overlap: int,
        summary_input_max_chars: int,
    ) -> None:
        self._store = store or RAG_store(
            dsn=Config.RAG_STORE_DSN,
            vector_dim=Config.RAG_VECTOR_DIM,
        )
        self._embeddings = OllamaEmbeddings(
            model=ollama_model,
            base_url=ollama_embeddings_base_url,
        )
        self._summary_llm = ChatOllama(  # type: ignore[call-arg]
            model=Config.RAG_SUMMARY_MODEL,
            base_url=ollama_base_url,
            temperature=0,
        )
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self._tika_server_endpoint = Config.RAG_TIKA_SERVER_ENDPOINT
        self._summary_input_max_chars = summary_input_max_chars

    def _embed_query(self, query: str) -> list[float]:
        return self._embeddings.embed_query(query)

    def _extract_text_with_tika(self, file_bytes: bytes, file_name: str) -> str:
        parsed = parser.from_buffer(
            file_bytes,
            serverEndpoint=self._tika_server_endpoint,
            headers={"X-File-Name": _safe_tika_file_name(file_name)},
        )
        content = _extract_tika_content(parsed)
        if not content:
            return ""
        return content.strip()

    def _is_table_file(self, content_type: str | None, file_name: str | None) -> bool:
        detected_mime = (content_type or "").split(";")[0].strip().lower()
        if detected_mime in TABLE_MIME_TYPES:
            return True
        guessed_mime, _ = mimetypes.guess_type(file_name or "")
        return (guessed_mime or "").lower() in TABLE_MIME_TYPES

    def _parse_table_rows(
        self,
        file_bytes: bytes,
        content_type: str | None,
        file_name: str | None,
    ) -> tuple[list[str], list[str]]:
        detected_mime = (content_type or "").split(";")[0].strip().lower()
        guessed_mime, _ = mimetypes.guess_type(file_name or "")
        mime = detected_mime or (guessed_mime or "").lower()

        if mime not in TABLE_MIME_TYPES:
            raise ValueError(f"Unsupported table MIME type: {mime or 'unknown'}")

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

    def _generate_summary_with_llm(self, text: str) -> str:
        source_text = text[: self._summary_input_max_chars]
        prompt = (
            "Ты делаешь краткое резюме документа для поиска.\n"
            "Правила:\n"
            "- Пиши только по-русски.\n"
            "- Без воды, только факты.\n"
            "- Сохраняй сущности, названия организаций, номера, даты и суммы как в тексте.\n"
            "- Формат: 4-7 предложений, без буллетов.\n"
            "- Не пиши про количество записей. ты видишь ограниченное их количество\n\n"
            f"Документ:\n{source_text}"
        )
        result = self._summary_llm.invoke(prompt)
        summary = str(result.content).strip()
        if not summary:
            raise ValueError("LLM returned empty summary.")
        return summary

    async def ingest(
        self,
        *,
        org_id: str,
        user_id: str | None,
        filename: str | None,
        content_type: str | None,
        data: bytes,
    ) -> dict[str, str | int | bool]:
        if not data:
            raise ValueError("Uploaded file is empty.")

        doc_id = str(uuid4())
        is_table = self._is_table_file(content_type, filename)

        if is_table:
            try:
                headers, table_rows = self._parse_table_rows(data, content_type, filename)
            except Exception as exc:
                raise ValueError(f"Table parsing failed: {exc}") from exc
            if not table_rows:
                raise ValueError("No table rows extracted from file.")

            try:
                row_embeddings = self._embeddings.embed_documents(table_rows)
            except Exception as exc:
                raise ValueError(f"Table rows embedding failed: {exc}") from exc

            summary_source = self._build_table_summary_source(filename, headers, table_rows)
            try:
                summary = self._generate_summary_with_llm(summary_source)
            except Exception as exc:
                raise ValueError(f"Summary generation failed: {exc}") from exc

            try:
                summary_embedding = self._embeddings.embed_query(summary)
            except Exception as exc:
                raise ValueError(f"Summary embedding failed: {exc}") from exc

            try:
                await self._store.insert_table_document_with_rows(
                    doc_id=doc_id,
                    doc_title=filename or doc_id,
                    summary=summary,
                    summary_embedding=summary_embedding,
                    org_id=org_id,
                    user_id=user_id,
                    rows_text=table_rows,
                    row_embeddings=row_embeddings,
                )
            except Exception as exc:
                raise ValueError(f"Database write failed: {exc}") from exc

            return {"doc_id": doc_id, "chunks_ingested": len(table_rows), "is_table": True}

        try:
            full_text = self._extract_text_with_tika(data, filename or "uploaded_file")
        except Exception as exc:
            raise ValueError(f"Tika extraction failed: {exc}") from exc

        if not full_text:
            raise ValueError("No text content extracted from file.")

        chunks = self._splitter.split_text(full_text)
        if not chunks:
            raise ValueError("Text splitting produced no chunks.")

        try:
            chunk_embeddings = self._embeddings.embed_documents(chunks)
        except Exception as exc:
            raise ValueError(f"Chunk embedding failed: {exc}") from exc

        try:
            summary = self._generate_summary_with_llm(full_text)
        except Exception as exc:
            raise ValueError(f"Summary generation failed: {exc}") from exc

        try:
            summary_embedding = self._embeddings.embed_query(summary)
        except Exception as exc:
            raise ValueError(f"Summary embedding failed: {exc}") from exc

        try:
            await self._store.insert_document_with_chunks(
                doc_id=doc_id,
                doc_title=filename or doc_id,
                summary=summary,
                summary_embedding=summary_embedding,
                is_table=False,
                org_id=org_id,
                user_id=user_id,
                chunks=chunks,
                chunk_embeddings=chunk_embeddings,
            )
        except Exception as exc:
            raise ValueError(f"Database write failed: {exc}") from exc

        return {"doc_id": doc_id, "chunks_ingested": len(chunks), "is_table": False}

    async def find_docs(
        self,
        org_id: str,
        user_id: str | None,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Return top-k related docs in org/user scope using SQL hybrid search."""
        query_embedding = self._embed_query(query)
        return await self._store.call_search_related_docs(
            org_id=org_id,
            user_id=user_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
        )

    async def search_abstract_in_doc(
        self,
        doc_id: str,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Return top-k abstract matches inside one doc."""
        query_embedding = self._embed_query(query)
        return await self._store.call_search_abstract_chunks(
            doc_id=doc_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
        )

    async def search_concrete_in_doc(
        self,
        doc_id: str,
        query: str,
        top_k: int,
        tsv_weight: float,
    ) -> list[dict[str, Any]]:
        """Return top-k concrete matches inside one doc."""
        query_embedding = self._embed_query(query)
        return await self._store.call_search_concrete_chunks(
            doc_id=doc_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
            tsv_weight=tsv_weight,
        )
