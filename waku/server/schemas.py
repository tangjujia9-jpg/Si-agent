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
