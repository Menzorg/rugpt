"""
Shared domain constants.

Non-env constants used across multiple modules.
"""

# File types
ALLOWED_FILE_TYPES = {"pdf", "docx", "csv", "xlsx", "xls", "ods", "tsv"}

# Extensions that represent tabular data — is_table flag set at upload time
TABLE_EXTENSIONS: frozenset[str] = frozenset({"csv", "tsv", "xlsx", "xls", "ods"})

CONTENT_TYPES = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "csv":  "text/csv",
    "tsv":  "text/tab-separated-values",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
    "ods":  "application/vnd.oasis.opendocument.spreadsheet",
}

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
