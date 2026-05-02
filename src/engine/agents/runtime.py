"""Per-run runtime context shared with agent tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tools.list_documents import ListDocumentsToolRuntime


def _list_documents_toolruntime_factory() -> ListDocumentsToolRuntime:
    from .tools.list_documents import ListDocumentsToolRuntime

    return ListDocumentsToolRuntime()


@dataclass
class RuntimeContext:
    """Mutable scratch state scoped to a single agent execution."""

    list_documents_toolruntime: ListDocumentsToolRuntime = field(
        default_factory=_list_documents_toolruntime_factory,
    )
