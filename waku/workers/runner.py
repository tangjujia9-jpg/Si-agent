"""Leased job processing with fencing, transactional imports, and bounded retries.

Postgres workers claim with SKIP LOCKED. A killed worker's job is reclaimable
after the lease expires. The processing transaction locks its job row so
another worker cannot apply the same import while it is being committed.
"""

import argparse
import logging
import os
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, case, or_, select

from waku.memory.candidates import apply_candidate
from waku.memory.embeddings import configured_embedder
from waku.memory.extraction import configured_extractor
from waku.memory.indexing import enqueue_index, process_index, refresh_indexes
from waku.memory.summaries import ExtractiveSummarizer, configured_summarizer
from waku.storage.database import build_engine, session_factory
from waku.storage.models import Job, Project, now
from waku.workers.enrichment import process_enrichment
from waku.workers.ingestion import import_documents

LOG = logging.getLogger(__name__)


def fail_runtime_job(session, job):
    """A dead worker must not leave a run permanently waiting for an exhausted job."""
    from waku.runtime.runner import TERMINAL, append_event
    from waku.storage.models import OutboxEvent, Run, ToolOperation

    operation = (
        session.get(ToolOperation, job.payload.get("operation_id"))
        if job.kind == "tool_outbox"
        else None
    )
    rid = operation.run_id if operation else job.payload.get("run_id")
    if not rid:
        return
    run = session.scalar(select(Run).where(Run.id == rid).with_for_update())
    if not run or run.status in TERMINAL:
        return
    # After a dispatched write, loss of the result means the outcome is uncertain.
    status = "unknown" if operation and operation.status == "running" else "failed"
    run.status = status
    run.context = {**run.context, "error_code": "worker_retry_exhausted"}
    if operation:
        from waku.domain.contracts import ToolResult

        operation.status = status
        operation.result = ToolResult(
            status,
            "Worker retries exhausted; inspect dispatched actions",
            operation_id=operation.id,
            error_code="worker_retry_exhausted",
        ).to_dict()
        outbox = session.scalar(select(OutboxEvent).where(OutboxEvent.operation_id == operation.id))
        if outbox:
            outbox.status = status
    append_event(
        session, run, "run_end", {"status": status, "error_code": "worker_retry_exhausted"}
    )


def claim(sessions, lease_seconds: int = 60) -> tuple[str, str] | None:
    current = now()
    with sessions.begin() as session:
        job = session.scalar(
            select(Job)
            .where(
                or_(
                    and_(Job.status == "queued", Job.available_at <= current),
                    and_(Job.status == "running", Job.lease_until <= current),
                )
            )
            .order_by(
                case(
                    (Job.kind == "run", -2),
                    (Job.kind == "tool_outbox", -2),
                    (Job.kind == "ingest", -3),
                    (Job.kind == "rebuild", 0),
                    (Job.kind == "consolidate", 1),
                    (Job.kind == "index", 2),
                    else_=3,
                ),
                Job.available_at,
                Job.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            job.error = "Retry limit reached after worker interruption"
            job.lease_until = None
            job.lease_token = None
            fail_runtime_job(session, job)
            return None
        job.attempts += 1
        job.status = "running"
        job.lease_until = current + timedelta(seconds=lease_seconds)
        job.lease_token = str(uuid4())
        return job.id, job.lease_token


def _process(
    sessions, job_id: str, token: str, embedder=None, summarizer=None, extractor=None, runtime=None
) -> bool:
    try:
        with sessions() as session:
            job = session.get(Job, job_id)
            is_index = job is not None and job.kind == "index"
            is_enrich = job is not None and job.kind == "enrich"
            runtime_kind = job.kind if job and job.kind in ("run", "tool_outbox") else None
            project_id = job.project_id if job else None
        if runtime_kind:
            if runtime is None:
                from waku.memory.service import MemoryService
                from waku.runtime.runner import Runtime
                from waku.tools.executor import project_executor

                memory = MemoryService(sessions, embedder)
                runtime = Runtime(sessions, memory, project_executor(memory, sessions))
            return (runtime.process if runtime_kind == "run" else runtime.process_operation)(
                job_id, token
            )
        if is_index:
            return process_index(sessions, job_id, token, embedder)
        if is_enrich:
            return process_enrichment(
                sessions,
                job_id,
                token,
                summarizer or ExtractiveSummarizer(),
                extractor,
                runtime.decisions if runtime else None,
            )
        with sessions() as session:
            is_distill = session.get(Job, job_id).kind == "distill"
        if is_distill:
            from waku.workers.distillation import process_distillation

            return process_distillation(
                sessions, job_id, token, extractor, runtime.decisions if runtime else None
            )
        with sessions.begin() as session:
            session.execute(select(Project).where(Project.id == project_id).with_for_update())
            job = session.scalar(
                select(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                .with_for_update()
            )
            if job is None:
                return False  # a newer worker owns the lease
            if job.kind == "consolidate":
                job.result = apply_candidate(session, job)
            elif job.kind == "ingest":
                job.result = import_documents(session, job)
            elif job.kind == "rebuild":
                refresh_indexes(session, job.project_id)
                indexing = enqueue_index(session, job.project_id)
                job.result = {"index_job_id": indexing.id}
            else:
                raise ValueError("unsupported job kind")
            job.status = "succeeded"
            job.lease_until = None
            job.lease_token = None
            job.error = None
        return True
    except Exception as exc:
        # The failed processing transaction has rolled back every node write.
        # Error text deliberately excludes document content and SQL payloads.
        with sessions.begin() as session:
            job = session.scalar(
                select(Job).where(Job.id == job_id, Job.lease_token == token).with_for_update()
            )
            if job is not None:
                job.status = "failed" if job.attempts >= job.max_attempts else "queued"
                job.available_at = now() + timedelta(seconds=2**job.attempts)
                job.lease_until = None
                job.lease_token = None
                job.error = f"Job failed ({type(exc).__name__})"
                if job.status == "failed":
                    fail_runtime_job(session, job)
        LOG.warning("Job %s failed (%s)", job_id, type(exc).__name__)
        return False


def process(sessions, job_id, token, embedder=None, summarizer=None, extractor=None, runtime=None):
    from waku.runtime.telemetry import Telemetry

    with sessions() as session:
        job = session.get(Job, job_id)
        kind, project_id, attempt = job.kind, job.project_id, job.attempts
    if kind in {"run", "tool_outbox"}:
        return _process(sessions, job_id, token, embedder, summarizer, extractor, runtime)
    records = []
    trace = Telemetry(records.append, runtime.exporter if runtime else None, trace_id=job_id)
    identity = {"job_id": job_id, "project_id": project_id, "stage": kind}
    if job.payload.get("thread_id"):
        identity["session_id"] = job.payload["thread_id"]
    if job.payload.get("candidate_id"):
        identity["candidate_id"] = job.payload["candidate_id"]
    with trace.span("job", identity) as (_, attrs):
        ok = _process(sessions, job_id, token, embedder, summarizer, extractor, runtime)
        attrs["status"] = "succeeded" if ok else "failed"
    with sessions.begin() as session:
        job = session.get(Job, job_id)
        # A stale worker must not replace a newer worker's result or tracing.
        if job.lease_token is None and job.attempts == attempt:
            job.result = {**job.result, "spans": records[-200:]}
    return ok


def run_once(sessions, embedder=None, summarizer=None, extractor=None, runtime=None) -> bool:
    claimed = claim(sessions)
    if claimed is None:
        return False
    process(
        sessions,
        *claimed,
        embedder=embedder,
        summarizer=summarizer,
        extractor=extractor,
        runtime=runtime,
    )
    return True


def main():
    parser = argparse.ArgumentParser(description="Process Copilot jobs")
    parser.add_argument("--once", action="store_true", help="claim and process at most one job")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    url = os.environ.get("SI_DATABASE_URL")
    if not url:
        parser.error("SI_DATABASE_URL is required")
    engine = build_engine(url)
    sessions = session_factory(engine)
    embedder = configured_embedder()
    summarizer = configured_summarizer()
    extractor = configured_extractor()
    from waku.memory.service import MemoryService
    from waku.runtime.decisions import configured_decisions
    from waku.runtime.runner import Runtime
    from waku.runtime.telemetry import configured_exporter
    from waku.tools.executor import project_executor

    memory = MemoryService(sessions, embedder)
    exporter = configured_exporter()
    runtime = Runtime(
        sessions,
        memory,
        project_executor(memory, sessions),
        decisions=configured_decisions(),
        exporter=exporter,
    )
    try:
        while True:
            worked = run_once(
                sessions,
                embedder=embedder,
                summarizer=summarizer,
                extractor=extractor,
                runtime=runtime,
            )
            if args.once:
                break
            if not worked:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(runtime.decisions, "close"):
            runtime.decisions.close()
        if exporter:
            exporter.close()
        engine.dispose()


if __name__ == "__main__":
    main()
