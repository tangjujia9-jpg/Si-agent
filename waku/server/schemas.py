"""Validated HTTP input/output for the single-user Copilot gateway."""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectInput(Input):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=10000)


class DocumentInput(Input):
    path: str = Field(min_length=1, max_length=240)
    content: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=200000
    )

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("content must contain text without NUL characters")
        return value

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or ":" in value
            or "%" in value
            or ".." in path.parts
            or any(not part for part in value.split("/"))
            or "?" in value
            or "#" in value
            or value == "."
            or any(part == "." for part in value.split("/"))
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("path must be a relative POSIX path without traversal or URI syntax")
        if path.suffix.lower() not in {".md", ".txt", ".py", ".ts", ".tsx", ".js", ".json"}:
            raise ValueError("supported extensions: md, txt, py, ts, tsx, js, json")
        return value


class IngestInput(Input):
    documents: list[DocumentInput] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("documents")
    @classmethod
    def bounded_batch(cls, docs: list[DocumentInput]):
        if len({doc.path for doc in docs}) != len(docs):
            raise ValueError("a batch cannot contain duplicate paths")
        if sum(len(doc.content.encode("utf-8")) for doc in docs) > 2000000:
            raise ValueError("batch must be at most 2 MB")
        return docs


class ThreadInput(Input):
    project_id: str
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectOutput(Output):
    id: str
    name: str
    description: str
    created_at: datetime


class JobOutput(Output):
    id: str
    project_id: str
    kind: str
    status: str
    attempts: int
    error: str | None
    result: dict


class NodeSummary(Output):
    id: str
    uri: str
    parent_uri: str | None
    kind: str
    title: str
    abstract: str
    overview: str
    revision: int
    status: str
    source_event_ids: list[str]


class NodeOutput(NodeSummary):
    content: str


class ThreadOutput(Output):
    id: str
    project_id: str
    title: str
    created_at: datetime


class SearchInput(Input):
    project_id: str = Field(min_length=1, max_length=36)
    q: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=50)
    max_tokens: int = Field(default=3000, ge=1, le=32000)
    include_details: bool = True
    tiers: str = "l0,l1,l2"
    as_of: datetime | None = None

    @field_validator("tiers")
    @classmethod
    def valid_tiers(cls, value: str):
        if not value or any(t not in {"l0", "l1", "l2"} for t in value.split(",")):
            raise ValueError("tiers must be a comma-separated list of l0,l1,l2")
        return value

    @field_validator("as_of")
    @classmethod
    def zoned_time(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        return value


class HitOutput(Output):
    id: str
    uri: str
    tier: str
    snippet: str
    score: float
    source: str
    project_scope: str
    evidence_ids: list[str]
    valid_from: datetime | None
    valid_to: datetime | None
    conflict_group: str | None


class SearchOutput(Output):
    hits: list[HitOutput]
    context: str
    estimated_tokens: int
    truncated: bool
    embedding_model: str | None
    warnings: list[str]
    stages: list[dict]
