#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.engine.config import Config
from src.engine.constants import CONTENT_TYPES
from src.engine.models.user_file import UserFile
from src.engine.services.converter_service import ConverterService
from src.engine.services.rag_service import RAGService
from src.engine.storage.rag_store import RAG_store
from src.engine.storage.storage_adapter import LocalStorageAdapter
from src.engine.storage.user_file_storage import UserFileStorage


LOGGER_NAME = "table_reingest"


@dataclass
class ScriptRuntimeContext:
    file_storage: UserFileStorage
    rag_store: RAG_store
    storage_adapter: LocalStorageAdapter
    converter: ConverterService
    rag_service: RAGService


@dataclass
class StorageGroupResult:
    old_key: str
    row_ids: list[str]
    converted: bool
    new_key: str | None = None
    new_filename: str | None = None
    old_size: int | None = None
    new_size: int | None = None
    content_hash: str | None = None
    db_update_count: int = 0


@dataclass
class RunStats:
    selected_file_ids: list[str] = field(default_factory=list)
    converted_groups: list[StorageGroupResult] = field(default_factory=list)
    unchanged_groups: list[StorageGroupResult] = field(default_factory=list)
    rebuilt_file_ids: list[str] = field(default_factory=list)
    skipped_file_ids: list[str] = field(default_factory=list)
    failed_file_ids: list[str] = field(default_factory=list)
    orphan_candidate_keys: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    """Read CLI flags and return argparse namespace with execution mode."""
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run by default. Reingest indexed table files with --execute."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform storage, DB, and RAG mutations. Without this flag the script is dry-run only.",
    )
    return parser.parse_args()


def setup_logging(execute: bool) -> tuple[logging.Logger, Path]:
    """Create console/file logging and return configured logger plus log path."""
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    suffix = "execute" if execute else "dry_run"
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"table_reingest_{timestamp}_{suffix}.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    return logger, log_path


def redact_dsn(dsn: str) -> str:
    """Take a DSN string and return the same DSN with password masked for logs."""
    try:
        parts = urlsplit(dsn)
    except Exception:
        return "<unparseable dsn>"
    if not parts.password:
        return dsn
    username = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def confirm(prompt: str) -> bool:
    """Ask operator for explicit approval and return True only for 'yes'."""
    answer = input(f"{prompt} Type 'yes' to continue: ").strip().lower()
    return answer == "yes"


def content_hash(data: bytes) -> str:
    """Take file bytes and return their SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def with_xlsx_extension(value: str) -> str:
    """Take a filename/storage key and return it with an .xlsx suffix."""
    path = PurePosixPath(value)
    if path.suffix:
        return str(path.with_suffix(".xlsx"))
    return f"{value}.xlsx"


def row_count_from_status(status: str) -> int:
    """Parse asyncpg command status like 'UPDATE 3' into affected row count."""
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


async def init_minimal_services(logger: logging.Logger) -> ScriptRuntimeContext:
    """Initialize only services needed by the script and return runtime context."""
    # Checking storage backend.
    if Config.STORAGE_BACKEND != "local":
        raise RuntimeError(
            f"Unsupported STORAGE_BACKEND={Config.STORAGE_BACKEND!r}; "
            "this maintenance script currently supports only local storage."
        )

    # Creating minimal services.
    file_storage = UserFileStorage(postgres_dsn=Config.get_postgres_dsn())
    rag_store = RAG_store(dsn=Config.RAG_STORE_DSN, vector_dim=Config.RAG_VECTOR_DIM)
    storage_adapter = LocalStorageAdapter(base_dir=Config.STORAGE_LOCAL_DIR)
    converter = ConverterService(unoserver_url=Config.UNOSERVER_URL)
    rag_service = RAGService(
        store=rag_store,
        embedding_model=Config.EMBEDDING_MODEL,
        llm_base_url=Config.LLM_BASE_URL,
        llm_api_key=Config.LLM_API_KEY,
        chunk_size=Config.RAG_CHUNK_SIZE,
        chunk_overlap=Config.RAG_CHUNK_OVERLAP,
        summary_input_max_tokens=Config.RAG_SUMMARY_INPUT_MAX_TOKENS,
        file_storage=file_storage,
    )

    # Opening DB pools and converter session.
    await file_storage.init()
    logger.info("Initialized UserFileStorage")
    await rag_store.init()
    logger.info("Initialized RAG_store")
    await converter.startup()
    logger.info("Initialized ConverterService")

    return ScriptRuntimeContext(
        file_storage=file_storage,
        rag_store=rag_store,
        storage_adapter=storage_adapter,
        converter=converter,
        rag_service=rag_service,
    )


async def close_services(ctx: ScriptRuntimeContext | None, logger: logging.Logger) -> None:
    """Close initialized script resources; accepts None for early failures."""
    if ctx is None:
        return
    # Closing opened resources.
    try:
        await ctx.converter.shutdown()
        logger.info("Closed ConverterService")
    finally:
        await ctx.rag_store.close()
        logger.info("Closed RAG_store")
        await ctx.file_storage.close()
        logger.info("Closed UserFileStorage")


async def fetch_active_files(ctx: ScriptRuntimeContext) -> list[UserFile]:
    """Fetch active user_files rows and map DB records into UserFile objects."""
    # Getting all active files.
    rows = await ctx.file_storage.fetch(
        """
        SELECT *
        FROM user_files
        WHERE is_active = true
        ORDER BY created_at ASC
        """
    )
    return [ctx.file_storage._row_to_file(row) for row in rows]


def select_candidates(
    active_files: list[UserFile],
) -> tuple[list[UserFile], Counter[str]]:
    """Filter active files into indexed table candidates and skipped reason counts."""
    candidates: list[UserFile] = []
    skip_reasons: Counter[str] = Counter()

    for file in active_files:
        # Keeping only table files.
        if not file.is_table:
            skip_reasons["not_table"] += 1
            continue
        if file.rag_status != "indexed":
            skip_reasons[f"rag_status:{file.rag_status}"] += 1
            continue
        candidates.append(file)

    return candidates, skip_reasons


def group_by_storage_key(files: list[UserFile]) -> dict[str, list[UserFile]]:
    """Group UserFile records by storage_key and return key-to-files mapping."""
    groups: dict[str, list[UserFile]] = defaultdict(list)
    for file in files:
        # Grouping files that share one blob.
        groups[file.storage_key].append(file)
    return dict(groups)


def is_xlsx_group(rows: list[UserFile]) -> bool:
    """Return True when every row in a storage group already points to .xlsx."""
    # Checking already-normal xlsx files.
    return all(
        row.file_type.lower() == "xlsx"
        and row.original_filename.lower().endswith(".xlsx")
        and row.storage_key.lower().endswith(".xlsx")
        for row in rows
    )


def log_inventory(
    logger: logging.Logger,
    active_files: list[UserFile],
    candidates: list[UserFile],
    candidate_groups: dict[str, list[UserFile]],
    skip_reasons: Counter[str],
) -> None:
    """Log scan totals, skip reasons, group counts, and selected file details."""
    # Logging inventory summary.
    xlsx_groups = sum(1 for rows in candidate_groups.values() if is_xlsx_group(rows))
    convert_groups = len(candidate_groups) - xlsx_groups

    logger.info("")
    logger.info("Inventory summary")
    logger.info("  active files: %d", len(active_files))
    logger.info("  selected indexed table files: %d", len(candidates))
    logger.info("  distinct selected storage keys: %d", len(candidate_groups))
    logger.info("  .xlsx groups: %d", xlsx_groups)
    logger.info("  non-.xlsx groups requiring conversion: %d", convert_groups)
    logger.info("  skipped active files by reason: %s", dict(skip_reasons))

    logger.info("")
    logger.info("Selected files")
    for file in candidates:
        logger.info(
            "  id=%s org_id=%s user_id=%s filename=%r file_type=%s "
            "storage_key=%r size=%s indexed_at=%s",
            file.id,
            file.org_id,
            file.user_id,
            file.original_filename,
            file.file_type,
            file.storage_key,
            file.file_size,
            file.indexed_at,
        )


async def update_storage_key_rows(
    ctx: ScriptRuntimeContext,
    old_key: str,
    *,
    new_key: str,
    new_filename: str,
    file_size: int,
    digest: str,
) -> int:
    """Update active rows sharing old_key to converted .xlsx metadata; return row count."""
    # Updating rows that share the old blob.
    status = await ctx.file_storage.execute(
        """
        UPDATE user_files
        SET
            storage_key = $2,
            original_filename = $3,
            file_type = 'xlsx',
            file_size = $4,
            content_hash = $5,
            is_table = true,
            updated_at = NOW()
        WHERE storage_key = $1
          AND is_active = true
        """,
        old_key,
        new_key,
        new_filename,
        file_size,
        digest,
    )
    return row_count_from_status(status)


async def normalize_storage_groups(
    ctx: ScriptRuntimeContext,
    candidate_groups: dict[str, list[UserFile]],
    all_active_by_key: dict[str, list[UserFile]],
    *,
    execute: bool,
    logger: logging.Logger,
) -> tuple[list[StorageGroupResult], list[StorageGroupResult], list[str]]:
    """Convert non-xlsx blob groups, update rows if in execute mode, and return group results."""
    converted: list[StorageGroupResult] = []
    unchanged: list[StorageGroupResult] = []
    orphan_candidates: list[str] = []

    logger.info("")
    logger.info("Storage normalization")
    for old_key, selected_rows in candidate_groups.items():
        # Getting clones/shared rows of this file.
        affected_rows = all_active_by_key.get(old_key, selected_rows)
        representative = selected_rows[0]
        row_ids = [str(row.id) for row in affected_rows]

        logger.info("")
        logger.info("Storage group start")
        logger.info("  old_key=%r", old_key)
        logger.info(
            "  representative id=%s filename=%r file_type=%s",
            representative.id,
            representative.original_filename,
            representative.file_type,
        )
        logger.info("  selected row ids=%s", [str(row.id) for row in selected_rows])
        logger.info("  affected active row ids=%s", row_ids)

        if is_xlsx_group(selected_rows):
            # Skipping conversion for xlsx blobs.
            result = StorageGroupResult(
                old_key=old_key,
                row_ids=row_ids,
                converted=False,
            )
            unchanged.append(result)
            logger.info("  unchanged: group already uses .xlsx metadata and key")
            continue

        new_key = with_xlsx_extension(old_key)
        new_filename = with_xlsx_extension(representative.original_filename)
        result = StorageGroupResult(
            old_key=old_key,
            row_ids=row_ids,
            converted=True,
            new_key=new_key,
            new_filename=new_filename,
        )

        if not execute:
            # Logging planned conversion only.
            converted.append(result)
            orphan_candidates.append(old_key)
            logger.info("  dry-run conversion planned")
            logger.info("  planned_new_key=%r", new_key)
            logger.info("  planned_new_filename=%r", new_filename)
            logger.info("  planned affected row count=%d", len(row_ids))
            continue

        # Reading old blob.
        logger.info("  downloading old blob")
        data = await ctx.storage_adapter.read(old_key)
        result.old_size = len(data)
        logger.info("  old byte size=%d", len(data))

        # Running conversion
        logger.info("  converting blob to .xlsx via ConverterService")
        converted_data = await ctx.converter.to_xlsx(data, representative.original_filename)
        digest = content_hash(converted_data)
        result.new_size = len(converted_data)
        result.content_hash = digest
        logger.info("  converted byte size=%d", len(converted_data))
        logger.info("  converted sha256=%s", digest)

        # Saving converted blob.
        logger.info("  saving converted blob new_key=%r", new_key)
        await ctx.storage_adapter.save(
            new_key,
            converted_data,
            CONTENT_TYPES["xlsx"],
        )

        logger.info("  updating active user_files rows with old storage_key")
        result.db_update_count = await update_storage_key_rows(
            ctx,
            old_key,
            new_key=new_key,
            new_filename=new_filename,
            file_size=len(converted_data),
            digest=digest,
        )
        logger.info("  DB update row count=%d", result.db_update_count)

        converted.append(result)
        orphan_candidates.append(old_key)

    return converted, unchanged, orphan_candidates


async def get_current_file(ctx: ScriptRuntimeContext, file_id: Any) -> UserFile:
    """Load a current active file row by id or raise if it disappeared."""
    # Getting fresh file row.
    file = await ctx.file_storage.get_by_id(file_id)
    if file is None:
        raise FileNotFoundError(f"user_files row not found or inactive: {file_id}")
    return file


async def reingest_one_file(
    ctx: ScriptRuntimeContext,
    file_id: Any,
    logger: logging.Logger,
) -> dict[str, str | int | bool]:
    """Download one file, delete its old RAG rows, reingest it, and return result."""
    file = await get_current_file(ctx, file_id)
    logger.info(
        "  current row id=%s filename=%r storage_key=%r file_size=%s",
        file.id,
        file.original_filename,
        file.storage_key,
        file.file_size,
    )

    # Reading current blob.
    data = await ctx.storage_adapter.read(file.storage_key)
    logger.info("  downloaded current blob bytes=%d", len(data))
    logger.info(
        "  deleting old RAG rows for file id only; deletes chunks and "
        "tables_rows_chunks, sets rag_status='unindexed'"
    )
    # Deleting old chunks for this file.
    deleted = await ctx.rag_store.delete_chunks(str(file.id))
    logger.info("  delete_chunks returned=%s", deleted)

    # Running reingestion.
    started = time.monotonic()
    result = await ctx.rag_service.try_ingest(
        org_id=str(file.org_id),
        user_id=str(file.user_id),
        filename=file.original_filename,
        data=data,
        file_id=file.id,
    )
    elapsed = time.monotonic() - started
    final_status = await ctx.file_storage.get_status(file.id)
    logger.info("  reingest result=%s", result)
    logger.info("  final rag_status=%s elapsed=%.2fs", final_status, elapsed)
    return result


async def rebuild_rag(
    ctx: ScriptRuntimeContext,
    candidates: list[UserFile],
    *,
    execute: bool,
    logger: logging.Logger,
) -> tuple[list[str], list[str], list[str]]:
    """Process candidate files through RAG rebuild and return rebuilt/skipped/failed ids."""
    rebuilt: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    logger.info("")
    logger.info("RAG rebuild")
    if not execute:
        # Logging planned reingestion only.
        for file in candidates:
            logger.info(
                "  dry-run planned rebuild id=%s filename=%r storage_key=%r",
                file.id,
                file.original_filename,
                file.storage_key,
            )
        return rebuilt, skipped, failed

    for file in candidates:
        file_id = str(file.id)
        while True:
            # Reingesting one file.
            logger.info("")
            logger.info("RAG rebuild start file_id=%s", file_id)
            try:
                await reingest_one_file(ctx, file.id, logger)
            except Exception as exc:
                failed.append(file_id)
                logger.error("RAG rebuild failed file_id=%s: %s", file_id, exc)
                logger.error("%s", traceback.format_exc())
                decision = input(
                    f"RAG rebuild failed for {file_id}. Type retry, skip, or abort: "
                ).strip().lower()
                logger.info("Operator decision for file_id=%s: %s", file_id, decision)
                if decision == "retry":
                    # Retrying the same file.
                    if failed and failed[-1] == file_id:
                        failed.pop()
                    continue
                if decision == "skip":
                    # Skipping this file.
                    if failed and failed[-1] == file_id:
                        failed.pop()
                    skipped.append(file_id)
                    break
                raise RuntimeError(f"Aborted after failure for file {file_id}") from exc
            else:
                rebuilt.append(file_id)
                break

    return rebuilt, skipped, failed


def log_final_report(
    logger: logging.Logger,
    stats: RunStats,
    log_path: Path,
    elapsed: float,
) -> None:
    """Log final selected, converted, rebuilt, skipped, failed, and timing data."""
    logger.info("")
    logger.info("Final report")
    logger.info("  selected file count=%d", len(stats.selected_file_ids))
    logger.info(
        "  converted storage groups=%s",
        [
            {"old_key": item.old_key, "new_key": item.new_key, "rows": item.row_ids}
            for item in stats.converted_groups
        ],
    )
    logger.info(
        "  unchanged .xlsx groups=%s",
        [{"old_key": item.old_key, "rows": item.row_ids} for item in stats.unchanged_groups],
    )
    logger.info("  successfully rebuilt file ids=%s", stats.rebuilt_file_ids)
    logger.info("  skipped file ids=%s", stats.skipped_file_ids)
    logger.info("  failed file ids=%s", stats.failed_file_ids)
    logger.info(
        "  old converted storage keys left as orphan candidates=%s",
        stats.orphan_candidate_keys,
    )
    logger.info("  total elapsed=%.2fs", elapsed)
    logger.info("  log file=%s", log_path)


async def async_main() -> int:
    """Run full dry-run or execute pipeline and return process exit code."""
    # Stage 0: setup.
    args = parse_args()
    logger, log_path = setup_logging(args.execute)
    started = time.monotonic()
    ctx: ScriptRuntimeContext | None = None
    stats = RunStats()

    logger.info("Table reingest script started")
    logger.info("  timestamp=%s", datetime.now(UTC).isoformat())
    logger.info("  pid=%s", os.getpid())
    logger.info("  cwd=%s", Path.cwd())
    logger.info("  mode=%s", "execute" if args.execute else "dry-run")
    logger.info("  log file=%s", log_path)
    if not args.execute:
        logger.warning("")
        logger.warning(
            "DRY RUN: no storage writes, DB updates, chunk deletion, or RAG reingestion will run."
        )
        logger.warning("DRY RUN: use --execute to perform real conversion and reingestion.")

    logger.info("")
    logger.info("Preflight config")
    logger.info("  postgres_dsn=%s", redact_dsn(Config.get_postgres_dsn()))
    logger.info("  rag_store_dsn=%s", redact_dsn(Config.RAG_STORE_DSN))
    logger.info("  storage_backend=%s", Config.STORAGE_BACKEND)
    logger.info("  storage_dir=%s", Config.STORAGE_LOCAL_DIR)
    logger.info("  converter_url=%s", Config.UNOSERVER_URL)
    logger.info("  tika_url=%s", Config.RAG_TIKA_SERVER_ENDPOINT)
    logger.info("  embedding_model=%s", Config.EMBEDDING_MODEL)
    logger.info("  vector_dim=%s", Config.RAG_VECTOR_DIM)

    try:
        # Stage 1: preflight.
        ctx = await init_minimal_services(logger)

        if not confirm("Continue to scan files?"):
            logger.info("Operator declined after preflight")
            return 0

        # Stage 2: inventory.
        active_files = await fetch_active_files(ctx)
        candidates, skip_reasons = select_candidates(active_files)
        # Files we will reingest.
        candidate_groups = group_by_storage_key(candidates)
        # Clones/shared rows that must follow converted blobs.
        all_active_by_key = group_by_storage_key(active_files)
        stats.selected_file_ids = [str(file.id) for file in candidates]

        log_inventory(logger, active_files, candidates, candidate_groups, skip_reasons)

        if not candidates:
            logger.info("No indexed table files selected. Nothing to do.")
            return 0

        if not confirm("Continue with conversion/storage updates?"):
            logger.info("Operator declined before storage normalization")
            return 0

        # Stage 3: conversion.
        converted, unchanged, orphan_candidates = await normalize_storage_groups(
            ctx,
            candidate_groups,
            all_active_by_key,
            execute=args.execute,
            logger=logger,
        )
        stats.converted_groups = converted
        stats.unchanged_groups = unchanged
        stats.orphan_candidate_keys = orphan_candidates

        if not confirm("Continue with per-file RAG rebuild?"):
            logger.info("Operator declined before RAG rebuild")
            return 0

        # Stage 4: RAG rebuild.
        rebuilt, skipped, failed = await rebuild_rag(
            ctx,
            candidates,
            execute=args.execute,
            logger=logger,
        )
        stats.rebuilt_file_ids = rebuilt
        stats.skipped_file_ids = skipped
        stats.failed_file_ids = failed

        return 0
    except Exception as exc:
        logger.error("Script failed: %s", exc)
        logger.error("%s", traceback.format_exc())
        return 1
    finally:
        # Stage 5: final report.
        elapsed = time.monotonic() - started
        log_final_report(logger, stats, log_path, elapsed)
        await close_services(ctx, logger)


def main() -> int:
    """Start the async script runner and return its exit code."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
