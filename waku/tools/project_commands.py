"""Approved command requests; execution belongs to the durable worker."""

import json
import os

from pydantic import Field
from sqlalchemy import select

from waku.runtime.workspace import manifest_hash, root_for, safe_relative, scan
from waku.storage.models import CommandTask, Job, Run, ToolOperation


def command_configuration():
    backend = os.environ.get("SI_COMMAND_BACKEND", "docker")
    if backend not in {"docker", "trusted-local"}:
        raise ValueError("unsupported command backend")
    return {"backend": backend, "image": os.environ.get("SI_COMMAND_IMAGE", "si-agent-runner:local")}


def command_preview(args, root):
    config = command_configuration()
    if sum(len(arg.encode()) for arg in args["argv"]) > 8000:
        raise ValueError("argv too large")
    if args["cwd"] != ".":
        safe_relative(args["cwd"] + "/placeholder.py")
        directory = root / args["cwd"]
        if not directory.is_dir() or directory.resolve() != directory:
            raise ValueError("working directory unavailable")
        for part in (directory, *directory.parents):
            if part == root:
                break
            if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
                raise ValueError("linked cwd")
    if (any("\x00" in a or "\n" in a or "\r" in a for a in args["argv"])
            or args["argv"][0] not in {"python", "python3", "pytest", "ruff", "node", "npm", "npx", "make", "go", "cargo"}):
        raise ValueError("unsupported executable")
    if config["backend"] == "trusted-local" and args["argv"][0] not in {"python", "python3", "node"}:
        raise ValueError("trusted local supports python and node executables only")
    files, skipped, truncated = scan(root)
    if truncated:
        raise ValueError("snapshot limit exceeded; use a smaller project root")
    preview = {"kind": "command", "root": str(root), **config, **args,
               "files": files, "snapshot_hash": manifest_hash(files), "skipped_files": skipped,
               "network": "none" if config["backend"] == "docker" else "host",
               "isolation": "container" if config["backend"] == "docker" else "trusted host access"}
    from waku.runtime.workspace import digest

    preview["hash"] = digest(json.dumps(preview, sort_keys=True).encode())
    return preview


def enqueue_command(sessions, ctx, oid):
    root = root_for(sessions, ctx, "execute_commands")
    with sessions.begin() as session:
        op = session.get(ToolOperation, oid)
        preview = op.preview
        if preview.get("root") != str(root) or preview.get("kind") != "command":
            raise ValueError("command preview unavailable")
        if any(preview[k] != v for k, v in command_configuration().items()):
            raise ValueError("execution configuration changed")
        old = session.scalar(select(CommandTask).where(CommandTask.operation_id == oid))
        if old:
            return {"command_id": old.id, "status": old.status, "snapshot_hash": old.snapshot_hash}
        job = Job(project_id=ctx.project_id, kind="command", idempotency_key="command:" + oid,
                  payload_hash=preview["hash"], payload={"operation_id": oid}, max_attempts=2)
        session.add(job)
        session.flush()
        task = CommandTask(project_id=ctx.project_id, run_id=ctx.run_id, operation_id=oid,
                           job_id=job.id, backend=preview["backend"], snapshot_hash=preview["snapshot_hash"], request=preview)
        session.add(task)
        session.flush()
        return {"command_id": task.id, "status": "queued", "snapshot_hash": task.snapshot_hash}


def command_view(task, offset=0, limit=3000):
    end = offset + len(task.output[offset:offset + limit].encode()[:4000].decode("utf-8", errors="ignore"))
    return {"command_id": task.id, "status": task.status, "backend": task.backend,
            "argv": task.request["argv"], "snapshot_hash": task.snapshot_hash,
            "output": task.output[offset:end], "next_offset": end if end < len(task.output) else None,
            "total_characters": len(task.output), "cancel_requested": task.cancel_requested, **task.result}


def command_tools(sessions, Arguments, ToolSpec):
    class CommandArgs(Arguments):
        argv: list[str] = Field(min_length=1, max_length=40)
        cwd: str = Field(default=".", min_length=1, max_length=200)
        timeout_seconds: int = Field(default=60, ge=1, le=300)

    class ResultArgs(Arguments):
        command_id: str = Field(min_length=1, max_length=36)
        offset: int = Field(default=0, ge=0)
        limit: int = Field(default=2000, ge=1, le=4000)

    class StopArgs(Arguments):
        command_id: str = Field(min_length=1, max_length=36)

    def access(session, ctx, identifier):
        from waku.runtime.context_store import dependencies_valid
        from waku.storage.models import Project

        task = session.get(CommandTask, identifier)
        project = session.get(Project, ctx.project_id)
        run = session.get(Run, task.run_id) if task else None
        if (not project or project.user_id != ctx.user_id or not run or task.project_id != ctx.project_id
                or run.context.get("redacted") or not dependencies_valid(session, ctx.project_id, run.context)):
            raise ValueError("command unavailable")
        return task

    def read(args, ctx, _oid):
        with sessions() as session:
            return command_view(access(session, ctx, args["command_id"]), args["offset"], args["limit"])

    def stop(args, ctx, _oid):
        with sessions.begin() as session:
            task = access(session, ctx, args["command_id"])
            if task.run_id != ctx.run_id:
                raise ValueError("can only stop commands created by this run")
            task.cancel_requested = True
            return {"command_id": task.id, "status": task.status, "cancel_requested": True}

    return [
        ToolSpec("run_project_command", "Run argv in an isolated, offline project snapshot after human approval. The agent waits for completion. No changes are copied back.",
                 CommandArgs, lambda args, ctx, oid: enqueue_command(sessions, ctx, oid), writes=True,
                 prepare=lambda args, ctx: command_preview(args, root_for(sessions, ctx, "execute_commands"))),
        ToolSpec("get_command_result", "Read command state, exit code and paginated logs", ResultArgs, read),
        ToolSpec("stop_project_command", "Request cancellation of a command created by this run", StopArgs, stop),
    ]
