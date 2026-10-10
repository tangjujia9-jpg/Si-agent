"""Evidence-backed candidates with explicit actions and optimistic revision checks."""

import json
import re
from urllib.parse import quote

from sqlalchemy import select

from waku.domain.contracts import MemoryNode
from waku.memory.lifecycle import blocked, digest, record_version
from waku.memory.port import MemoryWrite
from waku.memory.postgres import PostgresMemory, root_uri, validate_uri
from waku.storage.models import ContextNode, Job, MemoryCandidate, MemoryEvidence, Project


def submit_candidate(session, project, payload, *, queue=True):
    """Serialize idempotency checks and candidate/job creation on the project lock."""
    session.execute(select(Project).where(Project.id == project.id).with_for_update())
    parent = validate_uri(payload["uri"], project.user_id, project.id)
    if parent is None:
        raise ValueError("candidate must target a leaf URI")
    checksum = digest(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    old = session.scalar(
        select(MemoryCandidate).where(
            MemoryCandidate.project_id == project.id,
            MemoryCandidate.idempotency_key == payload["idempotency_key"],
        )
    )
    if old:
        if old.payload_hash != checksum:
            raise ValueError("idempotency key conflicts with different candidate")
        return old
    for evidence_id in payload["evidence_ids"]:
        evidence = session.get(MemoryEvidence, evidence_id)
        source = session.get(ContextNode, evidence.node_id) if evidence else None
        if not source or source.project_id != project.id or evidence.redacted:
            raise ValueError("evidence must belong to this project and not be redacted")
    candidate = MemoryCandidate(project_id=project.id, payload_hash=checksum, **payload)
    session.add(candidate)
    session.flush()
    if queue:
        queue_candidate(session, candidate)
    return candidate


def queue_candidate(session, candidate):
    if candidate.job_id or candidate.status != "pending":
        return
    job = Job(
        project_id=candidate.project_id,
        kind="consolidate",
        idempotency_key="candidate:" + candidate.id,
        payload_hash=candidate.payload_hash,
        payload={"candidate_id": candidate.id},
    )
    session.add(job)
    session.flush()
    candidate.job_id = job.id


def apply_candidate(session, job):
    session.execute(select(Project).where(Project.id == job.project_id).with_for_update())
    candidate = session.get(MemoryCandidate, job.payload["candidate_id"])
    if not candidate or candidate.project_id != job.project_id:
        raise ValueError("candidate missing in project")
    if candidate.status != "pending":
        return {
            "candidate_id": candidate.id,
            "status": candidate.status,
            "reason": candidate.reason,
        }

    def finish(status, reason):
        candidate.status, candidate.reason = status, reason
        return {
            "candidate_id": candidate.id,
            "status": status,
            "reason": reason,
            "node_id": candidate.node_id,
        }

    if candidate.action == "SKIP":
        return finish("skipped", "explicit_skip")
    if blocked(session, job.project_id, candidate.uri, candidate.content, candidate.evidence_ids):
        return finish("skipped", "forget_tombstone")
    for evidence_id in candidate.evidence_ids:
        evidence = session.get(MemoryEvidence, evidence_id)
        source = session.get(ContextNode, evidence.node_id) if evidence else None
        if (
            not source
            or source.project_id != job.project_id
            or source.status != "active"
            or source.revision != evidence.revision
        ):
            return finish("conflict", "stale_or_invalid_evidence")
    if not candidate.evidence_ids:
        return finish("conflict", "evidence_required")
    current = session.scalar(
        select(ContextNode).where(
            ContextNode.project_id == job.project_id, ContextNode.uri == candidate.uri
        )
    )
    if current and current.kind == "directory":
        return finish("conflict", "directory_collision")
    if candidate.action == "ADD" and current:
        if current.status == "active" and current.content == candidate.content:
            candidate.node_id = current.id
            return finish("skipped", "duplicate_content")
        return finish("conflict", "target_exists")
    if candidate.action in ("UPDATE", "RETRACT") and (
        not current or current.status != "active" or current.revision != candidate.expected_revision
    ):
        return finish("conflict", "revision_mismatch")
    if candidate.action == "SUPERSEDES":
        old = session.get(ContextNode, candidate.supersedes_id) if candidate.supersedes_id else None
        if (
            not old
            or old.project_id != job.project_id
            or old.status != "active"
            or old.revision != candidate.expected_revision
        ):
            return finish("conflict", "superseded_revision_mismatch")
        if current:
            return finish("conflict", "target_exists")
    if candidate.action == "RETRACT":
        record_version(session, current)
        current.status, current.revision = "retracted", current.revision + 1
        record_version(session, current)
        from waku.memory.indexing import enqueue_index, refresh_indexes

        refresh_indexes(session, job.project_id)
        enqueue_index(session, job.project_id)
        candidate.node_id = current.id
    else:
        project = session.get(Project, job.project_id)
        receipt = PostgresMemory(None).write_in_session(
            MemoryWrite(
                MemoryNode(
                    candidate.uri,
                    candidate.kind,
                    candidate.title,
                    content=candidate.content,
                    project_id=job.project_id,
                    user_id=project.user_id,
                    supersedes_id=candidate.supersedes_id
                    if candidate.action == "SUPERSEDES"
                    else None,
                ),
                "candidate:" + candidate.id,
                evidence_ids=tuple(candidate.evidence_ids),
            ),
            session,
        )
        candidate.node_id = receipt.id
    return finish("applied", candidate.action.lower())


def extract_decisions(session, node, proposals=None):
    """Opt-in source notation: Decision[key]: text / 决定[key]: text; never auto-apply."""
    project = session.get(Project, node.project_id)
    count = 0
    if proposals is None:
        proposals = [
            {
                "key": m.group(1).strip(),
                "content": m.group(2).strip(),
                "quote": m.group(),
                "kind": "episodic",
            }
            for m in re.finditer(
                r"(?m)^(?:Decision|决定)\[([^\]\n]{1,80})\]:\s*([^\n]+)", node.content
            )
        ]
    for proposal in proposals:
        key, content = proposal["key"], proposal["content"]
        if not re.fullmatch(r"[\w -]+", key) or not content:
            continue
        start = node.content.find(proposal["quote"])
        if start < 0 or not proposal["quote"]:
            raise ValueError("candidate quote must be exact source text")
        end = start + len(proposal["quote"])
        evidence = session.scalar(
            select(MemoryEvidence).where(
                MemoryEvidence.node_id == node.id,
                MemoryEvidence.revision == node.revision,
                MemoryEvidence.start_char == start,
                MemoryEvidence.end_char == end,
            )
        )
        if not evidence:
            evidence = MemoryEvidence(
                node_id=node.id,
                revision=node.revision,
                start_char=start,
                end_char=end,
                quote=proposal["quote"],
                checksum=digest(proposal["quote"]),
                source_event_ids=list(node.source_event_ids),
            )
            session.add(evidence)
            session.flush()
        submit_candidate(
            session,
            project,
            {
                "uri": root_uri(project.user_id, project.id)
                + "/decisions/"
                + quote(key, safe="")
                + ".md",
                "kind": proposal["kind"],
                "title": key,
                "content": content,
                "action": "ADD",
                "expected_revision": None,
                "supersedes_id": None,
                "evidence_ids": [evidence.id],
                "idempotency_key": f"extract:{node.id}:{node.revision}:{start}:{digest(key)[:12]}",
            },
            queue=False,
        )
        count += 1
        if count == 100:
            break
    return count
