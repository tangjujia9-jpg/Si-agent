"""Create an API with explicit storage. Run with uvicorn waku.server.app:create_app --factory."""

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from waku.memory.embeddings import configured_embedder
from waku.memory.indexing import enqueue_index, refresh_indexes
from waku.memory.port import MemoryQuery
from waku.memory.postgres import PostgresMemory
from waku.server.schemas import (
    IngestInput,
    JobOutput,
    NodeOutput,
    NodeSummary,
    ProjectInput,
    ProjectOutput,
    SearchInput,
    SearchOutput,
    ThreadInput,
    ThreadOutput,
)
from waku.storage.database import build_engine, session_factory
from waku.storage.models import ContextIndex, ContextNode, Job, Project, Thread

USER_ID = "default"
LOG = logging.getLogger(__name__)


def create_app(database_url: str | None = None, *, engine=None, embedder=None) -> FastAPI:
    # Supplying an engine lets tests use isolated SQLite databases. Importing
    # this module neither connects to a database nor initializes user state.
    database = engine or build_engine(
        database_url
        or os.environ.get(
            "SI_DATABASE_URL", "postgresql+psycopg://si:si-local@127.0.0.1:5432/si_agent"
        )
    )
    sessions = session_factory(database)
    memory_store = PostgresMemory(sessions, embedder or configured_embedder())

    @asynccontextmanager
    async def lifespan(_app):
        yield
        database.dispose()

    app = FastAPI(title="Si-agent Context Copilot", version="0.1", lifespan=lifespan)
    app.state.engine = database

    def db():
        with sessions() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    dependency = Depends(db, scope="function")

    def project_or_404(session: Session, project_id: str) -> Project:
        project = session.scalar(
            select(Project).where(Project.id == project_id, Project.user_id == USER_ID)
        )
        if project is None:
            raise HTTPException(404, "Project not found")
        return project

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(_request: Request, exc: SQLAlchemyError):
        # SQL errors can include imported content and credentials. Do not
        # return or log exception payloads to a local browser.
        LOG.error("Storage operation failed (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Storage unavailable"})

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "si-agent-api"}

    @app.get("/ready")
    def ready(session: Session = dependency):
        session.execute(text("SELECT 1"))
        session.execute(select(Project.id).limit(1))
        session.execute(select(ContextIndex.id).limit(1))
        return {"status": "ready"}

    @app.post("/api/projects", response_model=ProjectOutput, status_code=201)
    def create_project(payload: ProjectInput, session: Session = dependency):
        project = Project(user_id=USER_ID, **payload.model_dump())
        session.add(project)
        session.flush()
        return project

    @app.get("/api/projects", response_model=list[ProjectOutput])
    def list_projects(session: Session = dependency):
        return session.scalars(
            select(Project).where(Project.user_id == USER_ID).order_by(Project.created_at.desc())
        ).all()

    @app.post("/api/projects/{project_id}/ingest", response_model=JobOutput, status_code=202)
    def ingest(project_id: str, payload: IngestInput, session: Session = dependency):
        project_or_404(session, project_id)
        data = {"documents": [doc.model_dump() for doc in payload.documents]}
        digest = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

        def existing():
            job = session.scalar(
                select(Job).where(
                    Job.project_id == project_id, Job.idempotency_key == payload.idempotency_key
                )
            )
            if job and job.payload_hash != digest:
                raise HTTPException(409, "Idempotency key already used with different documents")
            return job

        if job := existing():
            return job
        job = Job(
            project_id=project_id,
            kind="ingest",
            payload=data,
            payload_hash=digest,
            idempotency_key=payload.idempotency_key,
        )
        # Savepoint catches concurrent retries without discarding the outer
        # request transaction. The unique key is the actual idempotency guard.
        try:
            with session.begin_nested():
                session.add(job)
                session.flush()
        except IntegrityError:
            if found := existing():
                return found
            raise
        return job

    @app.get("/api/jobs/{job_id}", response_model=JobOutput)
    def job_status(job_id: str, session: Session = dependency):
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        project_or_404(session, job.project_id)
        return job

    @app.get("/api/projects/{project_id}/context/tree", response_model=list[NodeSummary])
    def tree(project_id: str, session: Session = dependency):
        project_or_404(session, project_id)
        return session.scalars(
            select(ContextNode)
            .where(ContextNode.project_id == project_id, ContextNode.status == "active")
            .order_by(ContextNode.uri)
        ).all()

    @app.get("/api/memory/search", response_model=SearchOutput)
    def search_memory(payload: Annotated[SearchInput, Query()], session: Session = dependency):
        project_or_404(session, payload.project_id)
        result = memory_store.retrieve(
            MemoryQuery(
                text=payload.q,
                user_id=USER_ID,
                project_id=payload.project_id,
                top_k=payload.top_k,
                max_tokens=payload.max_tokens,
                tiers=tuple(payload.tiers.split(",")),
                include_details=payload.include_details,
                as_of=payload.as_of,
            )
        )
        return {
            "hits": result.context.hits,
            "context": result.context.text,
            "estimated_tokens": result.context.estimated_tokens,
            "truncated": result.context.truncated,
            "embedding_model": result.embedding_model,
            "warnings": result.warnings,
            "stages": result.stages,
        }

    @app.post(
        "/api/projects/{project_id}/context/reindex", response_model=JobOutput, status_code=202
    )
    def reindex(project_id: str, session: Session = dependency):
        project_or_404(session, project_id)
        session.execute(select(Project).where(Project.id == project_id).with_for_update())
        refresh_indexes(session, project_id)
        return enqueue_index(session, project_id)

    @app.get("/api/memory/{node_id}", response_model=NodeOutput)
    def memory(node_id: str, session: Session = dependency):
        node = session.get(ContextNode, node_id)
        if node is None:
            raise HTTPException(404, "Context node not found")
        project_or_404(session, node.project_id)
        return node

    @app.post("/api/threads", response_model=ThreadOutput, status_code=201)
    def create_thread(payload: ThreadInput, session: Session = dependency):
        project_or_404(session, payload.project_id)
        thread = Thread(**payload.model_dump())
        session.add(thread)
        session.flush()
        return thread

    return app
