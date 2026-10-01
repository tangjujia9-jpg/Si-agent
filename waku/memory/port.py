"""The product-level memory port.

The existing ``FactStore`` remains the compatibility contract for the local
assistant.  ``MemoryPort`` is the richer contract for the Project Copilot: it
returns inspectable evidence and accepts scoped, versioned nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable

from waku.domain.contracts import MemoryHit, MemoryNode


@dataclass(frozen=True)
class MemoryQuery:
    text: str
    user_id: str = "default"
    project_id: str | None = None
    top_k: int = 8
    tiers: tuple[str, ...] = ("l0", "l1", "l2")
    include_details: bool = True
    max_tokens: int = 3000
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("memory query text cannot be empty")
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")


@dataclass(frozen=True)
class MemoryWrite:
    node: MemoryNode
    idempotency_key: str
    evidence_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryWriteReceipt:
    id: str
    uri: str
    created: bool
    status: str = "accepted"


@runtime_checkable
class MemoryPort(Protocol):
    """Read/write boundary implemented by SQLite, Postgres, and adapters."""

    def search(self, query: MemoryQuery) -> Sequence[MemoryHit]:
        ...

    def write(self, request: MemoryWrite) -> MemoryWriteReceipt:
        ...

    def get(self, uri: str, *, user_id: str = "default") -> MemoryNode | None:
        ...

    def list_children(self, uri: str, *, user_id: str = "default") -> Sequence[MemoryNode]:
        ...

    def forget(self, uri: str, *, user_id: str = "default", reason: str = "user_request") -> bool:
        ...
