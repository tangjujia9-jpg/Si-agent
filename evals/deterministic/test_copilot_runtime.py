"""Run actual DB/API/Worker paths with scripted providers, without model keys."""

import json
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from evals.deterministic.test_copilot_backend import (
    backend as backend_fixture,
)
from evals.deterministic.test_copilot_backend import (
    project,
    submit,
)
from waku.memory.postgres import PostgresMemory
from waku.providers.contracts import ModelCapabilities, ModelResponse
from waku.runtime.runner import Runtime
from waku.storage.models import Job, Run, now
from waku.tools.executor import Arguments, Executor, ToolSpec, project_executor
from waku.workers.runner import claim, process, run_once

backend = backend_fixture


class Scripted:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def capabilities(self, model):
        return ModelCapabilities(tool_calling=True, context_window=32768)

    def complete(self, model, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item


def setup_run(backend, responses, *, executor=None, **kwargs):
    client, sessions = backend
    pid = project(client)
    tid = client.post("/api/threads", json={"project_id": pid}).json()["id"]
    model = Scripted(responses)
    memory = PostgresMemory(sessions)
    executor = executor or project_executor(memory, sessions)
    runtime = Runtime(sessions, memory, executor, provider_factory=lambda *a, **k: model)
    payload = {"content": "Does this project use pgvector?", "idempotency_key": "turn-1", **kwargs}
    response = client.post(f"/api/threads/{tid}/messages", json=payload)
    assert response.status_code == 202, response.text
    return client, sessions, pid, tid, response.json()["id"], runtime, model, payload


def run_job(sessions, runtime):
    for _ in range(12):
        claimed = claim(sessions)
        assert claimed
        with sessions() as session:
            kind = session.get(Job, claimed[0]).kind
        assert process(sessions, *claimed, runtime=runtime)
        if kind in {"run", "tool_outbox"}:
            return
    raise AssertionError("runtime job not reached")


def test_project_evidence_reaches_model_and_run_is_idempotent(backend):
    client, sessions, pid, tid, rid, runtime, model, payload = setup_run(
        backend, [ModelResponse("Use evidence")]
    )
    # Import first; worker priority runs ingestion before the queued conversation.
    submit(client, pid)
    assert run_once(sessions)
    run_job(sessions, runtime)
    assert "Use pgvector" in model.requests[0].system
    assert f"si://users/default/projects/{pid}" in model.requests[0].system
    snapshot = client.get(f"/api/runs/{rid}").json()
    assert snapshot["status"] == "succeeded"
    assert snapshot["reply"] == "Use evidence"
    again = client.post(f"/api/threads/{tid}/messages", json=payload)
    assert again.json()["id"] == rid
    assert (
        client.post(
            f"/api/threads/{tid}/messages", json={**payload, "content": "changed"}
        ).status_code
        == 409
    )
    records = client.get(f"/api/runs/{rid}/events").json()
    spans = [e["data"] for e in records if e["kind"] == "span"]
    root = next(e for e in spans if e["name"] == "run")
    assert root["parent_id"] is None
    assert all(s["parent_id"] in {r["span_id"] for r in spans} for s in spans if s["name"] != "run")
    assert any(s["name"] == "retrieval_l2_leaves" for s in spans)
    assert all(s["duration_ms"] >= 0 for s in spans)
    with client.stream("GET", f"/api/runs/{rid}/events/stream") as stream:
        body = "".join(stream.iter_text())
    assert "event: run_end" in body
    assert "event: snapshot" in body


def test_tool_arguments_and_dynamic_exposure(backend):
    responses = [
        ModelResponse(
            tool_calls=[{"id": "call1", "name": "read_memory", "arguments": {"uri": 123}}]
        ),
        ModelResponse("Invalid tool was handled"),
    ]
    client, sessions, _, _, rid, runtime, model, _ = setup_run(backend, responses)
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "succeeded"
    observed = json.loads(model.requests[1].messages[-1]["content"])
    assert observed["error_code"] == "invalid_arguments"
    assert {t["name"] for t in model.requests[0].tools} == {"read_memory", "search_memory"}


def test_write_requires_approval_outbox_then_resumes_without_replay(backend):
    calls = []

    def action(args, context, oid):
        calls.append(oid)
        return {"resource_id": "artifact-1"}

    tools = Executor([ToolSpec("propose_memory", "Write", Arguments, action, writes=True)])
    client, sessions, _, _, rid, runtime, model, _ = setup_run(
        backend,
        [
            ModelResponse(tool_calls=[{"id": "write", "name": "propose_memory", "arguments": {}}]),
            ModelResponse("done"),
        ],
        executor=tools,
        tools=["propose_memory"],
    )
    run_job(sessions, runtime)
    paused = client.get(f"/api/runs/{rid}").json()
    assert paused["status"] == "waiting_approval" and not calls
    oid = paused["operations"][0]["id"]
    assert (
        client.post(f"/api/operations/{oid}/approval", json={"approved": True}).status_code == 200
    )
    assert (
        client.post(f"/api/operations/{oid}/approval", json={"approved": True}).status_code == 200
    )
    run_job(sessions, runtime)
    run_job(sessions, runtime)
    assert calls == [oid]
    assert client.get(f"/api/runs/{rid}").json()["status"] == "succeeded"
    assert len(model.requests) == 2


def test_unknown_external_result_stops_run_without_automatic_retry(backend):
    calls = []

    def action(args, context, oid):
        calls.append(oid)
        raise TimeoutError("secret payload sk-PRIVATE")

    tools = Executor(
        [
            ToolSpec(
                "propose_memory", "External write", Arguments, action, writes=True, idempotent=False
            )
        ]
    )
    client, sessions, _, _, rid, runtime, _, _ = setup_run(
        backend,
        [ModelResponse(tool_calls=[{"id": "write", "name": "propose_memory", "arguments": {}}])],
        executor=tools,
        tools=["propose_memory"],
    )
    run_job(sessions, runtime)
    oid = client.get(f"/api/runs/{rid}").json()["operations"][0]["id"]
    client.post(f"/api/operations/{oid}/approval", json={"approved": True})
    run_job(sessions, runtime)
    run_job(sessions, runtime)
    snapshot = client.get(f"/api/runs/{rid}").json()
    assert snapshot["status"] == "unknown" and len(calls) == 1
    assert "PRIVATE" not in str(snapshot)
    assert "PRIVATE" not in client.get(f"/api/runs/{rid}/events").text


@pytest.mark.parametrize("limit", ["max_iterations", "max_tool_calls", "max_tokens"])
def test_budgets_stop_run_before_extra_work(backend, limit):
    options = (
        {"max_iterations": 1}
        if limit == "max_iterations"
        else {limit: 0 if limit == "max_tool_calls" else 1000}
    )
    responses = [
        ModelResponse(
            tool_calls=[{"id": "call", "name": "search_memory", "arguments": {"query": "storage"}}]
        )
    ]
    client, sessions, _, _, rid, runtime, model, _ = setup_run(backend, responses, **options)
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "failed"
    assert len(model.requests) <= 1


def test_cancel_and_cross_user_scope(backend):
    client, sessions, pid, tid, rid, runtime, model, _ = setup_run(
        backend, [ModelResponse("should not run")]
    )
    client.post(f"/api/runs/{rid}/cancel")
    run_job(sessions, runtime)
    assert not model.requests
    with sessions.begin() as session:
        from waku.storage.models import Project

        session.get(Project, pid).user_id = "hidden"
    for route in (f"/api/runs/{rid}", f"/api/runs/{rid}/events", f"/api/runs/{rid}/events/stream"):
        assert client.get(route).status_code == 404
    assert (
        client.post(
            f"/api/threads/{tid}/messages", json={"content": "x", "idempotency_key": "x"}
        ).status_code
        == 404
    )


def test_expired_deadline_never_calls_model(backend):
    client, sessions, _, _, rid, runtime, model, _ = setup_run(backend, [ModelResponse("late")])
    with sessions.begin() as session:
        run = session.get(Run, rid)
        run.context = {**run.context, "deadline": (now() - timedelta(seconds=1)).isoformat()}
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "failed"
    assert not model.requests


def test_restart_reuses_committed_tool_result(backend):
    executions = []
    tools = Executor(
        [
            ToolSpec(
                "read_memory",
                "Read",
                Arguments,
                lambda *a: executions.append(1) or {"value": "saved"},
            )
        ]
    )
    client, sessions, _, _, rid, runtime, model, _ = setup_run(
        backend,
        [
            ModelResponse(tool_calls=[{"id": "read", "name": "read_memory", "arguments": {}}]),
            ModelResponse("resumed"),
        ],
        executor=tools,
        tools=["read_memory"],
    )
    save = runtime._save

    def interrupted(*args, **kwargs):
        if args[3]["tool_count"] == 1 and kwargs.get("event") is None:
            raise KeyboardInterrupt()
        return save(*args, **kwargs)

    runtime._save = interrupted
    jid, token = claim(sessions)
    with pytest.raises(KeyboardInterrupt):
        runtime.process(jid, token)
    with sessions.begin() as session:
        session.get(Job, jid).lease_until = now() - timedelta(seconds=1)
    restarted = Runtime(sessions, runtime.memory, tools, provider_factory=lambda *a, **k: model)
    run_job(sessions, restarted)
    assert executions == [1]
    assert client.get(f"/api/runs/{rid}").json()["reply"] == "resumed"


def test_interrupted_non_idempotent_write_is_not_dispatched_again(backend):
    from waku.storage.models import ToolOperation

    dispatched = []
    tools = Executor(
        [
            ToolSpec(
                "propose_memory",
                "Write",
                Arguments,
                lambda *a: dispatched.append(1),
                writes=True,
                idempotent=False,
            )
        ]
    )
    client, sessions, _, _, rid, runtime, _, _ = setup_run(
        backend,
        [
            ModelResponse(tool_calls=[{"id": "write", "name": "propose_memory", "arguments": {}}]),
        ],
        executor=tools,
        tools=["propose_memory"],
    )
    run_job(sessions, runtime)
    oid = client.get(f"/api/runs/{rid}").json()["operations"][0]["id"]
    client.post(f"/api/operations/{oid}/approval", json={"approved": True})
    with sessions.begin() as session:
        session.get(ToolOperation, oid).status = "running"
    run_job(sessions, runtime)
    run_job(sessions, runtime)
    assert not dispatched
    assert client.get(f"/api/runs/{rid}").json()["status"] == "unknown"


def test_rejection_resumes_and_string_approval_is_invalid(backend):
    dispatched = []
    tools = Executor(
        [
            ToolSpec(
                "propose_memory", "Write", Arguments, lambda *a: dispatched.append(1), writes=True
            )
        ]
    )
    client, sessions, _, _, rid, runtime, model, _ = setup_run(
        backend,
        [
            ModelResponse(tool_calls=[{"id": "write", "name": "propose_memory", "arguments": {}}]),
            ModelResponse("Rejected"),
        ],
        executor=tools,
        tools=["propose_memory"],
    )
    run_job(sessions, runtime)
    oid = client.get(f"/api/runs/{rid}").json()["operations"][0]["id"]
    assert (
        client.post(f"/api/operations/{oid}/approval", json={"approved": "false"}).status_code
        == 422
    )
    client.post(f"/api/operations/{oid}/approval", json={"approved": False})
    run_job(sessions, runtime)
    assert not dispatched
    result = json.loads(model.requests[-1].messages[-1]["content"])
    assert result["error_code"] == "approval_rejected"


@pytest.mark.parametrize("transient", [True, False])
def test_provider_retries_only_transient_errors_and_reserves_budget(backend, transient):
    class ProviderError(Exception):
        status_code = 429 if transient else 401

    client, sessions, _, _, rid, runtime, model, _ = setup_run(
        backend,
        [
            ProviderError("sk-private prompt"),
            ModelResponse("ok"),
        ],
    )
    run_job(sessions, runtime)
    snap = client.get(f"/api/runs/{rid}").json()
    assert len(model.requests) == (2 if transient else 1)
    assert snap["status"] == ("succeeded" if transient else "failed")
    assert snap["charged_tokens"] > 0 and snap["usage_tokens"] == 0
    assert "private" not in str(snap)


def test_forget_during_model_call_cancels_and_redacts_history(backend):
    client, sessions, pid, tid, rid, runtime, model, _ = setup_run(backend, [])
    submit(client, pid)
    assert run_once(sessions)
    node = next(
        n for n in client.get(f"/api/projects/{pid}/context/tree").json() if n["kind"] == "resource"
    )

    def forget_then_reply(request):
        assert "Use pgvector" in request.system
        response = client.post(f"/api/memory/{node['id']}/forget", json={})
        assert response.status_code == 200
        return ModelResponse("Use pgvector")

    model.responses = [forget_then_reply]
    run_job(sessions, runtime)
    snap = client.get(f"/api/runs/{rid}").json()
    assert snap["status"] == "cancelled" and not snap["reply"]
    with sessions() as session:
        assert session.get(Run, rid).context["messages"] == []
        from sqlalchemy import select

        from waku.storage.models import Message

        assert all(
            "pgvector" not in m.content
            for m in session.scalars(select(Message).where(Message.thread_id == tid))
        )


def test_forget_redacts_inherited_context_and_blocks_resumption(backend):
    client, sessions, pid, tid, _rid, runtime, model, _ = setup_run(
        backend, [ModelResponse("Use pgvector")]
    )
    submit(client, pid)
    assert run_once(sessions)
    run_job(sessions, runtime)
    node = next(
        n for n in client.get(f"/api/projects/{pid}/context/tree").json() if n["kind"] == "resource"
    )
    second = client.post(
        f"/api/threads/{tid}/messages",
        json={"content": "Tell me more", "idempotency_key": "turn-2"},
    ).json()["id"]
    client.post(f"/api/memory/{node['id']}/forget", json={})
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{second}").json()["status"] == "cancelled"
    with sessions() as session:
        assert session.get(Run, second).context["messages"] == []
    assert len(model.requests) == 1


def test_exhausted_worker_marks_run_terminal(backend):
    client, sessions, _, _, rid, _, _, _ = setup_run(backend, [])
    jid, _ = claim(sessions)
    with sessions.begin() as session:
        job = session.get(Job, jid)
        job.attempts = job.max_attempts
        job.lease_until = now() - timedelta(seconds=1)
    assert claim(sessions) is None
    assert client.get(f"/api/runs/{rid}").json()["status"] == "failed"


def test_real_proposal_requires_approval_and_stays_pending(backend):
    from waku.storage.models import MemoryCandidate

    client, sessions, pid, _, rid, runtime, model, _ = setup_run(
        backend, [], tools=["propose_memory"]
    )
    submit(client, pid)
    assert run_once(sessions)
    node = next(
        n for n in client.get(f"/api/projects/{pid}/context/tree").json() if n["kind"] == "resource"
    )
    evidence = client.get(f"/api/memory/{node['id']}/evidence").json()
    model.responses = [
        ModelResponse(
            tool_calls=[
                {
                    "id": "proposal",
                    "name": "propose_memory",
                    "arguments": {
                        "uri": f"si://users/default/projects/{pid}/memories/storage.md",
                        "title": "Storage",
                        "content": "Use pgvector",
                        "evidence_ids": [evidence[0]["id"]],
                    },
                }
            ]
        ),
        ModelResponse("Proposed for review"),
    ]
    run_job(sessions, runtime)
    oid = client.get(f"/api/runs/{rid}").json()["operations"][0]["id"]
    client.post(f"/api/operations/{oid}/approval", json={"approved": True})
    run_job(sessions, runtime)
    run_job(sessions, runtime)
    snap = client.get(f"/api/runs/{rid}").json()
    assert snap["status"] == "succeeded"
    candidate_id = snap["operations"][0]["result"]["data"]["candidate_id"]
    with sessions() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        assert candidate.status == "pending" and candidate.job_id is None


@pytest.mark.parametrize("confidence,expected", [(0.95, "skipped"), (0.2, "pending")])
def test_retention_gate_filters_only_confident_negatives(backend, confidence, expected):
    from types import SimpleNamespace

    from sqlalchemy import select

    from waku.runtime.decisions import Decision
    from waku.storage.models import MemoryCandidate

    client, sessions = backend
    pid = project(client)
    submit(
        client, pid, docs=[{"path": "decision.md", "content": "Decision[storage]: Use pgvector"}]
    )
    runtime = SimpleNamespace(
        exporter=None,
        decisions=SimpleNamespace(
            decide=lambda purpose, state: Decision(False, 0.1, confidence, "jev-test")
        ),
    )
    assert run_once(sessions, runtime=runtime)
    for _ in range(4):
        run_once(sessions, runtime=runtime)
    with sessions() as session:
        candidates = list(
            session.scalars(select(MemoryCandidate).where(MemoryCandidate.project_id == pid))
        )
        assert len(candidates) == 1
        assert candidates[0].status == expected
        assert "jev-test" in candidates[0].reason
        assert candidates[0].job_id is None


def test_lease_change_after_cached_job_read_blocks_model_dispatch(backend):
    from sqlalchemy import event

    client, sessions, _, _, rid, runtime, model, _ = setup_run(backend, [ModelResponse("stale")])
    jid, token = claim(sessions)
    engine = sessions.kw["bind"]
    changed = []

    def reclaimed(_conn, _cursor, statement, _params, _context, _many):
        if not changed and "FROM projects" in statement:
            changed.append(True)
            with sessions.begin() as session:
                session.get(Job, jid).lease_token = "new-owner"

    event.listen(engine, "before_cursor_execute", reclaimed)
    try:
        with pytest.raises(RuntimeError, match="stale_lease"):
            runtime.process(jid, token)
    finally:
        event.remove(engine, "before_cursor_execute", reclaimed)
    assert changed and not model.requests
    assert client.get(f"/api/runs/{rid}").json()["status"] == "pending"


def test_cancel_during_write_does_not_claim_the_side_effect_was_undone(backend):
    state = {}

    def action(args, context, oid):
        state["client"].post(f"/api/runs/{context.run_id}/cancel")
        return {"resource_id": "already-created", "private": "sensitive body"}

    tools = Executor([ToolSpec("propose_memory", "Write", Arguments, action, writes=True)])
    client, sessions, _, _, rid, runtime, _, _ = setup_run(
        backend,
        [
            ModelResponse(tool_calls=[{"id": "write", "name": "propose_memory", "arguments": {}}]),
        ],
        executor=tools,
        tools=["propose_memory"],
    )
    state["client"] = client
    run_job(sessions, runtime)
    oid = client.get(f"/api/runs/{rid}").json()["operations"][0]["id"]
    client.post(f"/api/operations/{oid}/approval", json={"approved": True})
    run_job(sessions, runtime)
    snap = client.get(f"/api/runs/{rid}").json()
    assert snap["status"] == "cancelled"
    result = snap["operations"][0]["result"]
    assert result["status"] == "succeeded" and result["resource_id"] == "already-created"
    assert result["data"] == {} and "sensitive" not in str(snap)
