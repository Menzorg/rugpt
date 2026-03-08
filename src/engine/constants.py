"""
Shared domain constants.

Non-env constants used across multiple modules.
"""

# File types
ALLOWED_FILE_TYPES = {"pdf", "docx"}

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
