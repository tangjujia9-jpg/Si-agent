"""Incremental per-thread extraction. Only user statements can ground new facts."""

from datetime import timedelta

from sqlalchemy import select

from waku.memory.candidates import extract_decisions
from waku.memory.indexing import enqueue_index, refresh_indexes
from waku.memory.lifecycle import blocked, digest, materialize_evidence
from waku.memory.postgres import root_uri
from waku.memory.service import prepare_proposals
from waku.storage.models import ContextNode, Job, Message, Project, Thread, now


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
            prepared.append(
                (mid, seq, content, prepare_proposals(content, extractor, decisions, heartbeat))
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
        count, sources = 0, 0
        root = root_uri(user_id, project_id)
        for mid, seq, content, proposals in prepared:
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
            # Source nodes are immutable snapshots with back-references to messages.
            # They stay out of default fact/resource searches.
            for path, title in (
                (root, project.name),
                (root + "/sessions", "sessions"),
                (root + "/sessions/" + tid, "Conversation"),
            ):
                if not session.scalar(
                    select(ContextNode.id).where(
                        ContextNode.project_id == project_id, ContextNode.uri == path
                    )
                ):
                    session.add(
                        ContextNode(
                            project_id=project_id,
                            uri=path,
                            title=title,
                            parent_uri=path.rsplit("/", 1)[0] if path != root else None,
                            kind="directory",
                        )
                    )
                    session.flush()
            source = session.scalar(
                select(ContextNode).where(
                    ContextNode.project_id == project_id, ContextNode.uri == uri
                )
            )
            if source is None:
                source = ContextNode(
                    project_id=project_id,
                    uri=uri,
                    parent_uri=uri.rsplit("/", 1)[0],
                    kind="session",
                    title="User statement",
                    content=content,
                    checksum=digest(content),
                    abstract=content[:240],
                    overview=content[:1200],
                    source_event_ids=[mid],
                )
                session.add(source)
                session.flush()
            if source.status != "active":
                continue
            materialize_evidence(session, source)
            count += extract_decisions(session, source, proposals)
            sources += 1
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
        }
        job.error, job.lease_token, job.lease_until = None, None, None
    return True
