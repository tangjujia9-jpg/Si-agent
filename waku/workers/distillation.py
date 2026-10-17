"""Incremental per-thread extraction. Only user statements can ground new facts."""

from datetime import timedelta

from sqlalchemy import or_, select

from waku.memory.candidates import extract_decisions
from waku.memory.indexing import enqueue_index, refresh_indexes
from waku.memory.lifecycle import blocked, digest, materialize_evidence
from waku.memory.postgres import root_uri
from waku.memory.service import prepare_proposals
from waku.storage.models import ContextNode, Job, MemoryEvidence, Message, Project, Thread, now


def statement_source(session, project, thread_id, message):
    """Materialize an immutable user statement for both live tools and distillation."""
    root = root_uri(project.user_id, project.id)
    uri = f"{root}/sessions/{thread_id}/{message.id}.md"
    if message.event_data.get("redacted") or blocked(session, project.id, uri, message.content):
        raise ValueError("statement forgotten")
    for path, title in (
        (root, project.name),
        (root + "/sessions", "sessions"),
        (root + "/sessions/" + thread_id, "Conversation"),
    ):
        if not session.scalar(
            select(ContextNode.id).where(
                ContextNode.project_id == project.id, ContextNode.uri == path
            )
        ):
            session.add(
                ContextNode(
                    project_id=project.id,
                    uri=path,
                    title=title,
                    parent_uri=path.rsplit("/", 1)[0] if path != root else None,
                    kind="directory",
                )
            )
            session.flush()
    source = session.scalar(
        select(ContextNode).where(ContextNode.project_id == project.id, ContextNode.uri == uri)
    )
    if source is None:
        source = ContextNode(
            project_id=project.id,
            uri=uri,
            parent_uri=uri.rsplit("/", 1)[0],
            kind="session",
            title="User statement",
            content=message.content,
            checksum=digest(message.content),
            abstract=message.content[:240],
            overview=message.content[:1200],
            source_event_ids=[message.id],
        )
        session.add(source)
        session.flush()
    if source.status != "active" or source.content != message.content:
        raise ValueError("statement snapshot changed")
    materialize_evidence(session, source)
    session.flush()
    return source


def append_message(session, thread, *, role, content, event_data):
    """Caller holds the thread or project lock; sequence is local to one thread."""
    thread.event_sequence += 1
    message = Message(
        project_id=thread.project_id,
        thread_id=thread.id,
        role=role,
        content=content,
        sequence=thread.event_sequence,
        event_data=event_data,
    )
    session.add(message)
    session.flush()
    return message


def enqueue_distillation(session, thread, *, delay_seconds=30):
    """A delayed durable job also processes low-traffic conversations without N turns."""
    key = f"distill:{thread.id}:{thread.event_sequence}"
    old = session.scalar(
        select(Job).where(Job.project_id == thread.project_id, Job.idempotency_key == key)
    )
    if old:
        if delay_seconds == 0 and old.status == "queued":
            old.available_at = now()
        return old
    job = Job(
        project_id=thread.project_id,
        kind="distill",
        idempotency_key=key,
        payload_hash=digest(key),
        payload={"thread_id": thread.id, "through": thread.event_sequence},
        available_at=now() + timedelta(seconds=delay_seconds),
    )
    session.add(job)
    session.flush()
    return job


def process_distillation(sessions, job_id, token, extractor=None, decisions=None):
    with sessions() as session:
        job = session.get(Job, job_id)
        if not job or job.status != "running" or job.lease_token != token:
            return False
        project = session.get(Project, job.project_id)
        thread = session.get(Thread, job.payload["thread_id"])
        if not project or not thread or thread.project_id != project.id:
            raise ValueError("invalid distillation scope")
        project_id, user_id, tid = project.id, project.user_id, thread.id
        through = job.payload["through"]
        # Bound one batch. A continuation job handles the remaining interval.
        rows = session.scalars(
            select(Message)
            .where(
                Message.thread_id == tid,
                Message.project_id == project_id,
                Message.sequence > thread.memory_cursor,
                Message.sequence <= through,
            )
            .order_by(Message.sequence)
            .limit(100)
        ).all()
        snapshots = [(m.id, m.sequence, m.role, m.content, m.event_data) for m in rows]
        contextual = extractor is not None and hasattr(extractor, "extract_context")
        recent = (
            list(
                session.scalars(
                    select(Message)
                    .where(
                        Message.thread_id == tid,
                        Message.project_id == project_id,
                        Message.role == "user",
                        Message.sequence <= (snapshots[-1][1] if snapshots else through),
                    )
                    .order_by(Message.sequence.desc())
                    .limit(106)
                )
            )
            if contextual
            else []
        )
        recent = [(m.id, m.sequence, m.content) for m in recent if not m.event_data.get("redacted")]
        memories = (
            list(
                session.scalars(
                    select(ContextNode)
                    .where(
                        ContextNode.project_id == project_id,
                        ContextNode.status == "active",
                        ContextNode.needs_review.is_(False),
                        ContextNode.kind.in_(("semantic", "episodic", "procedural")),
                        or_(ContextNode.valid_from.is_(None), ContextNode.valid_from <= now()),
                        or_(ContextNode.valid_to.is_(None), ContextNode.valid_to > now()),
                    )
                    .order_by(ContextNode.created_at.desc())
                    .limit(8)
                )
            )
            if contextual
            else []
        )
        memory_context = [
            {
                "id": n.id,
                "uri": n.uri,
                "key": n.title,
                "kind": n.kind,
                "revision": n.revision,
                "content": n.content[:1000],
            }
            for n in memories
        ]

    def heartbeat():
        with sessions.begin() as session:
            owned = session.scalar(
                select(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                .with_for_update()
            )
            if not owned:
                raise RuntimeError("distillation lease lost")
            owned.lease_until = now() + timedelta(seconds=60)

    prepared = []
    for mid, seq, role, content, meta in snapshots:
        if role == "user" and not meta.get("redacted"):
            history = sorted([m for m in recent if m[1] < seq], key=lambda m: m[1])[-4:]
            context = (
                {
                    "recent_user_statements": [{"id": m[0], "text": m[2][:1500]} for m in history],
                    "memories": memory_context,
                }
                if contextual
                else None
            )
            prepared.append(
                (
                    mid,
                    seq,
                    content,
                    history,
                    prepare_proposals(content, extractor, decisions, heartbeat, context),
                )
            )
    with sessions.begin() as session:
        project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
        job = session.scalar(
            select(Job)
            .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
            .with_for_update()
        )
        if not job:
            return False
        if not project or project.user_id != user_id:
            raise ValueError("distillation owner changed")
        thread = session.scalar(select(Thread).where(Thread.id == tid).with_for_update())
        count, sources, source_ids = 0, 0, []
        root = root_uri(user_id, project_id)
        for mid, seq, content, history, proposals in prepared:
            if seq <= thread.memory_cursor:
                continue
            message = session.get(Message, mid)
            uri = f"{root}/sessions/{tid}/{mid}.md"
            if (
                not message
                or message.content != content
                or message.event_data.get("redacted")
                or blocked(session, project_id, uri, content)
            ):
                continue
            context_evidence = []
            # Background-assisted claims retain user/accepted-memory dependencies.
            # A concurrent edit/forget fences the whole extraction, never its evidence alone.
            for previous_id, _, previous_text in history:
                previous = session.get(Message, previous_id)
                if (
                    not previous
                    or previous.content != previous_text
                    or previous.event_data.get("redacted")
                ):
                    raise ValueError("extraction context changed")
                previous_source = statement_source(session, project, tid, previous)
                context_evidence.extend(
                    session.scalars(
                        select(MemoryEvidence.id).where(
                            MemoryEvidence.node_id == previous_source.id,
                            MemoryEvidence.redacted.is_(False),
                        )
                    )
                )
            for ref in memory_context:
                current = session.get(ContextNode, ref["id"])
                if (
                    not current
                    or current.revision != ref["revision"]
                    or current.status != "active"
                    or current.needs_review
                ):
                    raise ValueError("extraction context changed")
                if not session.scalar(
                    select(ContextNode.id).where(
                        ContextNode.id == current.id,
                        or_(ContextNode.valid_from.is_(None), ContextNode.valid_from <= now()),
                        or_(ContextNode.valid_to.is_(None), ContextNode.valid_to > now()),
                    )
                ):
                    raise ValueError("extraction context expired")
                # Do not make a correction depend on the obsolete fact it replaces.
                for proposal in proposals:
                    if proposal.get("target_uri") == ref["uri"] or (
                        proposal["key"] == ref["key"] and proposal["kind"] == ref["kind"]
                    ):
                        continue
                    proposal.setdefault("context_evidence_ids", []).extend(
                        session.scalars(
                            select(MemoryEvidence.id)
                            .where(
                                MemoryEvidence.node_id == current.id,
                                MemoryEvidence.revision == current.revision,
                                MemoryEvidence.redacted.is_(False),
                            )
                            .limit(4)
                        )
                    )
            for proposal in proposals:
                proposal.setdefault("context_evidence_ids", []).extend(context_evidence)
            source = statement_source(session, project, tid, message)
            count += extract_decisions(session, source, proposals)
            sources += 1
            source_ids.append(source.id)
        if snapshots:
            thread.memory_cursor = max(thread.memory_cursor, snapshots[-1][1])
        if snapshots and snapshots[-1][1] < through:
            # Reuse the same job with a durable next-batch checkpoint.
            job.status = "queued"
            job.available_at = now()
            job.attempts = 0
        else:
            job.status = "succeeded"
        if sources:
            refresh_indexes(session, project_id)
            enqueue_index(session, project_id)
        job.result = {
            "thread_id": tid,
            "through": thread.memory_cursor,
            "candidates": count,
            "sources": sources,
            "source_node_ids": source_ids,
        }
        job.error, job.lease_token, job.lease_until = None, None, None
    return True
