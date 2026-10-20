"""Workspace boundaries, streaming checkpoints and independent monitoring."""

from datetime import timedelta

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")
pytest.importorskip("pgvector")

from evals.deterministic.test_copilot_backend import backend as backend_fixture
from evals.deterministic.test_copilot_backend import project, submit
from evals.deterministic.test_copilot_runtime import run_job, setup_run
from waku.providers.contracts import ModelCapabilities, StreamEvent
from waku.runtime.runner import Runtime
from waku.runtime.telemetry import Telemetry
from waku.server.workspace import folder, manifest, resolve_file
from waku.storage.models import now
from waku.tools.executor import Arguments, ToolSpec
from waku.tools.project_files import file_tools
from waku.workers.runner import run_once

backend = backend_fixture


def test_local_workspace_is_opt_in_bounded_and_filters_sensitive_files(
    backend, tmp_path, monkeypatch
):
    client, _ = backend
    root = tmp_path / "project"
    root.mkdir()
    (root / "design.md").write_text("Original project specification", encoding="utf-8")
    (root / ".env.json").write_text("private", encoding="utf-8")
    (root / "api-token.txt").write_text("private", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "test.js").write_text("generated", encoding="utf-8")
    assert (
        client.post("/api/projects", json={"name": "Local", "root_path": str(root)}).status_code
        == 403
    )
    monkeypatch.setenv("SI_WORKSPACE_ROOTS", str(root))
    pid = client.post("/api/projects", json={"name": "Local", "root_path": str(root)}).json()["id"]
    assert [f["path"] for f in client.get(f"/api/projects/{pid}/workspace").json()["files"]] == [
        "design.md"
    ]
    assert (
        client.get(f"/api/projects/{pid}/workspace/file", params={"path": "design.md"})
        .json()["content"]
        .startswith("Original")
    )
    for path in (
        "../design.md",
        ".env.json",
        "api-token.txt",
        "node_modules/test.js",
        "C:/keys.txt",
    ):
        assert (
            client.get(f"/api/projects/{pid}/workspace/file", params={"path": path}).status_code
            == 403
        )
    assert (
        client.get("/api/workspace/directories", params={"path": str(tmp_path)}).status_code == 403
    )
    system = client.get("/api/system").json()
    assert system["database"] in {"sqlite", "postgresql"}
    assert "database_url" not in system and "password" not in str(system)


def test_workspace_refuses_symbolic_links(tmp_path, monkeypatch):
    monkeypatch.setenv("SI_WORKSPACE_ROOTS", str(tmp_path))
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "other.md").write_text("not in this project")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symbolic link privilege unavailable")
    with pytest.raises(ValueError):
        folder(root / "linked")
    with pytest.raises(ValueError):
        resolve_file(root, "linked/other.md")
    assert manifest(root)["files"] == []


def test_pending_and_conflict_suggestions_are_scoped_paginated_and_dismissible(backend):
    from waku.storage.models import MemoryCandidate

    client, sessions = backend
    pid, other = project(client), project(client, "Other")
    with sessions.begin() as session:
        for i, (scope, status) in enumerate(
            [(pid, "pending"), (pid, "conflict"), (pid, "applied"), (other, "pending")]
        ):
            session.add(
                MemoryCandidate(
                    project_id=scope,
                    uri=f"si://{i}.md",
                    kind="semantic",
                    title=str(i),
                    content="Fact",
                    action="ADD",
                    status=status,
                    idempotency_key=str(i),
                    payload_hash="fixture",
                    evidence_ids=[],
                )
            )
    path = f"/api/projects/{pid}/memory/candidates?status=pending,conflict&limit=1"
    first = client.get(path).json()[0]
    second = client.get(path + "&offset=1").json()[0]
    assert first["id"] != second["id"]
    assert {first["status"], second["status"]} == {"pending", "conflict"}
    conflict = next(c for c in (first, second) if c["status"] == "conflict")
    assert (
        client.post(f"/api/memory/candidates/{conflict['id']}/skip").json()["status"] == "skipped"
    )
    assert client.get(f"/api/projects/{other}/monitor").json()["runs"] == []
    assert client.get(f"/api/projects/{pid}/memory/candidates?status=invalid").status_code == 422


def test_file_tool_results_fit_context_and_expose_continuation(backend, tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from waku.storage.models import Project

    client, sessions = backend
    root = tmp_path / "project"
    root.mkdir()
    (root / "中文资料.md").write_text(
        ("这是一段项目架构说明。" * 12 + "\n") * 120, encoding="utf-8"
    )
    for i in range(100):
        (root / (f"{i:03d}-" + "x" * 100 + ".md")).write_text("project", encoding="utf-8")
    monkeypatch.setenv("SI_WORKSPACE_ROOTS", str(root))
    pid = project(client)
    with sessions.begin() as s:
        s.get(Project, pid).root_path = str(root)
    tools = {t.name: t for t in file_tools(sessions, Arguments, ToolSpec)}
    ctx = SimpleNamespace(user_id="default", project_id=pid)

    def call(name, values):
        t = tools[name]
        result = t.execute(t.arguments.model_validate(values).model_dump(), ctx, "fixture")
        assert len(json.dumps(result, ensure_ascii=False).encode()) < 5000
        return result

    first = call("read_project_file", {"path": "中文资料.md"})
    assert first["truncated"] and first["next_line"] > 1
    second = call("read_project_file", {"path": "中文资料.md", "start_line": first["next_line"]})
    assert second["content"].startswith(str(first["next_line"]) + ":")
    listing = call("list_project_files", {})
    assert listing["has_more"] and listing["next_offset"] == len(listing["files"])
    assert (
        call("list_project_files", {"offset": listing["next_offset"]})["files"][0]
        != listing["files"][0]
    )
    call("search_project_files", {"query": "项目"})


class Streaming:
    def __init__(self, callback=None, *, fail=False, tool=False):
        self.requests = []
        self.callback, self.fail, self.tool = callback, fail, tool

    def capabilities(self, model):
        return ModelCapabilities(streaming=True, tool_calling=True, context_window=32768)

    def stream(self, model, request):
        self.requests.append(request)
        if self.tool and len(self.requests) == 1:
            yield StreamEvent(
                "tool_call", {"id": "files", "name": "list_project_files", "arguments": {}}
            )
        else:
            yield StreamEvent("text_delta", {"text": "A" * 128})
            if self.callback:
                self.callback()
            if self.fail:
                raise OSError("disconnected")
            yield StreamEvent("text_delta", {"text": " end"})
        yield StreamEvent(
            "done",
            {"stop_reason": "tool_calls" if self.tool and len(self.requests) == 1 else "stop"},
        )


def test_model_deltas_are_durable_before_completion_and_tool_loop_resumes(backend):
    client, sessions, pid, tid, rid, runtime, _, _ = setup_run(
        backend, [], tools=["list_project_files"]
    )
    observations = []

    def inspect_midstream():
        snapshot = client.get(f"/api/runs/{rid}").json()
        events = client.get(f"/api/runs/{rid}/events").json()
        observations.append(
            snapshot["status"] == "running" and any(e["kind"] == "llm_delta" for e in events)
        )
        assert not any(
            m["role"] == "assistant" for m in client.get(f"/api/threads/{tid}/messages").json()
        )

    provider = Streaming(inspect_midstream, tool=True)
    runtime.provider_factory = lambda *a, **k: provider
    run_job(sessions, runtime)
    snapshot = client.get(f"/api/runs/{rid}").json()
    assert observations == [True]
    assert snapshot["status"] == "succeeded" and snapshot["reply"].endswith(" end")
    assert len(provider.requests) == 2
    assistant = next(m for m in provider.requests[1].messages if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["name"] == "list_project_files"
    assert assistant["tool_calls"][0]["id"] == provider.requests[1].messages[-1]["tool_call_id"]
    assert snapshot["operations"][0]["status"] == "succeeded"
    assert client.get(f"/api/projects/{pid}/monitor").json()["runs"][0]["id"] == rid
    other = project(client, "Other")
    assert client.get(f"/api/projects/{other}/monitor").json()["runs"] == []


def test_interrupted_stream_does_not_retry_or_commit_partial_answer(backend):
    client, sessions, _, tid, rid, runtime, _, _ = setup_run(backend, [])
    provider = Streaming(fail=True)
    runtime.provider_factory = lambda *a, **k: provider
    run_job(sessions, runtime)
    snapshot = client.get(f"/api/runs/{rid}").json()
    assert snapshot["status"] == "failed"
    assert not snapshot["reply"]
    assert len(provider.requests) == 1
    assert not any(
        m["role"] == "assistant" for m in client.get(f"/api/threads/{tid}/messages").json()
    )


def test_forget_redacts_already_streamed_output(backend):
    client, sessions, pid, _, rid, runtime, _, _ = setup_run(backend, [])
    submit(client, pid)
    run_once(sessions)
    node = next(
        n for n in client.get(f"/api/projects/{pid}/context/tree").json() if n["kind"] == "resource"
    )
    provider = Streaming(lambda: client.post(f"/api/memory/{node['id']}/forget", json={}))
    runtime.provider_factory = lambda *a, **k: provider
    run_job(sessions, runtime)
    deltas = [e for e in client.get(f"/api/runs/{rid}/events").json() if e["kind"] == "llm_delta"]
    assert deltas and all(e["data"] == {"redacted": True} for e in deltas)
    assert client.get(f"/api/runs/{rid}").json()["status"] == "cancelled"


def test_duration_uses_high_resolution_clock(monkeypatch):
    values = iter([10.0, 10.0004])
    monkeypatch.setattr("waku.runtime.telemetry.time.monotonic", lambda: 0)
    monkeypatch.setattr("waku.runtime.telemetry.time.perf_counter", lambda: next(values))
    records = []
    trace = Telemetry(records.append)
    with trace.span("retrieval"):
        pass
    assert records[-1]["duration_ms"] == 0.4


def test_incomplete_stream_requires_done_marker():
    runtime = Runtime.__new__(Runtime)
    provider = Streaming()
    provider.stream = lambda *a: iter([StreamEvent("text_delta", {"text": "partial"})])
    with pytest.raises(ValueError, match="incomplete_model_response"):
        runtime._stream_response(
            provider, None, None, lambda *a: None, now() + timedelta(seconds=10)
        )
