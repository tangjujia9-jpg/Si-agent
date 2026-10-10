"""Version/evidence lifecycle. Callers serialize changes with the project row lock."""

import hashlib
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import delete, select

from waku.storage.models import (
    ContextIndex,
    ContextNode,
    ForgetTombstone,
    Job,
    MemoryCandidate,
    MemoryEvidence,
    MemoryVersion,
)


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    text: str


def chunks(content: str, size: int = 2400, overlap: int = 200) -> list[Chunk]:
    """Bounded character windows, preferring line boundaries and preserving source offsets."""
    if size <= overlap or overlap < 0:
        raise ValueError("chunk size must exceed nonnegative overlap")
    result, start = [], 0
    while start < len(content):
        end = min(start + size, len(content))
        if end < len(content):
            boundary = content.rfind("\n", start + size // 2, end)
            if boundary >= 0:
                end = boundary + 1
        result.append(Chunk(start, end, content[start:end]))
        if end == len(content):
            break
        start = max(start + 1, end - overlap)
    return result


def record_version(session, node):
    session.flush()
    existing = session.scalar(
        select(MemoryVersion).where(
            MemoryVersion.node_id == node.id, MemoryVersion.revision == node.revision
        )
    )
    if existing:
        return existing
    snapshot = {
        key: getattr(node, key)
        for key in (
            "uri",
            "kind",
            "title",
            "abstract",
            "overview",
            "content",
            "checksum",
            "status",
            "source_event_ids",
            "confidence",
            "supersedes_id",
        )
    }
    for key in ("valid_from", "valid_to"):
        value = getattr(node, key)
        snapshot[key] = value.isoformat() if value else None
    version = MemoryVersion(node_id=node.id, revision=node.revision, snapshot=snapshot)
    session.add(version)
    session.flush()
    return version


def materialize_evidence(session, node):
    """Each quote is an exact slice of an immutable source revision."""
    if node.kind == "directory" or node.status != "active":
        return
    record_version(session, node)
    existing = {
        (e.start_char, e.end_char)
        for e in session.scalars(
            select(MemoryEvidence).where(
                MemoryEvidence.node_id == node.id, MemoryEvidence.revision == node.revision
            )
        )
    }
    for chunk in chunks(node.content):
        if (chunk.start, chunk.end) not in existing:
            session.add(
                MemoryEvidence(
                    node_id=node.id,
                    revision=node.revision,
                    start_char=chunk.start,
                    end_char=chunk.end,
                    quote=chunk.text,
                    checksum=digest(chunk.text),
                    source_event_ids=list(node.source_event_ids),
                )
            )


def blocked(session, project_id, uri, content, evidence_ids=()):
    checksum = digest(content)
    for tombstone in session.scalars(
        select(ForgetTombstone).where(ForgetTombstone.project_id == project_id)
    ):
        if tombstone.uri == uri or (content and checksum in tombstone.content_hashes):
            return True
    for evidence_id in evidence_ids:
        evidence = session.get(MemoryEvidence, evidence_id)
        if evidence:
            source = session.get(ContextNode, evidence.node_id)
            if evidence.redacted or source is None or source.status == "retracted":
                return True
    return False


def forget_node(session, node, reason):
    """Durable logical deletion, with redaction of versions and derived memories."""
    if node.kind == "directory":
        raise ValueError("forget accepts leaf nodes only")
    pending = [node]
    visited = set()
    while pending:
        current = pending.pop()
        if current.id in visited:
            continue
        visited.add(current.id)
        record_version(session, current)
        versions = list(
            session.scalars(select(MemoryVersion).where(MemoryVersion.node_id == current.id))
        )
        evidence = list(
            session.scalars(select(MemoryEvidence).where(MemoryEvidence.node_id == current.id))
        )
        evidence_ids = {e.id for e in evidence}
        hashes = {digest(v.snapshot["content"]) for v in versions if v.snapshot.get("content")}
        hashes.update(e.checksum for e in evidence)
        # Redact retained import payloads too, including imports predating
        # version tracking. Retain their hash/idempotency metadata for replay guards.
        for job in session.scalars(
            select(Job).where(Job.project_id == current.project_id, Job.kind == "ingest")
        ):
            docs = []
            changed = False
            for document in job.payload.get("documents", []):
                encoded_path = "/".join(quote(p, safe="") for p in document["path"].split("/"))
                matches_uri = current.uri.endswith("/resources/" + encoded_path)
                if matches_uri or digest(document["content"]) in hashes:
                    hashes.add(digest(document["content"]))
                    docs.append({**document, "content": ""})
                    changed = True
                else:
                    docs.append(document)
            if changed:
                job.payload = {**job.payload, "documents": docs}
        tombstone = session.scalar(
            select(ForgetTombstone).where(
                ForgetTombstone.project_id == current.project_id, ForgetTombstone.uri == current.uri
            )
        )
        if not tombstone:
            session.add(
                ForgetTombstone(
                    project_id=current.project_id,
                    uri=current.uri,
                    content_hashes=sorted(hashes),
                    reason=reason[:240],
                )
            )
        else:
            tombstone.content_hashes = sorted(set(tombstone.content_hashes) | hashes)
        for candidate in session.scalars(
            select(MemoryCandidate).where(MemoryCandidate.project_id == current.project_id)
        ):
            if candidate.uri == current.uri or evidence_ids.intersection(candidate.evidence_ids):
                if candidate.node_id and candidate.node_id != current.id:
                    pending.append(session.get(ContextNode, candidate.node_id))
                candidate.content = ""
                candidate.status, candidate.reason = "skipped", "forgotten_source"
        for derived in session.scalars(
            select(ContextNode).where(
                ContextNode.project_id == current.project_id, ContextNode.kind != "directory"
            )
        ):
            if evidence_ids.intersection(derived.source_event_ids):
                pending.append(derived)
        for version in versions:
            version.snapshot = {
                **version.snapshot,
                "content": "",
                "abstract": "",
                "overview": "",
                "status": "retracted",
                "redacted": True,
            }
        for item in evidence:
            item.quote, item.redacted = "", True
        session.execute(delete(ContextIndex).where(ContextIndex.node_id == current.id))
        current.content = current.abstract = current.overview = ""
        current.embedding, current.search_vector = None, None
        current.status = "retracted"
        current.revision += 1
        record_version(session, current)
    return visited
