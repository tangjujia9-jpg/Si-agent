"""Copilot schema. SQLite variants support offline tests, not vector retrieval."""

from datetime import UTC, datetime
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Project(Identity, Base):
    __tablename__ = "projects"
    user_id: Mapped[str] = mapped_column(String(80), default="default", index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")


class Thread(Identity, Base):
    __tablename__ = "threads"
    __table_args__ = (UniqueConstraint("id", "project_id"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))


class Message(Identity, Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(["thread_id", "project_id"], ["threads.id", "threads.project_id"]),
        CheckConstraint("role IN ('user', 'assistant', 'tool')", name="message_role"),
    )
    thread_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    event_data: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )


class Run(Identity, Base):
    __tablename__ = "runs"
    __table_args__ = (
        ForeignKeyConstraint(["thread_id", "project_id"], ["threads.id", "threads.project_id"]),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="run_status",
        ),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    model: Mapped[str] = mapped_column(String(160), default="")
    trace_id: Mapped[str | None] = mapped_column(String(36))
    context: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), default=dict)


class ContextNode(Identity, Base):
    __tablename__ = "context_nodes"
    __table_args__ = (
        UniqueConstraint("project_id", "uri", name="uq_node_project_uri"),
        CheckConstraint("status IN ('active', 'superseded', 'retracted')", name="node_status"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="node_confidence"
        ),
        Index("ix_nodes_project_parent", "project_id", "parent_uri"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    uri: Mapped[str] = mapped_column(Text)
    parent_uri: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(240))
    abstract: Mapped[str] = mapped_column(Text, default="")
    overview: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    checksum: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active")
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("context_nodes.id"))
    embedding: Mapped[list | None] = mapped_column(
        Vector(1536).with_variant(JSON(none_as_null=True), "sqlite")
    )
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR().with_variant(Text, "sqlite"))


class Job(Identity, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_job_project_key"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="job_status"
        ),
        Index("ix_jobs_claim", "status", "available_at"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), default=dict)


class ContextIndex(Identity, Base):
    """One searchable representation per node/tier; revisions fence stale indexes."""

    __tablename__ = "context_indexes"
    __table_args__ = (
        UniqueConstraint("node_id", "tier", "position", name="uq_index_node_tier_position"),
        CheckConstraint("tier IN ('l0', 'l1', 'l2')", name="index_tier"),
        Index("ix_context_indexes_search", "search_vector", postgresql_using="gin"),
    )
    node_id: Mapped[str] = mapped_column(ForeignKey("context_nodes.id", ondelete="CASCADE"))
    tier: Mapped[str] = mapped_column(String(2))
    revision: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)
    start_char: Mapped[int] = mapped_column(Integer, default=0)
    end_char: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(240))
    embedding: Mapped[list | None] = mapped_column(
        Vector(1536).with_variant(JSON(none_as_null=True), "sqlite")
    )
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR().with_variant(Text, "sqlite"))


class MemoryWriteRecord(Identity, Base):
    __tablename__ = "memory_writes"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_memory_write_key"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    node_id: Mapped[str] = mapped_column(ForeignKey("context_nodes.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    created: Mapped[bool]


class MemoryVersion(Identity, Base):
    __tablename__ = "memory_versions"
    __table_args__ = (UniqueConstraint("node_id", "revision", name="uq_version_node_revision"),)
    node_id: Mapped[str] = mapped_column(ForeignKey("context_nodes.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))


class MemoryEvidence(Identity, Base):
    """Exact original-document character spans, pinned to a source revision."""

    __tablename__ = "memory_evidence"
    __table_args__ = (
        UniqueConstraint("node_id", "revision", "start_char", "end_char", name="uq_evidence_span"),
        CheckConstraint("start_char >= 0 AND end_char >= start_char", name="evidence_range"),
    )
    node_id: Mapped[str] = mapped_column(ForeignKey("context_nodes.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    redacted: Mapped[bool] = mapped_column(default=False)


class ForgetTombstone(Identity, Base):
    __tablename__ = "forget_tombstones"
    __table_args__ = (UniqueConstraint("project_id", "uri", name="uq_tombstone_uri"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    uri: Mapped[str] = mapped_column(Text)
    content_hashes: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(240))


class MemoryCandidate(Identity, Base):
    __tablename__ = "memory_candidates"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_candidate_key"),
        CheckConstraint(
            "action IN ('ADD','UPDATE','SUPERSEDES','RETRACT','SKIP')", name="candidate_action"
        ),
        CheckConstraint(
            "status IN ('pending','applied','conflict','skipped')", name="candidate_status"
        ),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    uri: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(240))
    content: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    expected_revision: Mapped[int | None] = mapped_column(Integer)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("context_nodes.id"))
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(240))
    node_id: Mapped[str | None] = mapped_column(ForeignKey("context_nodes.id"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
