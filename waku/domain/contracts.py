"""Stable values that cross Waku subsystem boundaries.

These objects are intentionally plain dataclasses.  They can be serialized in
an event stream, stored in a database, or passed to an adapter without pulling
an SDK into the domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import uuid

OperationStatus = Literal["succeeded", "failed", "partial", "unknown"]
MemoryTier = Literal["l0", "l1", "l2"]


@dataclass(frozen=True)
class Budget:
    """Hard limits for one run.

    Limits belong to the harness rather than the model prompt so a provider
    cannot accidentally remove them by changing its output format.
    """

    max_iterations: int = 10
    max_tokens: int = 8192
    max_tool_calls: int = 20
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls cannot be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class RunContext:
    """Identity, scope, and guardrails for one agent execution."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = "default"
    project_id: str | None = None
    session_id: str = "default"
    model: str = ""
    trace_id: str | None = None
    budget: Budget = field(default_factory=Budget)
    approval_policy: Literal["never", "risky", "always"] = "risky"
    retry_policy: Literal["none", "safe", "aggressive"] = "safe"
    deadline: datetime | None = None

    def scope(self) -> dict[str, str | None]:
        """Return the scope fields used by memory and event storage."""

        return {"user_id": self.user_id, "project_id": self.project_id}


@dataclass(frozen=True)
class ToolResult:
    """Structured result returned by every tool boundary."""

    status: OperationStatus
    message: str
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resource_id: str | None = None
    completed_steps: tuple[str, ...] = ()
    pending_steps: tuple[str, ...] = ()
    retryable: bool = False
    error_code: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("a succeeded result cannot carry an error_code")
        if self.status in {"failed", "unknown"} and not self.message:
            raise ValueError("failed and unknown results need a message")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation for traces and APIs."""

        return {
            "status": self.status,
            "message": self.message,
            "operation_id": self.operation_id,
            "resource_id": self.resource_id,
            "completed_steps": list(self.completed_steps),
            "pending_steps": list(self.pending_steps),
            "retryable": self.retryable,
            "error_code": self.error_code,
            "data": self.data,
        }


@dataclass(frozen=True)
class MemoryNode:
    """A node in the inspectable ``waku://`` context namespace."""

    uri: str
    kind: str
    title: str
    abstract: str = ""
    overview: str = ""
    content: str = ""
    user_id: str = "default"
    project_id: str | None = None
    source_event_ids: tuple[str, ...] = ()
    confidence: float | None = None
    created_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: Literal["active", "superseded", "retracted"] = "active"
    supersedes_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uri.startswith("waku://"):
            raise ValueError("memory node URI must start with waku://")
        if not self.title.strip():
            raise ValueError("memory node title cannot be empty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class MemoryHit:
    """Evidence returned by retrieval, with enough metadata for citations."""

    id: str
    uri: str
    tier: MemoryTier
    snippet: str
    score: float
    source: str
    project_scope: str | None = None
    evidence_ids: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    conflict_group: str | None = None

    def citation(self) -> str:
        return f"[{self.uri}]"
