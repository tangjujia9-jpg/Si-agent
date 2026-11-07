"""Finite command jobs with fenced dispatch, bounded logs and cancellation."""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from waku.runtime.context_store import dependencies_valid
from waku.runtime.processes import ProcessTree
from waku.runtime.runner import TERMINAL, append_event, enqueue_run
from waku.runtime.workspace import digest, folder, manifest_hash, scan, target
from waku.storage.models import CommandTask, Job, Project, Run, now

ACTIVE = {"queued", "running"}


def redact(text):
    return re.sub(r"(?i)(?:sk-[a-z0-9_-]{10,}|bearer\s+[a-z0-9_.-]{10,}|(?:api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]+)", "[REDACTED]", text)


def docker(args, *, check=True, timeout=20):
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout,
                          check=check, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def cleanup_container(name, identifier):
    info = docker(["inspect", name], check=False)
    if info.returncode:
        return
    container = json.loads(info.stdout)[0]
    if container["Config"].get("Labels", {}).get("si.command") != identifier:
        raise ValueError("container ownership mismatch")
    docker(["rm", "-f", name])


def snapshot(request, destination):
    root = folder(request["root"])
    files, _, truncated = scan(root)
    if truncated or manifest_hash(files) != request["snapshot_hash"]:
        raise ValueError("snapshot_stale")
    for item in files:
        data = target(root, item["path"]).read_bytes()
        if digest(data) != item["sha256"]:
            raise ValueError("snapshot_stale")
        output = destination / item["path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    return files


def finish(sessions, task_id, job_id, token, status, result, output):
    with sessions.begin() as session:
        identity = session.get(CommandTask, task_id)
        session.execute(select(Project).where(Project.id == identity.project_id).with_for_update())
        job = session.scalar(select(Job).where(Job.id == job_id, Job.lease_token == token).with_for_update())
        if not job:
            return False
        task = session.get(CommandTask, task_id)
        run = session.scalar(select(Run).where(Run.id == task.run_id).with_for_update())
        task.status, task.result = status, result
        task.output = "" if run.context.get("redacted") else redact(output)
        job.status, job.lease_token, job.lease_until = "succeeded", None, None
        job.result = {"command_id": task.id, "status": status, **result}
        append_event(session, run, "command_end", {"command_id": task.id, "status": status, **result})
        if run.status not in TERMINAL:
            enqueue_run(session, run, "resume:command:" + task.id)
    return True


def process_command(sessions, job_id, token):
    with sessions.begin() as session:
        job = session.get(Job, job_id)
        identity = session.scalar(select(CommandTask).where(CommandTask.job_id == job_id))
        if identity is None or job.lease_token != token:
            return False
        task_id, request, previous = identity.id, identity.request, identity.status
        session.execute(select(Project).where(Project.id == identity.project_id).with_for_update())
        job = session.scalar(select(Job).where(Job.id == job_id, Job.lease_token == token).with_for_update())
        if not job:
            return False
        run = session.scalar(select(Run).where(Run.id == identity.run_id).with_for_update())
        project = session.get(Project, identity.project_id)
        stopped = (identity.cancel_requested or run.status in TERMINAL
                   or not project.permissions.get("execute_commands")
                   or not dependencies_valid(session, run.project_id, run.context))
        if previous == "queued":
            identity.status = "running"
        job.lease_until = now() + timedelta(seconds=30)
        append_event(session, run, "command_start", {"command_id": task_id, "backend": identity.backend,
                                                    "snapshot_hash": identity.snapshot_hash})
    name = "si-command-" + task_id
    if previous != "queued":
        # Never dispatch the argv again after a lost worker. Kill only a labeled container we own.
        if request["backend"] == "docker":
            try:
                cleanup_container(name, task_id)
            except Exception:
                pass
        return finish(sessions, task_id, job_id, token, "unknown",
                      {"error_code": "interrupted_command", "exit_code": None}, "Worker interrupted; command was not replayed.")
    if stopped:
        return finish(sessions, task_id, job_id, token, "cancelled", {"exit_code": None}, "")
    started, output, truncated, tree = time.monotonic(), "", False, None
    container_created = False
    status, result = "failed", {"exit_code": None}
    # The parent is private to the worker, never exposed as a model-supplied filesystem path.
    with tempfile.TemporaryDirectory(prefix="si-command-") as temporary:
        work = Path(temporary) / "workspace"
        work.mkdir()
        try:
            originals = snapshot(request, work)
            if request["backend"] == "docker":
                docker(["image", "inspect", request["image"]])  # no implicit pull or installation
                docker(["create", "--name", name, "--label", "si.command=" + task_id,
                        "--network", "none", "--read-only", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", "--pids-limit", "128",
                        "--memory", "512m", "--cpus", "1", "--user", "65534:65534",
                        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--workdir", "/workspace/" + request["cwd"],
                        "--mount", f"type=bind,src={work},dst=/workspace", "--entrypoint", request["argv"][0],
                        request["image"], *request["argv"][1:]])
                container_created = True
                # A non-root container can create test output only in this disposable snapshot.
                for parent, dirs, files in os.walk(work):
                    os.chmod(parent, 0o777)
                    for file in files:
                        os.chmod(Path(parent) / file, 0o666)
                tree = ProcessTree(["docker", "start", "-a", name], work, os.environ.copy())
            else:
                env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT") if k in os.environ}
                env.update(HOME=temporary, USERPROFILE=temporary, TEMP=temporary, TMP=temporary,
                           PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
                argv = list(request["argv"])
                argv[0] = sys.executable if argv[0] in {"python", "python3"} else shutil.which("node")
                if not argv[0]:
                    raise ValueError("executable_unavailable")
                tree = ProcessTree(argv, work / request["cwd"], env)
            chunks = queue.Queue(maxsize=64)

            def read():
                try:
                    while data := tree.process.stdout.read1(2048):
                        chunks.put(data)
                finally:
                    chunks.put(None)

            threading.Thread(target=read, daemon=True).start()
            import codecs

            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            eof, last = False, 0
            reason = None
            while not eof or tree.process.poll() is None:
                try:
                    chunk = chunks.get(timeout=0.1)
                    if chunk is None:
                        eof = True
                    else:
                        decoded = decoder.decode(chunk)
                        if len(output) < 200000:
                            output += decoded[:200000 - len(output)]
                        else:
                            truncated = True
                except queue.Empty:
                    pass
                if time.monotonic() - last >= 0.5:
                    with sessions.begin() as session:
                        job = session.scalar(select(Job).where(Job.id == job_id, Job.lease_token == token).with_for_update())
                        task = session.get(CommandTask, task_id)
                        run = session.get(Run, task.run_id)
                        project = session.get(Project, task.project_id)
                        if not job:
                            reason = "lease_lost"
                        elif task.cancel_requested or run.status in TERMINAL or not project.permissions.get("execute_commands"):
                            reason = "cancelled"
                        elif time.monotonic() - started > request["timeout_seconds"]:
                            reason = "command_timeout"
                        elif run.context.get("deadline") and now().isoformat() >= run.context["deadline"]:
                            reason = "run_deadline"
                        if job:
                            job.lease_until = now() + timedelta(seconds=30)
                            task.output = "" if run.context.get("redacted") else redact(output)
                            append_event(session, run, "command_progress", {"command_id": task_id, "characters": len(output)})
                    last = time.monotonic()
                if reason:
                    if request["backend"] == "docker":
                        cleanup_container(name, task_id)
                    tree.close()
                    break
            tree.process.wait(timeout=10)
            status = "cancelled" if reason == "cancelled" else "failed" if reason or tree.process.returncode else "succeeded"
            result = {"exit_code": tree.process.returncode, "error_code": reason, "output_truncated": truncated,
                      "duration_seconds": round(time.monotonic() - started, 3)}
            after, _, limit = scan(work)
            original_hashes = {f["path"]: f["sha256"] for f in originals}
            result["generated_or_changed_files"] = [f["path"] for f in after if original_hashes.get(f["path"]) != f["sha256"]][:100]
            result["artifact_listing_truncated"] = limit
            result["workspace_changes_copied_back"] = False
        except Exception as exc:
            code = str(exc) if str(exc) in {"snapshot_stale", "executable_unavailable"} else "execution_environment_unavailable"
            result = {"exit_code": None, "error_code": code}
        finally:
            if tree:
                tree.close()
                tree.process.stdout.close()
            if container_created:
                try:
                    cleanup_container(name, task_id)
                except Exception:
                    status, result["error_code"] = "unknown", "container_cleanup_unconfirmed"
    return finish(sessions, task_id, job_id, token, status, result, output)
