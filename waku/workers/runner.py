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

from waku.memory.embeddings import configured_embedder
from waku.memory.indexing import process_index
from waku.storage.database import build_engine, session_factory
from waku.storage.models import Job, now
from waku.workers.ingestion import import_documents

LOG = logging.getLogger(__name__)


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
            .order_by(case((Job.kind == "ingest", 0), else_=1), Job.available_at, Job.id)
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
            return None
        job.attempts += 1
        job.status = "running"
        job.lease_until = current + timedelta(seconds=lease_seconds)
        job.lease_token = str(uuid4())
        return job.id, job.lease_token


def process(sessions, job_id: str, token: str, embedder=None) -> bool:
    try:
        with sessions() as session:
            job = session.get(Job, job_id)
            is_index = job is not None and job.kind == "index"
        if is_index:
            return process_index(sessions, job_id, token, embedder)
        with sessions.begin() as session:
            job = session.scalar(
                select(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_token == token)
                .with_for_update()
            )
            if job is None:
                return False  # a newer worker owns the lease
            if job.kind != "ingest":
                raise ValueError("unsupported job kind")
            job.result = import_documents(session, job)
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
        LOG.warning("Job %s failed (%s)", job_id, type(exc).__name__)
        return False


def run_once(sessions, embedder=None) -> bool:
    claimed = claim(sessions)
    if claimed is None:
        return False
    process(sessions, *claimed, embedder=embedder)
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
    try:
        while True:
            worked = run_once(sessions, embedder=embedder)
            if args.once:
                break
            if not worked:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
