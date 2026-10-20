"""Exercise unified reads, incremental extraction and source lifecycle boundaries."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from evals.deterministic.test_copilot_backend import backend as backend_fixture
from evals.deterministic.test_copilot_backend import project, submit
from evals.deterministic.test_copilot_lifecycle import drain, propose
from evals.deterministic.test_copilot_memory import imported
from evals.deterministic.test_copilot_runtime import run_job, setup_run
from waku.memory.port import MemoryQuery
from waku.memory.postgres import root_uri
from waku.memory.service import MemoryService
from waku.providers.contracts import ModelResponse
from waku.runtime.decisions import Decision
from waku.storage.models import (
    ContextIndex,
    ContextNode,
    Job,
    MemoryCandidate,
    Message,
    Run,
    Thread,
)
from waku.workers.distillation import append_message, enqueue_distillation
from waku.workers.enrichment import process_enrichment
from waku.workers.runner import claim, process, run_once

backend = backend_fixture


def test_populated_week5_upgrade_backfills_sequences_and_stale_dependencies(backend):
    from pathlib import Path
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config

    _client, sessions = backend
    engine = sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires historical PostgreSQL schema")

    def migrate(target, *, downgrade=False):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            config.attributes["version_table_schema"] = connection.scalar(
                text("SELECT current_schema()")
            )
            (command.downgrade if downgrade else command.upgrade)(config, target)

    migrate("0005_runtime", downgrade=True)
    pid = str(uuid4())
    tid, source, fact, downstream, eid, fid = [str(uuid4()) for _ in range(6)]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects(id,created_at,user_id,name,description) VALUES (:id,CURRENT_TIMESTAMP,'default','Legacy','')"
            ),
            {"id": pid},
        )
        connection.execute(
            text("""
            INSERT INTO threads(id,created_at,project_id,title)
            VALUES (:id,CURRENT_TIMESTAMP,:pid,'Legacy chat')
        """),
            {"id": tid, "pid": pid},
        )
        for mid, day in (("later", 2), ("earlier", 1)):
            connection.execute(
                text("""
                INSERT INTO messages(id,created_at,project_id,thread_id,role,content,event_data)
                VALUES (:id,CAST(:day AS timestamptz),:pid,:tid,'user','Legacy message','{}')
            """),
                {"id": mid, "day": f"2026-01-0{day}T00:00:00Z", "pid": pid, "tid": tid},
            )
        for nid, rev, refs in (
            (source, 2, "[]"),
            (fact, 1, f'["{eid}"]'),
            (downstream, 1, f'["{fid}"]'),
        ):
            connection.execute(
                text("""
                INSERT INTO context_nodes(id,created_at,project_id,uri,title,kind,content,
                    abstract,overview,revision,source_event_ids,status)
                VALUES (:id,CURRENT_TIMESTAMP,:pid,:uri,'Legacy','semantic','Content',
                    'Abstract','Overview',:rev,CAST(:refs AS json),'active')
            """),
                {"id": nid, "pid": pid, "uri": f"si://legacy/{nid}", "rev": rev, "refs": refs},
            )
        for evidence_id, node_id in ((eid, source), (fid, fact)):
            connection.execute(
                text("""
                INSERT INTO memory_evidence(id,created_at,node_id,revision,start_char,end_char,
                    quote,checksum,source_event_ids,redacted)
                VALUES (:id,CURRENT_TIMESTAMP,:node,1,0,7,'Content','checksum','[]',false)
            """),
                {"id": evidence_id, "node": node_id},
            )
    migrate("head")
    with sessions() as session:
        thread = session.get(Thread, tid)
        assert thread.event_sequence == 2 and thread.memory_cursor == 0
        assert session.get(Message, "earlier").sequence == 1
        assert session.get(Message, "later").sequence == 2
        assert not session.get(ContextNode, source).needs_review
        assert session.get(ContextNode, fact).needs_review
        assert session.get(ContextNode, downstream).needs_review
        assert session.get(ContextNode, source).representation_revision == 1
    with pytest.raises(DBAPIError, match="Export unified memory state"):
        migrate("0005_runtime", downgrade=True)


class Summary:
    model_id = "test:summary"

    def summarize(self, text, heartbeat):
        heartbeat()
        return {"abstract": "A generated abstract", "overview": "A generated overview"}


def conversation(backend, statements):
    client, sessions = backend
    pid = project(client)
    tid = client.post("/api/threads", json={"project_id": pid}).json()["id"]
    with sessions.begin() as session:
        thread = session.get(Thread, tid)
        for role, text in statements:
            append_message(session, thread, role=role, content=text, event_data={})
        job = enqueue_distillation(session, thread, delay_seconds=0)
    return pid, tid, job.id


def test_flat_retrieval_and_explicit_hierarchy_share_scoped_evidence(backend):
    _, sessions = backend
    pid, leaf, _ = imported(backend, "# Storage\nPostgreSQL is the primary database.")
    memory = MemoryService(sessions)
    flat = memory.retrieve(MemoryQuery("PostgreSQL", project_id=pid))
    nested = memory.retrieve(MemoryQuery("PostgreSQL", project_id=pid, strategy="hierarchical"))
    assert flat.context.hits[0].id == nested.context.hits[0].id == leaf["id"]
    assert not any("directories" in s["stage"] for s in flat.stages)
    assert any("directories" in s["stage"] for s in nested.stages)


def test_react_reuses_initial_gate_and_evidence_across_iterations(backend):
    client, sessions, pid, _, rid, runtime, model, _ = setup_run(
        backend,
        [
            ModelResponse(
                tool_calls=[{"id": "read", "name": "read_memory", "arguments": {"uri": "bad"}}]
            ),
            ModelResponse("The evidence is still present"),
        ],
    )
    submit(client, pid)
    assert run_once(sessions)
    calls = []
    original = runtime.memory.search

    def counted(query):
        calls.append(query)
        return original(query)

    runtime.memory.search = counted
    run_job(sessions, runtime)
    assert len(calls) == 1 and len(model.requests) == 2
    assert "Use pgvector" in model.requests[1].system
    events = client.get(f"/api/runs/{rid}/events").json()
    assert sum(e["kind"] == "span" and e["data"]["name"] == "retrieval_gate" for e in events) == 1
    with sessions() as session:
        assert session.get(Run, rid).context["retrieval_cache"]["hits"]


def test_summary_revision_keeps_existing_evidence_and_candidate_valid(backend):
    client, sessions = backend
    pid, leaf, import_job = imported(backend, "# Source\nUse PostgreSQL.")
    candidate = propose(client, pid, leaf, content="Use PostgreSQL")
    with sessions() as session:
        enrich_id = session.get(Job, import_job["id"]).result["enrich_job_id"]
    # Claim the exact enrichment job, leaving the candidate queued for later acceptance.
    with sessions.begin() as session:
        job = session.get(Job, enrich_id)
        job.status, job.lease_token = "running", "summary-token"
    assert process_enrichment(sessions, enrich_id, "summary-token", Summary())
    detail = client.get(f"/api/memory/{leaf['id']}").json()
    assert detail["revision"] == 1 and detail["representation_revision"] == 2
    drain(sessions)
    with sessions() as session:
        assert session.get(MemoryCandidate, candidate["id"]).status == "applied"


def test_source_update_hides_derived_memory_until_explicit_revision(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend, "# Source\nUse SQLite.")
    candidate = propose(client, pid, leaf)
    drain(sessions)
    with sessions() as session:
        derived_id = session.get(MemoryCandidate, candidate["id"]).node_id
    submit(
        client,
        pid,
        key="changed",
        docs=[{"path": "docs/design.md", "content": "# Source\nUse PostgreSQL."}],
    )
    assert run_once(sessions)
    detail = client.get(f"/api/memory/{derived_id}").json()
    assert detail["needs_review"] is True
    assert not MemoryService(sessions).search(MemoryQuery("SQLite", project_id=pid))
    fresh_leaf = client.get(f"/api/memory/{leaf['id']}").json()
    update = propose(
        client,
        pid,
        fresh_leaf,
        action="UPDATE",
        key="fix",
        content="Use PostgreSQL",
        expected_revision=1,
    )
    drain(sessions)
    assert client.get(f"/api/memory/{derived_id}").json()["needs_review"] is False
    with sessions() as session:
        assert session.get(MemoryCandidate, update["id"]).status == "applied"


def test_incremental_user_extraction_ignores_assistant_and_retries(backend):
    client, sessions = backend
    pid, tid, jid = conversation(
        backend,
        [
            ("user", "决定[storage]: 项目使用 PostgreSQL。"),
            ("assistant", "决定[imagined]: 已部署至生产环境。"),
        ],
    )
    drain(sessions)
    rows = client.get(f"/api/projects/{pid}/memory/candidates").json()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    assert rows[0]["content"] == "项目使用 PostgreSQL。"
    assert not MemoryService(sessions).search(MemoryQuery("PostgreSQL", project_id=pid))
    # The session source is available only when explicitly requested.
    assert MemoryService(sessions).search(
        MemoryQuery("PostgreSQL", project_id=pid, kinds=("session",))
    )
    with sessions.begin() as session:
        assert session.get(Thread, tid).memory_cursor == 2
        old = session.get(Job, jid)
        old.status, old.lease_token = "running", "replay"
    assert process(sessions, jid, "replay")
    assert len(client.get(f"/api/projects/{pid}/memory/candidates").json()) == 1
    client.post(f"/api/memory/candidates/{rows[0]['id']}/apply")
    drain(sessions)
    hits = MemoryService(sessions).search(MemoryQuery("PostgreSQL", project_id=pid))
    assert hits and hits[0].source == "episodic"
    with sessions.begin() as session:
        thread = session.get(Thread, tid)
        append_message(
            session, thread, role="user", content="决定[workers]: 使用多个 Worker。", event_data={}
        )
        enqueue_distillation(session, thread, delay_seconds=0)
    drain(sessions)
    rows = client.get(f"/api/projects/{pid}/memory/candidates").json()
    assert len(rows) == 2 and sum("PostgreSQL" in row["content"] for row in rows) == 1


def test_message_and_delayed_job_are_committed_together_and_flush_is_scoped(backend):
    client, sessions, _, tid, rid, _, _, payload = setup_run(backend, [ModelResponse("unused")])
    duplicate = client.post(f"/api/threads/{tid}/messages", json=payload)
    assert duplicate.json()["id"] == rid
    with sessions() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "distill")).all()
        assert len(jobs) == 1
        assert session.get(Thread, tid).event_sequence == 1
        assert session.scalar(select(Message).where(Message.thread_id == tid)).sequence == 1
    assert client.post(f"/api/threads/{tid}/memory/distill").status_code == 202
    other_pid = project(client, "Other")
    with sessions.begin() as session:
        from waku.storage.models import Project

        session.get(Project, other_pid).user_id = "someone-else"
        other_thread = Thread(project_id=other_pid, title="Hidden")
        session.add(other_thread)
        session.flush()
    assert client.post(f"/api/threads/{other_thread.id}/memory/distill").status_code == 404


def test_distillation_resumes_bounded_batch_before_finishing(backend):
    client, sessions = backend
    pid, tid, jid = conversation(
        backend,
        [("assistant", "Acknowledged.")] * 100
        + [("user", "Decision[batch-tail]: Use PostgreSQL.")],
    )
    assert run_once(sessions)
    with sessions() as session:
        assert session.get(Thread, tid).memory_cursor == 100
        assert session.get(Job, jid).status == "queued"
        assert not session.scalar(select(MemoryCandidate.id))
    assert run_once(sessions)
    with sessions() as session:
        assert session.get(Thread, tid).memory_cursor == 101
        assert session.get(Job, jid).status == "succeeded"
    candidates = client.get(f"/api/projects/{pid}/memory/candidates").json()
    assert len(candidates) == 1 and candidates[0]["title"] == "batch-tail"
    with sessions() as session:
        source = session.scalar(select(ContextNode).where(ContextNode.kind == "session"))
    rejected = client.post(
        f"/api/projects/{pid}/memory/candidates",
        json={
            "uri": source.uri,
            "kind": "semantic",
            "title": "Overwrite source",
            "content": "Replace history",
            "action": "UPDATE",
            "expected_revision": 1,
            "evidence_ids": candidates[0]["evidence_ids"],
            "idempotency_key": "overwrite-session",
        },
    )
    assert rejected.status_code == 409
    assert (
        client.get(f"/api/memory/{source.id}").json()["content"]
        == "Decision[batch-tail]: Use PostgreSQL."
    )


def test_forget_session_source_redacts_message_and_prevents_redistillation(backend):
    client, sessions = backend
    pid, tid, jid = conversation(backend, [("user", "决定[secret]: 机密方案采用 PostgreSQL。")])
    drain(sessions)
    with sessions() as session:
        node = session.scalar(
            select(ContextNode).where(ContextNode.project_id == pid, ContextNode.kind == "session")
        )
    assert client.post(f"/api/memory/{node.id}/forget", json={}).status_code == 200
    with sessions.begin() as session:
        assert session.scalar(select(Message).where(Message.thread_id == tid)).event_data[
            "redacted"
        ]
        # Simulate an old cursor/retry after forgetting.
        session.get(Thread, tid).memory_cursor = 0
        job = session.get(Job, jid)
        job.status, job.lease_token = "running", "replay"
    assert process(sessions, jid, "replay")
    candidates = client.get(f"/api/projects/{pid}/memory/candidates").json()
    assert all(c["status"] == "skipped" and not c["content"] for c in candidates)
    assert not MemoryService(sessions).search(
        MemoryQuery("PostgreSQL", project_id=pid, kinds=("session",))
    )


def test_distillation_lost_lease_does_not_commit_candidate_or_cursor(backend):
    _, sessions = backend
    _, tid, jid = conversation(backend, [("user", "Use PostgreSQL.")])
    claim_id, token = claim(sessions)
    assert claim_id == jid

    class Interrupted:
        def extract(self, content, heartbeat):
            with sessions.begin() as session:
                session.get(Job, jid).lease_token = "new-owner"
            return [{"key": "storage", "kind": "semantic", "content": content, "quote": content}]

    assert not process(sessions, jid, token, extractor=Interrupted())
    with sessions() as session:
        assert session.get(Thread, tid).memory_cursor == 0
        assert not session.scalar(select(MemoryCandidate.id))


def test_retention_policy_is_shared_by_tool_and_session_proposals(backend):
    _, sessions = backend
    pid, _, jid = conversation(backend, [("user", "决定[temp]: 临时试用 PostgreSQL。")])

    class Reject:
        def decide(self, purpose, state):
            assert purpose == "retain"
            return Decision(False, 0.01, 0.99, "test")

    from waku.workers.distillation import process_distillation

    _, token = claim(sessions)
    assert process_distillation(sessions, jid, token, decisions=Reject())
    with sessions() as session:
        candidate = session.scalar(select(MemoryCandidate))
        assert candidate.status == "skipped"
        evidence = list(candidate.evidence_ids)
    service = MemoryService(sessions)
    result = service.propose(
        {
            "uri": root_uri("default", pid) + "/memories/temp.md",
            "title": "Temporary",
            "content": "临时试用 PostgreSQL。",
            "kind": "semantic",
            "action": "ADD",
            "expected_revision": None,
            "supersedes_id": None,
            "evidence_ids": evidence,
            "idempotency_key": "tool-temp",
        },
        user_id="default",
        project_id=pid,
        decisions=Reject(),
    )
    assert result["status"] == "skipped"


def test_representation_worker_cannot_commit_old_summary_vectors(backend):
    from waku.memory.embeddings import DemoEmbedder
    from waku.memory.indexing import process_index, refresh_indexes

    _, sessions = backend
    _, leaf, _ = imported(backend, "# Source\nPostgreSQL.")
    jid, token = claim(sessions)
    with sessions() as session:
        assert session.get(Job, jid).kind == "index"

    class Racing(DemoEmbedder):
        def embed(self, texts):
            with sessions.begin() as session:
                source = session.get(ContextNode, leaf["id"])
                source.abstract = "A replacement summary"
                source.representation_revision += 1
                refresh_indexes(session, source.project_id)
            return super().embed(texts)

    assert process_index(sessions, jid, token, Racing())
    with sessions() as session:
        rows = session.scalars(select(ContextIndex).where(ContextIndex.node_id == leaf["id"])).all()
        assert all(row.embedding is None for row in rows)
