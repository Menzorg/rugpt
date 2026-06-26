"""
Shared domain constants.

Non-env constants used across multiple modules.
"""

# File types

# Image types — supported as chat attachments; NOT supported by RAG indexer.
IMAGE_TYPES: frozenset[str] = frozenset({"jpg", "jpeg", "png", "gif", "webp"})

# Document types — supported by RAG indexer.
RAG_COMPATIBLE_TYPES: frozenset[str] = frozenset({
    "pdf", "docx", "doc", "wps", "odt",           # documents
    "xlsx", "xls", "ods", "tsv", "csv", "xlsm",   # tables
    "txt", "json", "html", "log", "rtf", "xml",   # other
})

# All types accepted by the upload pipeline.
ALLOWED_FILE_TYPES: frozenset[str] = RAG_COMPATIBLE_TYPES | IMAGE_TYPES

# Extensions that represent tabular data — is_table flag set at upload time
TABLE_EXTENSIONS: frozenset[str] = frozenset({"csv", "tsv", "xlsx", "xls", "ods", "xlsm"})

CONTENT_TYPES = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "csv":  "text/csv",
    "tsv":  "text/tab-separated-values",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
    "ods":  "application/vnd.oasis.opendocument.spreadsheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "gif":  "image/gif",
    "webp": "image/webp",
}

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
