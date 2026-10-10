"""Summary generation outside transactions, fenced by lease and source revision."""

from datetime import timedelta

from sqlalchemy import select

from waku.memory.candidates import extract_decisions
from waku.memory.indexing import enqueue_index, refresh_indexes
from waku.memory.lifecycle import blocked, record_version
from waku.storage.models import ContextNode, Job, Project, now


def process_enrichment(sessions, job_id, token, summarizer, extractor=None):
    with sessions() as session:
        job = session.get(Job, job_id)
        if not job or job.status != "running" or job.lease_token != token:
            return False
        project_id = job.project_id
        snapshots = [
            (n.id, n.revision, n.content)
            for n in session.scalars(
                select(ContextNode).where(
                    ContextNode.project_id == project_id,
                    ContextNode.id.in_(job.payload["node_ids"]),
                    ContextNode.status == "active",
                    ContextNode.kind != "directory",
                )
            )
        ]

    def heartbeat():
        with sessions.begin() as session:
            job = session.scalar(
                select(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                .with_for_update()
            )
            if not job:
                raise RuntimeError("enrichment lease lost")
            job.lease_until = now() + timedelta(seconds=60)

    prepared = [
        (
            node_id,
            revision,
            content,
            summarizer.summarize(content, heartbeat),
            extractor.extract(content, heartbeat) if extractor else None,
        )
        for node_id, revision, content in snapshots
    ]
    with sessions.begin() as session:
        session.execute(select(Project).where(Project.id == project_id).with_for_update())
        job = session.scalar(
            select(Job)
            .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
            .with_for_update()
        )
        if not job:
            return False
        applied = extracted = 0
        for node_id, revision, content, summary, proposals in prepared:
            node = session.get(ContextNode, node_id)
            if (
                not node
                or node.status != "active"
                or node.revision != revision
                or node.content != content
                or blocked(session, project_id, node.uri, content)
            ):
                continue
            if (node.abstract, node.overview) != (summary["abstract"], summary["overview"]):
                record_version(session, node)
                node.revision += 1
                node.abstract, node.overview = summary["abstract"], summary["overview"]
            refresh_indexes(session, project_id)
            extracted += extract_decisions(session, node, proposals)
            applied += 1
        if applied:
            enqueue_index(session, project_id)
        job.result = {
            "summarized": applied,
            "stale_skipped": len(snapshots) - applied,
            "candidates": extracted,
            "summary_model": summarizer.model_id,
        }
        job.status, job.error, job.lease_token, job.lease_until = "succeeded", None, None, None
    return True
