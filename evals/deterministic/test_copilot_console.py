"""Project console uses the actual API, worker, evidence and approval boundaries."""

import json

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")
pytest.importorskip("pgvector")

from sqlalchemy import select

from evals.deterministic.test_copilot_backend import backend as backend_fixture
from evals.deterministic.test_copilot_lifecycle import drain
from evals.deterministic.test_copilot_memory import imported
from evals.deterministic.test_copilot_runtime import run_job, setup_run
from evals.deterministic.test_unified_memory import conversation
from waku.memory.service import MemoryService
from waku.providers.contracts import ModelResponse
from waku.storage.models import ContextNode, MemoryCandidate, MemoryEvidence, Message, Thread
from waku.workers.distillation import append_message, enqueue_distillation
from waku.workers.runner import claim, process, run_once

backend = backend_fixture


def accepted_fact(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend, "Decision[storage]: Use SQLite.")
    drain(sessions)
    rows = client.get(f"/api/projects/{pid}/memory/candidates").json()
    candidate = rows[0]
    client.post(f"/api/memory/candidates/{candidate['id']}/apply")
    drain(sessions)
    applied = next(
        c
        for c in client.get(f"/api/projects/{pid}/memory/candidates").json()
        if c["id"] == candidate["id"]
    )
    return pid, leaf, client.get(f"/api/memory/{applied['node_id']}").json()


def test_console_history_scope_pagination_and_sse_reconnect(backend):
    client, sessions, pid, tid, rid, runtime, _, _ = setup_run(backend, [ModelResponse("Done")])
    run_job(sessions, runtime)
    threads = client.get(f"/api/projects/{pid}/threads").json()
    assert threads[0]["id"] == tid
    history = client.get(f"/api/threads/{tid}/messages?limit=1").json()
    assert history[0]["role"] == "assistant"
    previous = client.get(f"/api/threads/{tid}/messages?before={history[0]['sequence']}").json()
    assert previous[0]["role"] == "user"
    assert client.get(f"/api/threads/{tid}/runs").json()[0]["id"] == rid
    events = client.get(f"/api/runs/{rid}/events").json()
    cursor = events[-2]["id"]
    with client.stream(
        "GET", f"/api/runs/{rid}/events/stream", headers={"Last-Event-ID": str(cursor)}
    ) as response:
        body = "".join(response.iter_text())
    assert f"id: {cursor}\n" not in body and f"id: {events[-1]['id']}\n" in body
    assert "event: snapshot" in body
    assert (
        client.get(
            f"/api/runs/{rid}/events/stream", headers={"Last-Event-ID": "invalid"}
        ).status_code
        == 422
    )
    with sessions.begin() as session:
        from waku.storage.models import Project

        session.get(Project, pid).user_id = "other"
    for path in (
        f"/api/projects/{pid}/threads",
        f"/api/threads/{tid}/messages",
        f"/api/threads/{tid}/runs",
        f"/api/projects/{pid}/jobs",
        f"/api/projects/{pid}/evidence?ids=x",
    ):
        assert client.get(path).status_code == 404


def test_correction_has_new_user_evidence_and_optimistic_review(backend):
    client, sessions = backend
    _, _, node = accepted_fact(backend)
    payload = {
        "content": "Use PostgreSQL instead.",
        "expected_revision": 1,
        "idempotency_key": "correction-1",
    }
    response = client.post(f"/api/memory/{node['id']}/corrections", json=payload)
    assert response.status_code == 202
    candidate = response.json()
    assert candidate["status"] == "pending" and candidate["job_id"] is None
    assert (
        client.post(f"/api/memory/{node['id']}/corrections", json=payload).json()["id"]
        == candidate["id"]
    )
    with sessions() as session:
        quote = session.get(MemoryEvidence, candidate["evidence_ids"][0])
        source = session.get(ContextNode, quote.node_id)
        assert quote.quote == payload["content"] and source.kind == "session"
        assert session.get(Message, source.source_event_ids[0]).role == "user"
    client.post(f"/api/memory/candidates/{candidate['id']}/apply")
    assert client.post(f"/api/memory/candidates/{candidate['id']}/skip").status_code == 409
    drain(sessions)
    updated = client.get(f"/api/memory/{node['id']}").json()
    assert updated["content"] == payload["content"] and updated["revision"] == 2
    assert (
        client.post(
            f"/api/memory/{node['id']}/corrections", json={**payload, "idempotency_key": "stale"}
        ).status_code
        == 409
    )
    versions = client.get(f"/api/memory/{node['id']}/versions").json()
    assert any(v["snapshot"]["content"] == "Use SQLite." for v in versions)
    preview = client.get(f"/api/memory/{source.id}/forget-preview").json()
    assert node["id"] in preview["derived_node_ids"] and preview["redacts_messages"]
    client.post(f"/api/memory/{source.id}/forget", json={})
    assert client.get(f"/api/memory/{node['id']}").json()["content"] == ""


def test_skip_is_idempotent_and_does_not_apply(backend):
    client, sessions = backend
    pid, tid, _ = conversation(backend, [("user", "Decision[skip-me]: Use PostgreSQL.")])
    drain(sessions)
    c = client.get(f"/api/projects/{pid}/memory/candidates").json()[0]
    assert client.post(f"/api/memory/candidates/{c['id']}/skip").json()["reason"] == "user_skipped"
    assert client.post(f"/api/memory/candidates/{c['id']}/skip").json()["status"] == "skipped"
    assert client.post(f"/api/memory/candidates/{c['id']}/apply").json()["job_id"] is None
    assert client.get(f"/api/threads/{tid}/messages").json()[0]["content"].startswith("Decision")


def test_contextual_extraction_aligns_updates_without_assistant_evidence(backend):
    client, sessions = backend
    pid, _, node = accepted_fact(backend)
    tid = client.post("/api/threads", json={"project_id": pid}).json()["id"]
    with sessions.begin() as session:
        thread = session.get(Thread, tid)
        append_message(
            session,
            thread,
            role="user",
            content="We are discussing the project database.",
            event_data={},
        )
        append_message(
            session,
            thread,
            role="assistant",
            content="Fabricated fact about storage",
            event_data={},
        )
        append_message(
            session, thread, role="user", content="Use PostgreSQL instead.", event_data={}
        )
        enqueue_distillation(session, thread, delay_seconds=0)

    class Contextual:
        def extract_context(self, text, heartbeat, context):
            assert "Fabricated" not in json.dumps(context)
            if text != "Use PostgreSQL instead.":
                return []
            assert context["recent_user_statements"][0]["text"].startswith("We are discussing")
            target = next(m for m in context["memories"] if m["uri"] == node["uri"])
            return [
                {
                    "key": target["key"],
                    "kind": target["kind"],
                    "target_uri": target["uri"],
                    "content": "Use PostgreSQL for the project database.",
                    "quote": text,
                }
            ]

    while run_once(sessions, extractor=Contextual()):
        pass
    candidate = client.get(f"/api/projects/{pid}/memory/candidates").json()[0]
    assert candidate["action"] == "UPDATE" and candidate["expected_revision"] == 1
    with sessions() as session:
        quotes = [session.get(MemoryEvidence, eid).quote for eid in candidate["evidence_ids"]]
        assert (
            "Use PostgreSQL instead." in quotes
            and "We are discussing the project database." in quotes
        )
        assert "Use SQLite." not in quotes
    client.post(f"/api/memory/candidates/{candidate['id']}/apply")
    drain(sessions)
    assert client.get(f"/api/memory/{node['id']}").json()["revision"] == 2


def test_context_changed_during_model_call_does_not_commit(backend):
    _, sessions = backend
    pid, _, node = accepted_fact(backend)
    client = backend[0]
    tid = client.post("/api/threads", json={"project_id": pid}).json()["id"]
    with sessions.begin() as session:
        thread = session.get(Thread, tid)
        append_message(
            session, thread, role="user", content="Use PostgreSQL instead.", event_data={}
        )
        job = enqueue_distillation(session, thread, delay_seconds=0)

    class Racing:
        def extract_context(self, text, heartbeat, context):
            MemoryService(sessions).forget(node["uri"])
            return [{"key": "storage", "kind": "episodic", "content": text, "quote": text}]

    jid, token = claim(sessions)
    assert jid == job.id
    assert not process(sessions, jid, token, extractor=Racing())
    with sessions() as session:
        assert session.get(Thread, tid).memory_cursor == 0
        assert not session.scalar(
            select(MemoryCandidate.id).where(MemoryCandidate.content == "Use PostgreSQL instead.")
        )


def test_memory_change_tool_requires_approval_and_keeps_candidate_pending(backend):
    client, sessions, pid, _, rid, runtime, model, _ = setup_run(
        backend, [], tools=["change_memory"]
    )
    # The current user statement is already an exact, auditable source.
    with sessions() as session:
        source = session.scalar(
            select(ContextNode).where(ContextNode.project_id == pid, ContextNode.kind == "session")
        )
        evidence = session.scalar(
            select(MemoryEvidence.id).where(MemoryEvidence.node_id == source.id)
        )
    from waku.domain.contracts import MemoryNode
    from waku.memory.port import MemoryWrite

    uri = f"si://users/default/projects/{pid}/memories/storage.md"
    runtime.memory.write(
        MemoryWrite(
            MemoryNode(uri, "semantic", "Storage", content="Use SQLite.", project_id=pid), "initial"
        )
    )
    model.responses = [
        ModelResponse(
            tool_calls=[
                {
                    "id": "change",
                    "name": "change_memory",
                    "arguments": {
                        "uri": uri,
                        "action": "UPDATE",
                        "expected_revision": 1,
                        "title": "Storage",
                        "kind": "semantic",
                        "content": "Use pgvector",
                        "evidence_ids": [evidence],
                    },
                }
            ]
        ),
        ModelResponse("Correction pending review"),
    ]
    run_job(sessions, runtime)
    paused = client.get(f"/api/runs/{rid}").json()
    assert paused["status"] == "waiting_approval"
    assert not client.get(f"/api/projects/{pid}/memory/candidates").json()
    client.post(
        f"/api/operations/{paused['operations'][0]['id']}/approval", json={"approved": True}
    )
    run_job(sessions, runtime)
    candidate = client.get(f"/api/projects/{pid}/memory/candidates").json()[0]
    assert candidate["status"] == "pending" and candidate["job_id"] is None
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "succeeded"


def test_forget_approval_fences_changed_revision_and_retries_idempotently(backend):
    from waku.domain.contracts import RunContext
    from waku.tools.executor import project_executor

    client, sessions = backend
    pid, leaf, _ = imported(backend, "Source text.")
    memory = MemoryService(sessions)
    executor = project_executor(memory, sessions)
    args = {"uri": leaf["uri"], "reason": "user explicitly asked", "expected_revision": 99}
    context = RunContext(
        run_id="test", user_id="default", project_id=pid, session_id="test", model="test"
    )
    result = executor.execute("forget_memory", args, context, "forget-1")
    assert result.status == "failed"
    assert client.get(f"/api/memory/{leaf['id']}").json()["content"] == "Source text."
    args["expected_revision"] = 1
    assert executor.execute("forget_memory", args, context, "forget-1").status == "succeeded"
    assert executor.execute("forget_memory", args, context, "forget-1").status == "succeeded"
    assert client.get(f"/api/memory/{leaf['id']}").json()["content"] == ""


def test_new_turn_omits_stale_assistant_context_and_demo_cites_only_evidence(backend):
    from waku.providers.adapters import DemoProvider
    from waku.providers.contracts import ModelRef, ModelRequest
    from waku.runtime.runner import SYSTEM

    client, sessions, pid, tid, _, runtime, _, _ = setup_run(
        backend, [ModelResponse("Stale answer")]
    )
    from evals.deterministic.test_copilot_backend import submit

    submit(client, pid)
    assert run_once(sessions)
    run_job(sessions, runtime)
    submit(
        client, pid, key="updated", docs=[{"path": "docs/design.md", "content": "Updated design"}]
    )
    assert run_once(sessions)
    response = client.post(
        f"/api/threads/{tid}/messages", json={"content": "What changed?", "idempotency_key": "next"}
    )
    assert response.status_code == 202
    from waku.storage.models import Run

    with sessions() as session:
        state = session.get(Run, response.json()["id"]).context
        assert "Stale answer" not in json.dumps(state["messages"])
    text = (
        DemoProvider()
        .complete(
            ModelRef("demo", "evidence-demo"),
            ModelRequest(system=SYSTEM + "\nEvidence:\n", messages=[]),
        )
        .text
    )
    assert "No project evidence" in text and "[si://...]" not in text
