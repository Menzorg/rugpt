from __future__ import annotations

from src.engine.unified_logger import get_logger
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from uuid import UUID

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_DOC_SUMMARY_PROMPT: str = (_PROMPTS_DIR / "rag_doc_summary.md").read_text(encoding="utf-8")
_TABLE_SUMMARY_PROMPT: str = (_PROMPTS_DIR / "rag_table_summary.md").read_text(encoding="utf-8")
_IMAGE_SUMMARY_PROMPT: str = (_PROMPTS_DIR / "rag_image_summary.md").read_text(encoding="utf-8")

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from tika import parser

from ..agents.metadata import build_initial_extra_body, resolve_litellm_session_id
from ..config import Config
from ..constants import IMAGE_TYPES
from ..models.rag import ChunkRow, ChunkSearchResult, RelatedDoc
from ..models.user_file import UserFile
from ..storage.content_type_storage import ContentTypeStorage
from ..storage.rag_store import RAG_store
from ..storage.user_file_storage import UserFileStorage
from ..utils.image_parser import image_bytes_to_data_url

logger = get_logger("services")
_ABSTRACT_SEARCH_MIN_WORDS = 5
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_DOCUMENT_SEARCH_INSTRUCT = (
    "Search for documents matching the user's request, including matching "
    "document summaries, document parameters, manual comments, and business categories"
)
_PASSAGE_SEARCH_INSTRUCT = (
    "Given a web search query, retrieve relevant passages that answer the query."
)


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

def _col_letter(idx: int) -> str:
    """Convert 0-based column index to Excel column letter (0→A, 25→Z, 26→AA)."""
    label = ""
    n = idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        label = chr(65 + rem) + label
    return label


def _format_row(headers: list[str], values: list[str], sheet_name: str | None = None) -> str:
    # Row format: "Sheet:SheetName, A:ColumnHeader=value, B:ColumnHeader=value, ..."
    # Column letters follow Excel convention (A–Z, then AA, AB, ...; max 16 384 columns = XFD).
    pairs = [f"{_col_letter(i)}:{header}={value}" for i, (header, value) in enumerate(zip(headers, values))]
    if sheet_name:
        pairs.insert(0, f"Sheet:{sheet_name}")
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
        content_type_storage: ContentTypeStorage | None = None,
    ) -> None:
        self._store = store or RAG_store(
            dsn=Config.RAG_STORE_DSN,
            vector_dim=Config.RAG_VECTOR_DIM,
        )
        # UserFileStorage для обновления rag_status в процессе индексации.
        # Опциональный: если не передан, обновление статусов не производится.
        self._file_storage = file_storage
        self._content_type_storage = content_type_storage
        self._embeddings = OpenAIEmbeddings(
            model=embedding_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            timeout=180
        )
        # TODO: Refactor. Use agentexecutor with invocation kind system for summaries
        self._summary_llm = ChatOpenAI(
            model=Config.RAG_SUMMARY_MODEL,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0,
            max_tokens=4096
        )
        self._image_summary_llm = ChatOpenAI(
            model=Config.IMAGE_ANALYSIS_MODEL,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0,
            max_tokens=4096,
            timeout=120,
        )
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        self._tika_server_endpoint = Config.RAG_TIKA_SERVER_ENDPOINT
        self._summary_input_max_tokens = summary_input_max_tokens

    def _build_embedding_extra_body(self) -> dict[str, Any]:
        return build_initial_extra_body(
            litellm_session_id=resolve_litellm_session_id(),
            agent_name="rag_embedding",
            chat_id=None,
        )

    async def _embed_query(self, query: str, instruct: str | None = None) -> list[float]:
        if instruct:
            query = f"Instruct: {instruct}\nQuery:{query}"
        return self._embeddings.aembed_query(
            query,
            extra_body=self._build_embedding_extra_body(),
        )

    async def _embed_documents(self, documents: list[str]) -> list[list[float]]:
        return await self._embeddings.aembed_documents(
            documents,
            extra_body=self._build_embedding_extra_body(),
        )

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
    ) -> tuple[list[str], list[dict], list[dict]]:
        """Parse an xlsx file with pylightxl and emit one dict per data row.

        Stage 1 — read workbook.
            pylightxl.readxl() returns a Workbook; ws.rows is a property that
            returns an iterator of rows as lists of raw cell values (strings/ints/floats).

        Stage 2 — per-sheet scan.
            enumerate(ws.row) yields (0-based index, row).  We add 1 so
            `excel_row` matches what the user sees in Excel (row 1 = header row,
            row 2 = first data row, etc.).  Blank rows are skipped; the first
            non-blank row is treated as the header.

        Stage 3 — build output.
            row_index stored in the DB == Excel row number (1-based).
            This means LLM can tell the user "see Excel row 5" and it will
            be correct.  sheet_infos records the min/max Excel row numbers
            for the data rows (not counting the header) so the summary block
            injected into the document accurately describes ranges a human
            can navigate to.

        Returns:
            summary_headers: flat list of "SheetName.ColumnHeader" strings
            rows_with_meta:  list of {text, sheet_name, row_index} dicts —
                             row_index is the 1-based Excel row number
            sheet_infos:     list of {sheet, min_row, max_row} dicts —
                             min_row/max_row are 1-based Excel row numbers
                             for the data rows of that sheet
        """
        import io
        import pylightxl as xl

        # Stage 1: read workbook from bytes — no temp file needed
        db = xl.readxl(fn=io.BytesIO(file_bytes))
        summary_headers: list[str] = []
        rows_with_meta: list[dict] = []
        sheet_infos: list[dict] = []

        for ws_name in db.ws_names:
            ws = db.ws(ws=ws_name)

            # Stage 2: collect non-blank rows with their real Excel row numbers.
            # enumerate gives 0-based index; +1 converts to 1-based Excel row.
            data_rows = [
                (i + 1, row)
                for i, row in enumerate(ws.rows)
                if any(str(c).strip() for c in row)
            ]
            if not data_rows:
                continue

            # First non-blank row is the header (Excel row N, typically row 1).
            header_excel_row, header_row = data_rows[0]
            header_cells = [_normalize_cell(c) for c in header_row]
            headers = [v if v else f"Column{i + 1}" for i, v in enumerate(header_cells)]
            summary_headers.extend([f"{ws_name}.{h}" for h in headers])

            # Stage 3: remaining non-blank rows are data rows.
            # row_index == excel_row so the LLM and the user share the same coordinates.
            sheet_data_rows = data_rows[1:]
            for excel_row, row in sheet_data_rows:
                padded = [_normalize_cell(c) for c in row] + [""] * (len(headers) - len(row))
                text = _format_row(headers, padded[: len(headers)], sheet_name=ws_name)
                rows_with_meta.append({
                    "text": text,
                    "sheet_name": ws_name,
                    "row_index": excel_row,  # 1-based Excel row number
                })

            if sheet_data_rows:
                sheet_infos.append({
                    "sheet": ws_name,
                    "min_row": sheet_data_rows[0][0],   # Excel row of first data row
                    "max_row": sheet_data_rows[-1][0],  # Excel row of last data row
                })

        return summary_headers, rows_with_meta, sheet_infos

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

    async def _generate_summary_with_llm(self, text: str, *, is_table: bool = False) -> str:
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
        result = await self._summary_llm.ainvoke(messages)
        summary = str(result.content).strip()
        if not summary:
            raise ValueError("LLM returned empty summary.")
        return summary

    async def _generate_image_summary(self, data: bytes, file_type: str) -> str:
        data_url = image_bytes_to_data_url(data, file_type=file_type)
        result = await self._image_summary_llm.ainvoke([
            HumanMessage(content=[
                {"type": "text", "text": _IMAGE_SUMMARY_PROMPT.strip()},
                {"type": "image_url", "image_url": {"url": data_url}},
            ])
        ])
        summary = str(result.content).strip()
        if not summary:
            raise ValueError("LLM returned empty image summary.")
        return summary

    def _build_embedding_text(self, summary: str, file_record: UserFile | None) -> str:
        return summary

    def _build_manual_comment_embedding(self, file_record: UserFile | None) -> list[float] | None:
        if (
            file_record is None
            or file_record.content_type_id is not None
            or not file_record.comment
        ):
            return None
        return self._embed_query(file_record.comment)

    async def _update_manual_comment_embedding(
        self,
        file_id: UUID,
        file_record: UserFile | None,
    ) -> None:
        comment_embedding = self._build_manual_comment_embedding(file_record)
        if comment_embedding is None or self._file_storage is None or file_record is None:
            return
        await self._file_storage.update_comment(
            file_id=file_id,
            comment=file_record.comment,
            content_type_id=None,
            comment_embedding=comment_embedding,
        )

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
        is_invoice: bool = False,
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
        file_record = None
        if self._file_storage:
            file_record = await self._file_storage.get_by_id(file_id)
            if file_record:
                is_table = file_record.is_table
        is_image = bool(file_record and file_record.file_type in IMAGE_TYPES)
        logger.info(f"[{fid}] is_table={is_table} is_image={is_image}")

        # Single try/except wraps all pipeline stages.
        # `stage` is updated before each step so the except block
        # can report exactly where the failure occurred.
        stage = "init"
        try:
            if is_image:
                if is_invoice:
                    stage = "image_summary"
                    logger.info(f"[{fid}] stage={stage}")
                    summary = await self._generate_image_summary(data, file_record.file_type)
                    logger.info(f"[{fid}] image summary generated ({len(summary)} chars)")

                    stage = "summary_embedding"
                    logger.info(f"[{fid}] stage={stage}")
                    embedding_text = self._build_embedding_text(summary, file_record)
                    summary_embedding = await self._embed_query(embedding_text)

                    stage = "db_write"
                    logger.info(f"[{fid}] stage={stage}")
                    await self._store.update_user_file_summary(
                        file_id=str(file_id),
                        summary=summary,
                        summary_embedding=summary_embedding,
                    )
                    await self._update_manual_comment_embedding(file_id, file_record)
                    # Per design: image invoices get a summary but are NOT marked indexed.
                    await self.set_status(file_id, "not_indexed")
                    logger.info(f"[{fid}] ingest completed (image invoice) — summarized")
                    return {"file_id": fid, "image": True, "summarized": True}
                # Non-invoice image: no LLM call, leave it un-indexed.
                await self.set_status(file_id, "not_indexed")
                logger.info(f"[{fid}] skipping image (not an invoice) — no summary generated")
                return {"file_id": fid, "image": True, "summarized": False}

            if is_table:
                stage = "table_parsing"
                logger.info(f"[{fid}] stage={stage}")
                summary_headers, rows_with_meta, sheet_infos = self._parse_table_rows(data, filename)
                if not rows_with_meta:
                    raise ValueError("No table rows extracted from file.")
                row_texts = [r["text"] for r in rows_with_meta]
                sheet_names = [r["sheet_name"] for r in rows_with_meta]
                row_indexes = [r["row_index"] for r in rows_with_meta]
                logger.info(
                    f"[{fid}] parsed {len(row_texts)} rows across {len(sheet_infos)} sheets"
                )

                stage = "table_embedding"
                logger.info(f"[{fid}] stage={stage}")
                row_embeddings = await self._embed_documents(row_texts)
                logger.info(f"[{fid}] embedded {len(row_embeddings)} row vectors")

                stage = "summary_generation"
                logger.info(f"[{fid}] stage={stage}")
                structure_lines = ["<Table structure>"]
                for si in sheet_infos:
                    structure_lines.append(
                        f"  Лист «{si['sheet']}»: строки {si['min_row']}–{si['max_row']}"
                    )
                structure_lines.append("</Table structure>")
                structure_block = "\n".join(structure_lines)
                summary_source = (
                    + f"<structure>{structure_block}<structure>"
                    + "<system>Структуру, не пересказывать</system>\n\n"
                    + self._build_table_summary_source(filename, summary_headers, row_texts)
                )
                summary = await self._generate_summary_with_llm(summary_source, is_table=True)
                summary = (
                    f"Количество строк в таблице: {len(row_texts)}\n"
                    + structure_block
                    + "\n"
                    + summary
                )
                logger.info(f"[{fid}] summary generated ({len(summary)} chars)")

                stage = "summary_embedding"
                logger.info(f"[{fid}] stage={stage}")
                embedding_text = self._build_embedding_text(summary, file_record)
                summary_embedding = await self._embed_query(embedding_text)

                stage = "db_write"
                logger.info(f"[{fid}] stage={stage}")
                await self._store.insert_rows_chunks_and_update_table_summary(
                    file_id=str(file_id),
                    summary=summary,
                    summary_embedding=summary_embedding,
                    rows_text=row_texts,
                    row_embeddings=row_embeddings,
                    sheet_names=sheet_names,
                    row_indexes=row_indexes,
                )
                await self._update_manual_comment_embedding(file_id, file_record)

                await self.set_status(file_id, "indexed")
                logger.info(f"[{fid}] ingest completed (table) — rows_ingested={len(row_texts)}")
                return {"file_id": fid, "chunks_ingested": len(row_texts)}

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
            chunk_embeddings = await self._embed_documents(chunks)
            logger.info(f"[{fid}] embedded {len(chunk_embeddings)} chunk vectors")

            stage = "summary_generation"
            logger.info(f"[{fid}] stage={stage}")
            summary = await self._generate_summary_with_llm(full_text)
            logger.info(f"[{fid}] summary generated ({len(summary)} chars)")

            stage = "summary_embedding"
            logger.info(f"[{fid}] stage={stage}")
            embedding_text = self._build_embedding_text(summary, file_record)
            summary_embedding = await self._embed_query(embedding_text)

            stage = "db_write"
            logger.info(f"[{fid}] stage={stage}")
            await self._store.insert_chunks_and_update_document_summary(
                file_id=str(file_id),
                summary=summary,
                summary_embedding=summary_embedding,
                chunks=chunks,
                chunk_embeddings=chunk_embeddings,
            )
            await self._update_manual_comment_embedding(file_id, file_record)

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
        is_invoice: bool = False,
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
                    is_invoice=is_invoice,
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
        is_admin: bool = False,
        filter_user_id: str | None = None,
        exclude_images: bool = True,
        search_mode: str = "abstract",
        filter_content_type_id: str | None = None,
    ) -> list[RelatedDoc]:
        """Return top-k related docs in org/user scope using SQL hybrid search."""
        logger.info(
            "rag find_docs start: query=%r mode=%s top_k=%d org=%s user=%s filter_user=%s is_admin=%s exclude_images=%s filter_content_type=%s",
            query, search_mode, top_k, org_id, user_id, filter_user_id, is_admin, exclude_images, filter_content_type_id,
        )
        query_embedding = await self._embed_query(query, instruct=_DOCUMENT_SEARCH_INSTRUCT)
        docs = await self._store.call_search_related_docs(
            org_id=org_id,
            user_id=user_id,
            is_admin=is_admin,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
            filter_user_id=filter_user_id,
            exclude_images=exclude_images,
            search_mode=search_mode,
            filter_content_type_id=filter_content_type_id,
        )
        logger.info(
            "rag find_docs done: query=%r mode=%s returned=%d top_k=%d",
            query, search_mode, len(docs), top_k,
        )
        return docs

    async def search_abstract_in_doc(
        self,
        file_id: str,
        query: str,
        top_k: int,
    ) -> list[ChunkSearchResult]:
        """Return top-k abstract matches inside one file."""
        logger.info(
            "rag search_in_doc start: file_id=%s mode=abstract query=%r top_k=%d",
            file_id, query, top_k,
        )
        query_embedding = await self._embed_query(query)
        chunks = await self._store.call_search_abstract_chunks(
            file_id=file_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
        )
        logger.info(
            "rag search_in_doc done: file_id=%s mode=abstract returned=%d top_k=%d",
            file_id, len(chunks), top_k,
        )
        return chunks

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
                "rag search_in_doc switch: file_id=%s query=%r requested=concrete actual=abstract words=%d top_k=%d",
                file_id, query, word_count, top_k,
            )
            return await self.search_abstract_in_doc(
                file_id=file_id,
                query=query,
                top_k=top_k,
            )

        logger.info(
            "rag search_in_doc start: file_id=%s mode=concrete query=%r top_k=%d words=%d",
            file_id, query, top_k, word_count,
        )
        query_embedding = await self._embed_query(query, instruct=_PASSAGE_SEARCH_INSTRUCT)
        chunks = await self._store.call_search_concrete_chunks(
            file_id=file_id,
            query=query,
            query_embedding=query_embedding,
            top_k=top_k,
            tsv_weight=tsv_weight,
        )
        logger.info(
            "rag search_in_doc done: file_id=%s mode=concrete returned=%d top_k=%d",
            file_id, len(chunks), top_k,
        )
        return chunks

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
        sheet_name: str | None = None,
    ) -> list[ChunkSearchResult]:
        """Return table rows from a specific row_index range within a file."""
        return await self._store.get_table_rows_by_range(
            file_id=file_id,
            row_start=row_start,
            row_end=row_end,
            sheet_name=sheet_name,
        )
