"""Long tasks, exact constraints, ledger recovery and scoped result re-reading."""

import json
from dataclasses import asdict

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from sqlalchemy import select

from evals.deterministic.test_copilot_backend import backend as backend_fixture
from evals.deterministic.test_copilot_runtime import Scripted, run_job, setup_run
from waku.domain.contracts import RunContext
from waku.providers.contracts import ModelCapabilities, ModelRequest, ModelResponse, Usage
from waku.runtime.budget import reserve, resume_reservations, settle, totals
from waku.runtime.compaction import (
    compile_working,
    cut_points,
    validate_summary,
)
from waku.runtime.context_store import latest_summary
from waku.runtime.runner import Runtime
from waku.storage.models import ContextNode, ContextSummary, Message, Run, Thread, ToolOperation
from waku.tools.executor import Arguments, Executor, ToolSpec, project_executor

backend = backend_fixture


def test_budget_releases_only_confirmed_usage_and_settles_once():
    state = {"budget": {"max_tokens": 12000}, "charged_tokens": 0}
    aid = reserve(state, 9000, "llm")
    assert settle(state, aid, Usage(800, 200, 1000))
    assert not settle(state, aid, Usage(800, 200, 1000))
    assert state["charged_tokens"] == state["usage_tokens"] == 1000
    second = reserve(state, 5000, "summary")
    resume_reservations(state)
    assert state["budget_ledger"][1]["status"] == "uncertain"
    assert not settle(state, second, Usage())
    assert totals(state)["unsettled_tokens"] == 5000
    third = reserve(state, 1000, "llm")
    assert not settle(state, third, Usage(3000, 100, 100))
    assert state["charged_tokens"] == 7000
    with pytest.raises(ValueError, match="token_budget"):
        reserve(state, 6000, "llm")
    assert totals({"charged_tokens": 2000, "usage_tokens": 100})["unsettled_tokens"] == 2000


def test_parallel_tool_batch_cannot_be_split():
    messages = [
        {"role": "user", "content": "Keep API compatible"},
        {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
        {"role": "tool", "tool_call_id": "b", "content": "second"},
        {"role": "assistant", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "tool_call_id": "c", "content": "latest"},
    ]
    assert cut_points(messages, 0) == [1, 4]
    state = {"messages": messages, "query": "Keep API compatible", "compaction_cursor": 4,
             "working_summary": {"body": {"goals": ["Continue"]}}}
    req = compile_working(ModelRequest([], system="Fixed policy"), state, [])
    assert req.system == "Fixed policy"
    assert [m["tool_call_id"] for m in req.messages if m["role"] == "tool"] == ["c"]
    assert any(m["content"] == "Keep API compatible" for m in req.messages if "content" in m)


def test_summary_quotes_are_exact_and_previous_constraints_cannot_disappear():
    old = {"message_id": "m1", "quote": "不要改变公开接口"}
    payload = {"previous_summary": {"constraints": [old]}, "transcript": [
        {"role": "user", "message_id": "m2", "content": "只修改测试，不安装依赖"}
    ]}
    body = {"goals": [], "constraints": [], "decisions": [], "progress": [], "open_questions": [], "next_steps": []}
    assert validate_summary(json.dumps(body), payload)["constraints"] == [old]
    body["constraints"] = [{"message_id": "m2", "quote": "允许安装依赖"}]
    with pytest.raises(ValueError):
        validate_summary(json.dumps(body), payload)


def add_history(sessions, rid, count=20):
    from waku.runtime.compaction import fingerprint
    from waku.workers.distillation import append_message

    with sessions.begin() as session:
        run = session.get(Run, rid)
        thread = session.get(Thread, run.thread_id)
        state = dict(run.context)
        # Insert real earlier messages without moving the current request ahead of them.
        current = session.get(Message, state["message_refs"][-1]["id"])
        current.sequence = count + 1
        session.flush()
        thread.event_sequence = 0
        history, refs = [], []
        for i in range(count):
            role = "user" if i % 2 == 0 else "assistant"
            content = ("不要改变公开接口。" if i == 0 else "") + f"记录 {i} " + "进展细节 " * 100
            row = append_message(session, thread, role=role, content=content, event_data={})
            history.append({"role": role, "content": content})
            refs.append({"id": row.id, "sequence": row.sequence, "hash": fingerprint(content)})
        state["messages"] = [*history, *state["messages"]]
        state["message_refs"][-1]["sequence"] = count + 1
        state["message_refs"] = [*refs, *state["message_refs"]]
        thread.event_sequence = count + 1
        run.context = state


def test_semantic_compaction_survives_restart_and_preserves_raw_transcript(backend):
    client, sessions, _, tid, rid, runtime, model, _ = setup_run(
        backend, [ModelResponse("Finished", usage=Usage(1000, 100, 1100))],
        tools=[], max_tokens=256000,
    )
    add_history(sessions, rid)
    run_job(sessions, runtime)
    snapshot = client.get(f"/api/runs/{rid}").json()
    assert snapshot["status"] == "succeeded", snapshot
    assert model.summary_requests and len(model.requests) == 1
    request = model.requests[0]
    assert "不要改变公开接口" in str(request.messages)
    assert "不要改变公开接口" not in request.system
    inspection = client.get(f"/api/runs/{rid}/context").json()
    assert inspection["compaction_cursor"] > 0
    assert inspection["canonical_message_count"] == 22
    with sessions() as session:
        raw = session.get(Run, rid).context["messages"]
        assert raw[0]["content"].startswith("不要改变公开接口")
        assert session.scalar(select(ContextSummary).where(ContextSummary.run_id == rid))
    response = client.post(f"/api/threads/{tid}/messages", json={
        "content": "继续，保留前面的约束", "idempotency_key": "next", "tools": [], "max_tokens": 256000,
    })
    next_rid = response.json()["id"]
    fresh_model = Scripted([ModelResponse("Continued")])
    fresh = Runtime(sessions, runtime.memory, runtime.executor, provider_factory=lambda *a, **k: fresh_model)
    run_job(sessions, fresh)
    assert client.get(f"/api/runs/{next_rid}").json()["status"] == "succeeded"
    assert "不要改变公开接口" in str(fresh_model.requests[0].messages)


def test_more_than_twelve_messages_are_loaded_without_silent_trimming(backend):
    client, sessions, _, tid, rid, runtime, _model, _ = setup_run(backend, [ModelResponse("ok")], tools=[])
    add_history(sessions, rid, 14)
    run_job(sessions, runtime)
    response = client.post(f"/api/threads/{tid}/messages", json={
        "content": "continue", "tools": [], "idempotency_key": "second", "max_tokens": 256000,
    })
    with sessions() as session:
        state = session.get(Run, response.json()["id"]).context
        assert len(state["messages"]) > 12 or state["working_summary"]


def test_summary_failure_keeps_history_and_does_not_start_business_call(backend):
    client, sessions, _, _, rid, runtime, model, _ = setup_run(backend, [], tools=[], max_tokens=256000)
    add_history(sessions, rid)
    model.complete = lambda *_: ModelResponse("invalid json", usage=Usage(50, 10, 60))
    run_job(sessions, runtime)
    snapshot = client.get(f"/api/runs/{rid}").json()
    assert snapshot["error_code"] == "compaction_failed"
    assert snapshot["usage_tokens"] == 120
    with sessions() as session:
        assert len(session.get(Run, rid).context["messages"]) == 21
        assert not session.scalar(select(ContextSummary).where(ContextSummary.run_id == rid))


def test_scoped_result_reader_keeps_full_output_and_rejects_cross_project(backend):
    from evals.deterministic.test_copilot_backend import project

    tools = Executor([ToolSpec("read_memory", "Read", Arguments,
                             lambda *_: {"text": "完整日志" * 4000})])
    client, sessions, pid, tid, rid, runtime, _, _ = setup_run(backend, [
        ModelResponse(tool_calls=[{"id": "r", "name": "read_memory", "arguments": {}}]),
        ModelResponse("done"),
    ], executor=tools, tools=["read_memory"])
    run_job(sessions, runtime)
    with sessions() as session:
        op = session.scalar(select(ToolOperation).where(ToolOperation.run_id == rid))
        oid = op.id
        assert op.result["data"]["text"] == "完整日志" * 4000
        state = session.get(Run, rid).context
        projection = json.loads(next(m["content"] for m in state["messages"] if m["role"] == "tool"))
        assert projection["context_projected"] and projection["status"] == "succeeded"
    executor = project_executor(runtime.memory, sessions)
    args = executor.validate("read_operation_result", {"operation_id": oid, "limit": 300}, ["read_operation_result"])
    ctx = RunContext(run_id=rid, user_id="default", project_id=pid, session_id=tid, model="demo")
    result = executor.execute("read_operation_result", args, ctx, "reader")
    assert result.status == "succeeded" and result.data["next_offset"] == 300, result
    assert "完整日志" in result.data["content"]
    other = project(client, "Other")
    other_ctx = RunContext(**{**asdict(ctx), "project_id": other})
    assert executor.execute("read_operation_result", args, other_ctx, "reader2").status == "failed"


def test_source_forget_redacts_summary_and_source_revision_invalidates_cache(backend):
    from waku.memory.lifecycle import forget_node

    client, sessions, pid, tid, rid, runtime, _, _ = setup_run(backend, [ModelResponse("done")], tools=[], max_tokens=256000)
    add_history(sessions, rid)
    run_job(sessions, runtime)
    with sessions.begin() as session:
        run = session.get(Run, rid)
        source = session.get(ContextNode, run.context["source_nodes"][0]["id"])
        source.revision += 1
        assert latest_summary(session, session.get(Thread, tid)) is None
        source.revision -= 1
        forget_node(session, source, "test user request")
    assert not client.get(f"/api/runs/{rid}/context").json()["summary"]
    with sessions() as session:
        rows = list(session.scalars(select(ContextSummary).where(ContextSummary.project_id == pid)))
        assert rows and all(r.status == "redacted" and r.body == {} for r in rows)


def test_business_call_settlement_releases_headroom_for_following_iterations(backend):
    client, sessions, _, _, rid, runtime, _, _ = setup_run(backend, [
        ModelResponse(tool_calls=[{"id": "x", "name": "search_memory", "arguments": {"query": "x"}}], usage=Usage(100, 20, 120)),
        ModelResponse("done", usage=Usage(200, 30, 230)),
    ], max_tokens=8000)
    run_job(sessions, runtime)
    result = client.get(f"/api/runs/{rid}").json()
    assert result["status"] == "succeeded", result
    assert result["charged_tokens"] == result["usage_tokens"] == 350
    assert result["budget_usage"]["unsettled_tokens"] == 0


def test_forget_during_summarization_cannot_commit_or_restore_text(backend):
    from waku.memory.lifecycle import forget_node
    from waku.runtime.compaction import SUMMARY_SYSTEM

    client, sessions, _, _, rid, runtime, model, _ = setup_run(backend, [], tools=[], max_tokens=256000)
    add_history(sessions, rid)
    complete = model.complete

    def forget_and_complete(ref, request):
        assert request.system == SUMMARY_SYSTEM and not request.tools
        with sessions.begin() as session:
            source = session.get(ContextNode, session.get(Run, rid).context["source_nodes"][0]["id"])
            forget_node(session, source, "user requested deletion")
        return complete(ref, request)

    model.complete = forget_and_complete
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "cancelled"
    with sessions() as session:
        assert session.get(Run, rid).context["messages"] == []
        assert not session.scalar(select(ContextSummary).where(ContextSummary.run_id == rid))


def test_failed_turn_statement_forget_redacts_inherited_history_and_summary(backend):
    from waku.memory.lifecycle import forget_node

    client, sessions, _, tid, rid, runtime, _, _ = setup_run(backend, [ValueError("failed")], tools=[])
    run_job(sessions, runtime)
    with sessions() as session:
        source_id = session.get(Run, rid).context["source_nodes"][0]["id"]
    second = client.post(f"/api/threads/{tid}/messages", json={
        "content": "Continue from earlier", "idempotency_key": "next", "tools": [],
    }).json()["id"]
    with sessions.begin() as session:
        source = session.get(ContextNode, source_id)
        # Historical rows from before source metadata was attached still have message locators.
        run = session.get(Run, second)
        state = dict(run.context)
        state["source_nodes"] = [r for r in state["source_nodes"] if r["id"] != source_id]
        state["evidence_ids"] = []
        run.context = state
        forget_node(session, source, "forget failed request")
    with sessions() as session:
        assert session.get(Run, second).status == "cancelled"
        assert session.get(Run, second).context["messages"] == []


def test_compaction_approval_and_resume_do_not_replay_or_expand_durable_results(backend):
    calls = []

    def write(_args, _context, oid):
        calls.append(oid)
        return {"content": "完整输出" * 4000}

    tools = Executor([ToolSpec("propose_memory", "Write", Arguments, write, writes=True)])
    client, sessions, _, _, rid, runtime, model, _ = setup_run(backend, [
        ModelResponse(tool_calls=[{"id": "write", "name": "propose_memory", "arguments": {}}]),
        ModelResponse("done"),
    ], executor=tools, tools=["propose_memory"], max_tokens=256000)
    add_history(sessions, rid)
    run_job(sessions, runtime)
    paused = client.get(f"/api/runs/{rid}").json()
    assert paused["status"] == "waiting_approval" and model.summary_requests and not calls
    oid = paused["operations"][0]["id"]
    assert client.get(f"/api/runs/{rid}/context").json()["working_state"]["operations"][0]["status"] == "pending"
    client.post(f"/api/operations/{oid}/approval", json={"approved": True})
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{rid}/context").json()["working_state"]["operations"][0]["status"] == "succeeded"
    restarted = Runtime(sessions, runtime.memory, tools, provider_factory=lambda *a, **k: model)
    run_job(sessions, restarted)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "succeeded"
    assert calls == [oid]
    outcome = json.loads(next(m["content"] for m in model.requests[-1].messages if m["role"] == "tool"))
    assert outcome["context_projected"] and outcome["operation_id"] == oid


def test_result_page_fits_projection_for_multibyte_text(backend):
    from waku.runtime.results import project_result

    _client, sessions, pid, tid, rid, runtime, _, _ = setup_run(backend, [ModelResponse("done")], tools=[])
    run_job(sessions, runtime)
    with sessions.begin() as session:
        op = ToolOperation(run_id=rid, project_id=pid, call_id="large", tool="read_memory",
                           arguments={}, status="succeeded", result={"content": "中文日志" * 4000})
        session.add(op)
        session.flush()
        oid = op.id
    executor = project_executor(runtime.memory, sessions)
    args = executor.validate("read_operation_result", {"operation_id": oid, "limit": 4000}, ["read_operation_result"])
    context = RunContext(run_id=rid, user_id="default", project_id=pid, session_id=tid, model="demo")
    result = executor.execute("read_operation_result", args, context, "page").to_dict()
    assert result["status"] == "succeeded"
    assert not project_result(result).get("context_projected")
    assert len(result["data"]["content"].encode()) <= 2500
    assert result["data"]["next_offset"] < 4000


def test_oversized_single_user_message_is_preserved_and_stops_before_provider(backend):
    client, sessions, _, _, rid, runtime, model, _ = setup_run(
        backend, [], tools=[], max_tokens=256000, content="中文" * 1800,
    )
    model.capabilities = lambda _: ModelCapabilities(context_window=8192)
    with sessions.begin() as session:
        run = session.get(Run, rid)
        state = dict(run.context)
        state["retrieval_cache"] = {"hits": []}
        run.context = state
    run_job(sessions, runtime)
    assert client.get(f"/api/runs/{rid}").json()["error_code"] == "context_budget"
    assert not model.requests and not model.summary_requests
