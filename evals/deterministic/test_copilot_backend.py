"""Offline API/worker invariants; SQLite does not stand in for vector tests."""

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi", reason="install the copilot extra")
pytest.importorskip("sqlalchemy", reason="install the copilot extra")
pytest.importorskip("pgvector", reason="install the copilot extra")

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from waku.server.app import create_app
from waku.storage.database import build_engine, session_factory
from waku.storage.models import Base, ContextNode, Job, Message, Project, Thread, now
from waku.workers.runner import claim, process, run_once


@pytest.fixture(params=["sqlite", "postgres"])
def backend(tmp_path, request):
    schema = None
    admin = None
    if request.param == "postgres":
        url = os.environ.get("SI_TEST_DATABASE_URL")
        if not url:
            pytest.skip("set SI_TEST_DATABASE_URL for real Postgres migration tests")
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import create_engine

        admin = build_engine(url)
        schema = "test_copilot_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["version_table_schema"] = schema
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    else:
        engine = build_engine(f"sqlite:///{tmp_path / 'copilot.db'}")
        Base.metadata.create_all(engine)
    with TestClient(create_app(engine=engine)) as client:
        yield client, session_factory(engine)
    engine.dispose()
    if schema:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def project(client, name="Project Alpha"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def submit(client, project_id, key="import-1", docs=None):
    return client.post(
        f"/api/projects/{project_id}/ingest",
        json={
            "idempotency_key": key,
            "documents": docs
            or [{"path": "docs/design.md", "content": "# Alpha\n\nUse pgvector."}],
        },
    )


def test_project_import_worker_and_inspection_round_trip(backend):
    client, sessions = backend
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    pid = project(client)
    response = submit(client, pid)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert client.get(f"/api/projects/{pid}/context/tree").json() == []
    assert run_once(sessions)
    completed = client.get(f"/api/jobs/{job['id']}").json()
    assert completed["status"] == "succeeded"
    assert completed["result"]["imported"] == 1
    tree = client.get(f"/api/projects/{pid}/context/tree").json()
    node = next(n for n in tree if n["kind"] == "resource")
    assert node["uri"] == f"waku://users/default/projects/{pid}/resources/docs/design.md"
    assert node["source_event_ids"] == [job["id"]]
    assert client.get(f"/api/memory/{node['id']}").json()["content"] == "# Alpha\n\nUse pgvector."
    assert client.post("/api/threads", json={"project_id": pid}).status_code == 201


def test_import_is_idempotent_and_rejects_reused_key_with_different_payload(backend):
    client, sessions = backend
    pid = project(client)
    first = submit(client, pid).json()
    assert submit(client, pid).json()["id"] == first["id"]
    response = submit(client, pid, docs=[{"path": "docs/design.md", "content": "changed"}])
    assert response.status_code == 409
    run_once(sessions)
    submit(client, pid, key="import-2")
    run_once(sessions)
    with sessions() as session:
        nodes = session.scalars(select(ContextNode).where(ContextNode.kind == "resource")).all()
        assert len(nodes) == 1
        assert nodes[0].revision == 1


def test_document_update_has_stable_uri_and_new_revision(backend):
    client, sessions = backend
    pid = project(client)
    submit(client, pid)
    run_once(sessions)
    before = next(
        n for n in client.get(f"/api/projects/{pid}/context/tree").json() if n["kind"] == "resource"
    )
    submit(
        client, pid, key="updated", docs=[{"path": "docs/design.md", "content": "Use Postgres."}]
    )
    run_once(sessions)
    after = client.get(f"/api/memory/{before['id']}").json()
    assert after["uri"] == before["uri"]
    assert after["revision"] == 2
    assert after["content"] == "Use Postgres."


def test_import_preserves_source_whitespace(backend):
    client, sessions = backend
    pid = project(client)
    content = "\n    return 'keep indentation'  \n"
    response = submit(client, pid, docs=[{"path": "snippet.py", "content": content}])
    assert response.status_code == 202
    run_once(sessions)
    node = next(
        n for n in client.get(f"/api/projects/{pid}/context/tree").json() if n["kind"] == "resource"
    )
    assert "content" not in node
    assert client.get(f"/api/memory/{node['id']}").json()["content"] == content


@pytest.mark.parametrize(
    "path",
    ["../secret.md", "/tmp/test.md", "x\\y.md", "a//b.md", "x/%2e%2e.md", "x#y.md", "image.png"],
)
def test_ingestion_refuses_unsafe_or_unsupported_paths(backend, path):
    client, _ = backend
    pid = project(client)
    assert submit(client, pid, docs=[{"path": path, "content": "test"}]).status_code == 422


def test_empty_name_duplicate_paths_and_cross_project_access(backend):
    client, sessions = backend
    assert client.post("/api/projects", json={"name": "  "}).status_code == 422
    pid = project(client)
    other = project(client, "Other")
    submit(client, pid)
    run_once(sessions)
    assert client.get(f"/api/projects/{other}/context/tree").json() == []
    assert client.get("/api/projects/missing/context/tree").status_code == 404
    docs = [{"path": "a.md", "content": "a"}, {"path": "a.md", "content": "b"}]
    assert submit(client, pid, docs=docs).status_code == 422
    with sessions.begin() as session:
        hidden = Project(user_id="another-user", name="Private")
        session.add(hidden)
        session.flush()
        hidden_id = hidden.id
    assert hidden_id not in {p["id"] for p in client.get("/api/projects").json()}
    assert submit(client, hidden_id).status_code == 404


def test_database_refuses_message_in_another_projects_thread(backend):
    client, sessions = backend
    a, b = project(client), project(client, "B")
    with sessions.begin() as session:
        thread = Thread(project_id=a, title="A")
        session.add(thread)
        session.flush()
        tid = thread.id
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(Message(thread_id=tid, project_id=b, role="user", content="wrong scope"))


def test_worker_reclaims_expired_lease_and_fences_old_worker(backend):
    client, sessions = backend
    job_id = submit(client, project(client)).json()["id"]
    claimed = claim(sessions)
    assert claimed[0] == job_id
    assert claim(sessions) is None
    with sessions.begin() as session:
        session.get(Job, job_id).lease_until = now() - timedelta(seconds=1)
    reclaimed = claim(sessions)
    assert reclaimed[1] != claimed[1]
    assert process(sessions, *claimed) is False
    assert process(sessions, *reclaimed) is True
    assert client.get(f"/api/jobs/{job_id}").json()["attempts"] == 2


def test_postgres_workers_claim_distinct_jobs_and_lock_busy_rows(backend):
    client, sessions = backend
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires Postgres SKIP LOCKED semantics")
    pid = project(client)
    first = submit(client, pid).json()["id"]
    second = submit(client, pid, key="second").json()["id"]
    with sessions.begin() as locked:
        locked.scalar(select(Job).where(Job.id == first).with_for_update())
        claimed = claim(sessions)
        assert claimed[0] == second
    assert claim(sessions)[0] == first


def test_failed_import_rolls_back_nodes_and_stops_after_retry_limit(backend):
    client, sessions = backend
    docs = [
        {"path": "a.md", "content": "document"},
        {"path": "a.md/nested.txt", "content": "collision"},
    ]
    job_id = submit(client, project(client), docs=docs).json()["id"]
    # This batch attempts to use a document as a parent directory.
    for attempt in range(3):
        with sessions.begin() as session:
            session.get(Job, job_id).available_at = now() - timedelta(seconds=1)
        assert run_once(sessions)
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed"
        assert job.attempts == 3
        assert session.scalar(select(ContextNode.id)) is None


def test_unmigrated_storage_is_not_ready_and_errors_are_sanitized(tmp_path):
    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'empty.db'}")) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Storage unavailable"}
