"""Provider-neutral contracts inspired by pi's model collection design."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str
    kind: Literal["chat", "embedding", "reranker"] = "chat"


@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool = False
    streaming: bool = False
    structured_output: bool = False
    vision: bool = False
    reasoning: bool = False
    embeddings: bool = False
    context_window: int | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: Sequence[dict[str, Any]]
    system: str = ""
    tools: Sequence[dict[str, Any]] = ()
    max_tokens: int = 2048
    temperature: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: Sequence[dict[str, Any]] = ()
    stop_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    raw: Any = None


@dataclass(frozen=True)
class StreamEvent:
    kind: Literal["start", "text_delta", "tool_call", "usage", "done", "error"]
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ProviderPort(Protocol):
    """Minimal interface required by the Waku runtime."""

    def complete(self, model: ModelRef, request: ModelRequest) -> ModelResponse:
        ...

    def stream(self, model: ModelRef, request: ModelRequest) -> Iterator[StreamEvent]:
        ...

    def capabilities(self, model: ModelRef) -> ModelCapabilities:
        ...

    def list_models(self) -> Sequence[ModelRef]:
        ...
