"""Exercise real patch/command/Git work through the product API and worker."""

import os
import subprocess
import threading
import time
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from sqlalchemy import select

from evals.deterministic.test_copilot_backend import backend as backend_fixture
from evals.deterministic.test_copilot_runtime import Scripted
from waku.domain.contracts import RunContext
from waku.memory.postgres import PostgresMemory
from waku.providers.contracts import ModelResponse, Usage
from waku.runtime.runner import Runtime
from waku.runtime.workspace import digest, scan, target
from waku.storage.models import CommandTask, Job, Project, Run, now
from waku.tools.executor import project_executor
from waku.workers.runner import claim, process

backend = backend_fixture


def tool(name, args, call_id="step"):
    return ModelResponse(tool_calls=[{"id": call_id, "name": name, "arguments": args}], usage=Usage(100, 10, 110))


def development(backend, tmp_path, monkeypatch, responses):
    client, sessions = backend
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("SI_WORKSPACE_ROOTS", str(root))
    monkeypatch.setenv("SI_DEVELOPMENT_ENABLED", "true")
    monkeypatch.setenv("SI_COMMAND_BACKEND", "trusted-local")
    pid = client.post("/api/projects", json={"name": "Development", "root_path": str(root)}).json()["id"]
    permissions = client.put(f"/api/projects/{pid}/permissions", json={"write_files": True, "execute_commands": True})
    assert permissions.status_code == 200, permissions.text
    tid = client.post("/api/threads", json={"project_id": pid}).json()["id"]
    memory = PostgresMemory(sessions)
    executor = project_executor(memory, sessions)
    model = Scripted(responses)
    runtime = Runtime(sessions, memory, executor, provider_factory=lambda *a, **k: model)
    rid = client.post(f"/api/threads/{tid}/messages", json={
        "content": "Fix the validation bug, test it and inspect the diff.", "idempotency_key": "dev-1",
        "mode": "development", "tools": list(executor.tools), "max_tokens": 256000,
        "timeout_seconds": 600, "max_iterations": 20, "max_tool_calls": 40,
    }).json()["id"]
    return client, sessions, root, pid, rid, runtime, model


def work(sessions, runtime, *, kind=None):
    for _ in range(30):
        claimed = claim(sessions)
        assert claimed, "expected a queued job"
        with sessions() as session:
            actual = session.get(Job, claimed[0]).kind
        assert process(sessions, *claimed, runtime=runtime)
        if kind is None or actual == kind:
            return
    raise AssertionError("job not reached")


def approve(client, rid):
    run = client.get(f"/api/runs/{rid}").json()
    assert run["status"] == "waiting_approval", run
    op = next(op for op in run["operations"] if op["status"] == "pending")
    response = client.post(f"/api/operations/{op['id']}/approval", json={"approved": True, "preview_hash": op["preview"]["hash"]})
    assert response.status_code == 200, response.text
    return op


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_fix_validate_test_and_diff_is_a_real_loop(backend, tmp_path, monkeypatch, newline):
    source = "def valid(value):\n    return value >= 0\n".replace("\n", newline)
    corrected = source.replace(">= 0", "> 0")
    regression = "import unittest\nfrom app import valid\nclass TestValidation(unittest.TestCase):\n    def test_zero(self):\n        self.assertFalse(valid(0))\n    def test_positive(self):\n        self.assertTrue(valid(1))\n"
    args = {"files": [
        {"path": "app.py", "expected_sha256": digest(source.encode()), "old_text": "def valid(value):\n    return value >= 0", "new_text": "def valid(value):\n    return value > 0"},
        {"path": "test_app.py", "expected_sha256": None, "old_text": "", "new_text": regression},
    ]}
    client, sessions, root, _, rid, runtime, model = development(backend, tmp_path, monkeypatch, [
        tool("update_plan", {"steps": [{"title": "Fix validation", "status": "in_progress"}]}),
        tool("read_project_file", {"path": "app.py"}, "read"),
        tool("apply_project_patch", args, "patch"),
        tool("run_project_command", {"argv": ["python", "-m", "unittest", "-v"]}, "test"),
        tool("git_diff", {}, "diff"), ModelResponse("Validation fixed and both regression tests passed.", usage=Usage(100, 20, 120)),
    ])
    (root / "app.py").write_bytes(source.encode())
    (root / "notes.md").write_text("Existing user edit must survive", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "Initial"], check=True)
    work(sessions, runtime, kind="run")
    patch = approve(client, rid)
    assert "> 0" in patch["preview"]["files"][0]["diff"]
    assert "after" not in patch["preview"]["files"][0]
    work(sessions, runtime, kind="tool_outbox")
    work(sessions, runtime, kind="run")
    assert (root / "app.py").read_bytes() == corrected.encode()
    assert (root / "notes.md").read_text() == "Existing user edit must survive"
    command = approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    work(sessions, runtime, kind="command")
    for _ in range(5):
        if client.get(f"/api/runs/{rid}").json()["status"] == "succeeded":
            break
        work(sessions, runtime, kind="run")
    run = client.get(f"/api/runs/{rid}").json()
    assert run["status"] == "succeeded", run
    assert len(run["commands"]) == 1 and run["commands"][0]["exit_code"] == 0
    assert run["commands"][0]["snapshot_hash"] == command["preview"]["snapshot_hash"]
    full = client.get(f"/api/commands/{run['commands'][0]['command_id']}").json()
    assert "Ran 2 tests" in full["output"] and "OK" in full["output"]
    diff = next(op for op in run["operations"] if op["tool"] == "git_diff")["result"]["data"]["diff"]
    assert "-    return value >= 0" in diff and "+    return value > 0" in diff
    assert any("commands" in str(request.messages) and "exit_code" in str(request.messages) for request in model.requests)
    # Duplicate approval cannot create another command or apply the patch again.
    assert client.post(f"/api/operations/{command['id']}/approval", json={"approved": True, "preview_hash": command["preview"]["hash"]}).status_code == 200
    with sessions() as session:
        assert len(list(session.scalars(select(CommandTask)))) == 1


def test_preview_failure_explains_recovery_without_bypassing_approval(backend, tmp_path, monkeypatch):
    source = b"value = 1\r\n"
    client, sessions, root, _, rid, runtime, model = development(backend, tmp_path, monkeypatch, [
        tool("apply_project_patch", {"files": [{"path": "app.py", "expected_sha256": digest(source), "old_text": "missing", "new_text": "value = 2"}]}, "bad-patch"),
        tool("run_project_command", {"argv": ["ls", "-la"]}, "bad-command"),
        tool("apply_project_patch", {"files": [{"path": "app.py", "expected_sha256": digest(source), "old_text": "value = 1\n", "new_text": "value = 2\n"}]}, "fixed-patch"),
    ])
    (root / "app.py").write_bytes(source)
    work(sessions, runtime, kind="run")
    run = client.get(f"/api/runs/{rid}").json()
    errors = {op["result"].get("error_code"): op["result"] for op in run["operations"] if op["status"] == "failed"}
    assert errors["patch_text_mismatch"]["message"].startswith("old_text must match exactly once")
    assert "shell commands are unavailable" in errors["unsupported_executable"]["message"]
    assert str(root) not in str(errors)
    assert any("patch_text_mismatch" in str(request.messages) for request in model.requests)
    assert not run["commands"]
    assert (root / "app.py").read_bytes() == source
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    assert (root / "app.py").read_bytes() == b"value = 2\r\n"


def test_patch_preserves_untouched_mixed_newlines_and_requires_unique_text(tmp_path):
    from waku.tools.executor import PreviewError
    from waku.tools.project_patch import preview_patch

    source = b"first\nvalue = 1\r\nlast\n"
    (tmp_path / "app.py").write_bytes(source)
    edit = {"path": "app.py", "expected_sha256": digest(source), "old_text": "value = 1\n", "new_text": "value = 2\n"}
    preview = preview_patch({"files": [edit]}, tmp_path)
    assert preview["files"][0]["after"].encode() == b"first\nvalue = 2\r\nlast\n"
    assert (tmp_path / "app.py").read_bytes() == source
    with pytest.raises(PreviewError) as failure:
        preview_patch({"files": [{**edit, "old_text": "\n"}]}, tmp_path)
    assert failure.value.code == "patch_text_mismatch"


def test_git_inspection_respects_newlines_without_running_content_filters(tmp_path, monkeypatch):
    from waku.tools.project_git import git_read

    config = tmp_path / "global.config"
    config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_bytes(b"value = 1\r\n")
    (root / ".gitattributes").write_bytes(b"*.py filter=probe\r\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "Initial"], check=True)
    sentinel = root / "filter-ran.txt"
    code = 'from pathlib import Path; Path("filter-ran.txt").write_text("ran")'
    import shlex
    import sys

    command = shlex.quote(sys.executable.replace("\\", "/")) + " -c " + shlex.quote(code)
    subprocess.run(["git", "-C", str(root), "config", "filter.probe.clean", command], check=True)
    subprocess.run(["git", "-C", str(root), "config", "filter.probe.required", "true"], check=True)
    # Change bytes to force Git to inspect content instead of trusting its stat cache.
    (root / "app.py").write_bytes(b"value = 2\r\n")
    diff, _ = git_read(root, ["diff", "--no-ext-diff", "--no-textconv", "--", "app.py"])
    assert "+value = 2" in diff and "-value = 1" in diff
    assert not sentinel.exists()
    (root / "app.py").write_bytes(b"value = 1\r\n")
    status, _ = git_read(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    assert status == ""


def test_failed_build_can_be_fixed_and_verified_in_a_new_snapshot(backend, tmp_path, monkeypatch):
    client, sessions, root, _, rid, runtime, _model = development(backend, tmp_path, monkeypatch, [
        tool("run_project_command", {"argv": ["python", "build.py"]}, "build-1"),
        tool("apply_project_patch", {"files": [{"path": "config.json", "new_text": '{"title": "Si"}'}]}, "fix"),
        tool("run_project_command", {"argv": ["python", "build.py"]}, "build-2"),
        ModelResponse("The build passed after adding its configuration.", usage=Usage(10, 10, 20)),
    ])
    (root / "build.py").write_text('import json\nfrom pathlib import Path\nconfig=json.loads(Path("config.json").read_text())\nPath("output.md").write_text(config["title"])\nprint("build complete")\n')
    work(sessions, runtime, kind="run")
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    work(sessions, runtime, kind="command")
    work(sessions, runtime, kind="run")
    failed = client.get(f"/api/runs/{rid}").json()["commands"][0]
    assert failed["status"] == "failed" and failed["exit_code"] != 0
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    work(sessions, runtime, kind="run")
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    work(sessions, runtime, kind="command")
    for _ in range(4):
        if client.get(f"/api/runs/{rid}").json()["status"] == "succeeded":
            break
        work(sessions, runtime, kind="run")
    runs = client.get(f"/api/runs/{rid}").json()
    assert runs["status"] == "succeeded", runs
    successful = next(c for c in runs["commands"] if c["status"] == "succeeded")
    assert successful["snapshot_hash"] != failed["snapshot_hash"]
    assert not (root / "output.md").exists()  # isolated command artifacts do not bypass patch approval


def test_stale_preview_hash_scope_and_permissions_are_enforced(backend, tmp_path, monkeypatch):
    client, sessions, root, pid, rid, runtime, _ = development(backend, tmp_path, monkeypatch, [
        tool("apply_project_patch", {"files": [{"path": "README.md", "new_text": "Approved content"}]}),
        ModelResponse("conflict recorded"),
    ])
    work(sessions, runtime, kind="run")
    run = client.get(f"/api/runs/{rid}").json()
    op = run["operations"][0]
    assert client.post(f"/api/operations/{op['id']}/approval", json={"approved": True}).status_code == 409
    (root / "README.md").write_text("Concurrent user edit")
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    assert (root / "README.md").read_text() == "Concurrent user edit"
    assert client.get(f"/api/runs/{rid}").json()["operations"][0]["result"]["error_code"] == "patch_conflict"
    with sessions.begin() as session:
        session.get(Project, pid).user_id = "someone-else"
    assert client.get(f"/api/projects/{pid}/capabilities").status_code == 404
    assert client.put(f"/api/projects/{pid}/permissions", json={"write_files": True}).status_code == 404


@pytest.mark.parametrize("path", ["../escape.py", ".env.json", ".git/config", "sub/NUL.py", "sub/trailing. /x.py", "C:/x.py", ".ssh/key.py"])
def test_development_paths_refuse_escape_and_sensitive_files(tmp_path, path):
    with pytest.raises(ValueError):
        target(tmp_path, path, create=True)


def test_snapshot_and_file_search_disclose_coverage(backend, tmp_path, monkeypatch):
    _client, _sessions, root, pid, rid, runtime, _ = development(backend, tmp_path, monkeypatch, [])
    (root / ".gitignore").write_text("*.pyc\n")
    (root / ".github").mkdir()
    (root / ".github" / "ci.yml").write_text("name: CI\n")
    (root / "code.py").write_text("needle\nneedle\n")
    (root / "api-token.txt").write_text("private")
    files, skipped, truncated = scan(root)
    assert {f["path"] for f in files} == {".gitignore", ".github/ci.yml", "code.py"}
    assert skipped == 1 and not truncated
    ctx = RunContext(run_id=rid, project_id=pid)
    exe = runtime.executor
    args = exe.validate("search_project_files", {"query": "needle", "glob": "*.py", "limit": 1}, exe.tools)
    first = exe.execute("search_project_files", args, ctx, "search").data
    assert first["next_offset"] == 1 and not first["scan_truncated"]
    args["offset"] = 1
    assert exe.execute("search_project_files", args, ctx, "next").data["matches"][0]["line"] == 2


@pytest.mark.parametrize("reason", ["timeout", "cancel"])
def test_command_termination_preserves_logs_and_kills_tree(backend, tmp_path, monkeypatch, reason):
    client, sessions, root, _, rid, runtime, _ = development(backend, tmp_path, monkeypatch, [
        tool("run_project_command", {"argv": ["python", "slow.py"], "timeout_seconds": 1 if reason == "timeout" else 30}),
    ])
    (root / "slow.py").write_text('import time\nprint("started", flush=True)\ntime.sleep(20)\n')
    work(sessions, runtime, kind="run")
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    if reason == "cancel":
        def cancel():
            time.sleep(1)
            client.post(f"/api/runs/{rid}/cancel")
        thread = threading.Thread(target=cancel)
        thread.start()
    start = time.monotonic()
    work(sessions, runtime, kind="command")
    if reason == "cancel":
        thread.join()
    assert time.monotonic() - start < 12
    command = client.get(f"/api/runs/{rid}").json()["commands"][0]
    assert command["status"] == ("failed" if reason == "timeout" else "cancelled")
    assert "started" in client.get(f"/api/commands/{command['command_id']}").json()["output"]


def test_stale_snapshot_and_lost_worker_never_replay_a_command(backend, tmp_path, monkeypatch):
    client, sessions, root, _, rid, runtime, _ = development(backend, tmp_path, monkeypatch, [
        tool("run_project_command", {"argv": ["python", "check.py"]}),
    ])
    (root / "check.py").write_text('print("original")\n')
    work(sessions, runtime, kind="run")
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    (root / "check.py").write_text('print("changed")\n')
    work(sessions, runtime, kind="command")
    command = client.get(f"/api/runs/{rid}").json()["commands"][0]
    assert command["status"] == "failed" and command["error_code"] == "snapshot_stale"
    with sessions.begin() as session:
        task = session.get(CommandTask, command["command_id"])
        task.status = "running"
        job = session.get(Job, task.job_id)
        job.status, job.lease_until = "running", now() - timedelta(seconds=1)
    work(sessions, runtime, kind="command")
    command = client.get(f"/api/commands/{command['command_id']}").json()
    assert command["status"] == "unknown" and command["error_code"] == "interrupted_command"


def test_analysis_mode_does_not_gain_development_tools(backend, tmp_path, monkeypatch):
    client, sessions, _root, pid, rid, _, _ = development(backend, tmp_path, monkeypatch, [])
    tid = client.post("/api/threads", json={"project_id": pid}).json()["id"]
    response = client.post(f"/api/threads/{tid}/messages", json={"content": "Inspect", "idempotency_key": "analysis", "tools": ["apply_project_patch", "run_project_command", "git_status"]})
    with sessions() as session:
        assert session.get(Run, response.json()["id"]).context["tools"] == ["git_status"]
    client.put(f"/api/projects/{pid}/permissions", json={})
    ctx = RunContext(run_id=rid, project_id=pid)
    from waku.runtime.workspace import root_for
    with pytest.raises(ValueError):
        root_for(sessions, ctx, "write_files")


def test_partial_patch_journal_recovers_without_overwriting_prior_changes(backend, tmp_path, monkeypatch):
    client, sessions, root, pid, rid, runtime, _ = development(backend, tmp_path, monkeypatch, [
        tool("apply_project_patch", {"files": [{"path": "a.md", "new_text": "First"}, {"path": "b.md", "new_text": "Second"}]}),
    ])
    work(sessions, runtime, kind="run")
    op = approve(client, rid)
    from waku.tools import project_patch
    original = project_patch.os.replace
    calls = []

    def fail_second(source, destination):
        calls.append(str(destination))
        if len(calls) == 2:
            raise OSError("simulated interrupted multi-file write")
        return original(source, destination)

    monkeypatch.setattr(project_patch.os, "replace", fail_second)
    work(sessions, runtime, kind="tool_outbox")
    result = client.get(f"/api/runs/{rid}").json()["operations"][0]["result"]
    assert result["status"] == "partial" and result["completed_steps"] == ["a.md"]
    assert result["pending_steps"] == ["b.md"] and (root / "a.md").read_text() == "First"
    monkeypatch.setattr(project_patch.os, "replace", original)
    recovered = project_patch.apply_patch(sessions, RunContext(run_id=rid, project_id=pid), op["id"])
    assert recovered.status == "succeeded"
    assert (root / "b.md").read_text() == "Second" and len(recovered.completed_steps) == 2


def test_large_logs_are_bounded_redacted_and_project_scoped(backend, tmp_path, monkeypatch):
    client, sessions, root, pid, rid, runtime, _ = development(backend, tmp_path, monkeypatch, [
        tool("run_project_command", {"argv": ["python", "logs.py"]}),
    ])
    monkeypatch.setenv("SI_FAKE_SECRET", "credential-never-inherited")
    (root / "logs.py").write_text('import os\nprint(os.getenv("SI_FAKE_SECRET", "no-key"), flush=True)\nprint("sk-abcdefghijklmnopqrstuv", flush=True)\nprint("中文"*150000)\n')
    work(sessions, runtime, kind="run")
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    work(sessions, runtime, kind="command")
    command = client.get(f"/api/runs/{rid}").json()["commands"][0]
    page = client.get(f"/api/commands/{command['command_id']}").json()
    assert "no-key" in page["output"] and "[REDACTED]" in page["output"] and "sk-abc" not in page["output"]
    assert page["output_truncated"] and page["next_offset"] > 0
    second = client.get(f"/api/commands/{command['command_id']}?offset={page['next_offset']}").json()
    assert "中文" in second["output"] and len(second["output"].encode()) <= 4000
    with sessions.begin() as session:
        session.get(Project, pid).user_id = "other-user"
    assert client.get(f"/api/commands/{command['command_id']}").status_code == 404


@pytest.mark.skipif(os.environ.get("SI_TEST_COMMAND_DOCKER") != "true", reason="explicit container execution acceptance")
def test_docker_snapshot_is_offline_unprivileged_and_never_copied_back(backend, tmp_path, monkeypatch):
    client, sessions, root, _, rid, runtime, _ = development(backend, tmp_path, monkeypatch, [
        tool("run_project_command", {"argv": ["python", "isolation.py"]}),
    ])
    monkeypatch.setenv("SI_COMMAND_BACKEND", "docker")
    monkeypatch.setenv("SI_FAKE_SECRET", "not-a-container-credential")
    (root / "isolation.py").write_text('''import os, socket
from pathlib import Path
assert os.geteuid() != 0
assert os.getenv("SI_FAKE_SECRET") is None
try:
    Path("/etc/si-probe").write_text("forbidden")
    raise AssertionError("container root writable")
except OSError:
    pass
try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.5)
    raise AssertionError("network reachable")
except OSError:
    pass
Path("generated.md").write_text("snapshot only")
print("isolation verified")
''')
    work(sessions, runtime, kind="run")
    approve(client, rid)
    work(sessions, runtime, kind="tool_outbox")
    work(sessions, runtime, kind="command")
    command = client.get(f"/api/runs/{rid}").json()["commands"][0]
    full = client.get(f"/api/commands/{command['command_id']}").json()
    assert full["status"] == "succeeded", full
    assert "isolation verified" in full["output"] and "generated.md" in full["generated_or_changed_files"]
    assert not (root / "generated.md").exists()
