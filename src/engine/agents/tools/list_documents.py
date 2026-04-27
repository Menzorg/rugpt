"""STUB for missing list_documents module.

The real list_documents tool lives in document_tool.create_document_tools
and is registered AFTER this stub on engine_service.py — so this module
just needs to expose a callable named `list_documents` for the import to
succeed. Tracked separately as a pre-existing bug.
"""


def list_documents(*args, **kwargs):
    """No-op placeholder; overwritten by document_tool registration."""
    raise NotImplementedError(
        "Stub list_documents — real implementation lives in document_tool"
    )
