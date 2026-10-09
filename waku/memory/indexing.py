"""Tier materialization and fenced asynchronous vector indexing."""

import hashlib
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, literal_column, select

from waku.memory.embeddings import Embedder, terms, validate_vectors
from waku.storage.models import ContextIndex, ContextNode, Job, now


def refresh_indexes(session, project_id: str) -> None:
    """Update previews/lexical indexes in the same transaction as document writes."""
    session.flush()
    nodes = list(session.scalars(select(ContextNode).where(ContextNode.project_id == project_id)))
    active = [n for n in nodes if n.status == "active"]
    for directory in sorted(
        (n for n in active if n.kind == "directory"), key=lambda n: -len(n.uri)
    ):
        children = sorted((n for n in active if n.parent_uri == directory.uri), key=lambda n: n.uri)
        abstract = f"{directory.title}: " + "; ".join(n.abstract or n.title for n in children)
        overview = "\n".join(f"{n.title}: {n.overview or n.abstract}" for n in children)
        abstract, overview = abstract[:600], overview[:6000]
        if (directory.abstract, directory.overview) != (abstract, overview):
            directory.abstract, directory.overview = abstract, overview
            directory.revision += 1
    existing = {
        (row.node_id, row.tier): row
        for row in session.scalars(
            select(ContextIndex).join(ContextNode).where(ContextNode.project_id == project_id)
        )
    }
    for node in active:
        bodies = {"l0": node.abstract, "l1": node.overview}
        if node.kind != "directory":
            bodies["l2"] = node.content
        for tier, body in bodies.items():
            row = existing.get((node.id, tier))
            if row is None:
                row = ContextIndex(node_id=node.id, tier=tier, revision=node.revision, body=body)
                session.add(row)
            elif row.revision == node.revision and row.body == body:
                continue
            row.revision, row.body = node.revision, body
            row.embedding, row.embedding_model = None, None
            tokens = " ".join(terms(f"{node.title} {body}"))
            row.search_vector = (
                func.to_tsvector(literal_column("'simple'"), tokens)
                if session.bind.dialect.name == "postgresql"
                else tokens
            )
    session.flush()


def enqueue_index(session, project_id: str) -> Job:
    job = Job(
        project_id=project_id,
        kind="index",
        payload={},
        payload_hash=hashlib.sha256(b"index").hexdigest(),
        idempotency_key="index:" + uuid4().hex,
    )
    session.add(job)
    session.flush()
    return job


def process_index(sessions, job_id: str, token: str, embedder: Embedder | None) -> bool:
    """External requests run outside transactions. Revisions and leases fence commits."""
    with sessions() as session:
        job = session.scalar(select(Job).where(Job.id == job_id, Job.lease_token == token))
        if job is None or job.status != "running":
            return False
        rows = session.execute(
            select(ContextIndex, ContextNode.title)
            .join(ContextNode)
            .where(
                ContextNode.project_id == job.project_id,
                ContextNode.status == "active",
                ContextIndex.revision == ContextNode.revision,
            )
        ).all()
        snapshot = [
            (row.id, row.revision, row.body, f"{title}\n{row.body}"[:6000])
            for row, title in rows
            if embedder and row.embedding_model != embedder.model_id
        ]
    vectors = []
    for offset in range(0, len(snapshot), 16):
        # Renew between bounded network batches; a stolen lease stops further
        # requests. The final commit still verifies the token and revisions.
        with sessions.begin() as session:
            owned = session.scalar(
                select(Job)
                .where(
                    Job.id == job_id,
                    Job.status == "running",
                    Job.lease_token == token,
                )
                .with_for_update()
            )
            if owned is None:
                return False
            owned.lease_until = now() + timedelta(seconds=60)
        batch = snapshot[offset : offset + 16]
        vectors.extend(validate_vectors(embedder.embed([r[3] for r in batch]), len(batch)))
    with sessions.begin() as session:
        job = session.scalar(
            select(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.lease_token == token,
            )
            .with_for_update()
        )
        if job is None:
            return False
        applied = 0
        for (row_id, revision, body, _), vector in zip(snapshot, vectors, strict=True):
            row = session.scalar(
                select(ContextIndex).where(ContextIndex.id == row_id).with_for_update()
            )
            if row and row.revision == revision and row.body == body:
                row.embedding, row.embedding_model = vector, embedder.model_id
                applied += 1
        job.result = {
            "indexed": applied,
            "stale_skipped": len(snapshot) - applied,
            "embedding_model": embedder.model_id if embedder else None,
        }
        job.status, job.error, job.lease_token, job.lease_until = "succeeded", None, None, None
    return True
