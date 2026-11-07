"""Exact, previewed text edits with a durable per-file recovery journal."""

import difflib
import json
import os
import tempfile

from pydantic import Field
from sqlalchemy import select

from waku.domain.contracts import ToolResult
from waku.runtime.workspace import digest, root_for, target
from waku.storage.models import Project, Run, ToolOperation


def preview_patch(args, root):
    changes = []
    if len({f["path"] for f in args["files"]}) != len(args["files"]):
        raise ValueError("duplicate paths")
    for edit in args["files"]:
        path = target(root, edit["path"], create=True)
        before = path.read_bytes() if path.exists() else None
        if before is None:
            if edit["expected_sha256"] is not None or edit["old_text"]:
                raise ValueError("new file precondition")
            after = edit["new_text"].encode()
        else:
            if edit["expected_sha256"] != digest(before) or not edit["old_text"]:
                raise ValueError("file changed; read it again")
            text = before.decode("utf-8")
            if text.count(edit["old_text"]) != 1:
                raise ValueError("old text must match exactly once")
            after = text.replace(edit["old_text"], edit["new_text"], 1).encode()
        if (len(after) > 200000 or b"\x00" in after or before == after):
            raise ValueError("invalid edit")
        patch = "".join(difflib.unified_diff(
            (before or b"").decode("utf-8").splitlines(keepends=True),
            after.decode().splitlines(keepends=True), fromfile=edit["path"], tofile=edit["path"],
        ))
        changes.append({"path": edit["path"], "before_hash": digest(before) if before is not None else None,
                        "after_hash": digest(after), "after": after.decode(), "diff": patch, "phase": "pending"})
    if len(json.dumps(changes, ensure_ascii=False).encode()) > 180000:
        raise ValueError("patch too large; split it")
    preview = {"kind": "patch", "root": str(root), "files": changes}
    preview["hash"] = digest(json.dumps(preview, sort_keys=True).encode())
    return preview


def apply_patch(sessions, ctx, oid):
    root = root_for(sessions, ctx, "write_files")
    completed, preview = [], {}
    try:
        with sessions() as session:
            preview = session.get(ToolOperation, oid).preview
        if preview.get("root") != str(root) or preview.get("kind") != "patch":
            raise ValueError("preview unavailable")
        completed = [edit["path"] for edit in preview["files"] if edit["phase"] == "completed"]
        # Validate every file before the first replace. Recovery only accepts our recorded after hash.
        for edit in preview["files"]:
            path = target(root, edit["path"], create=True)
            actual = digest(path.read_bytes()) if path.exists() else None
            if actual != edit["before_hash"] and not (
                edit["phase"] in {"applying", "completed"} and actual == edit["after_hash"]
            ):
                raise ValueError("stale patch preview")
        for index, edit in enumerate(preview["files"]):
            # Serialize agent writers and permission changes; external editors are checked again.
            with sessions.begin() as session:
                project = session.scalar(select(Project).where(Project.id == ctx.project_id).with_for_update())
                run = session.scalar(select(Run).where(Run.id == ctx.run_id).with_for_update())
                if not project.permissions.get("write_files") or run.status in {"cancelled", "failed", "unknown"}:
                    raise ValueError("write revoked")
                op = session.get(ToolOperation, oid)
                journal = json.loads(json.dumps(op.preview))
                journal["files"][index]["phase"] = "applying"
                op.preview = journal
            path = target(root, edit["path"], create=True)
            actual = digest(path.read_bytes()) if path.exists() else None
            if actual != edit["after_hash"]:
                if actual != edit["before_hash"]:
                    raise ValueError("stale patch preview")
                path.parent.mkdir(parents=True, exist_ok=True)
                path = target(root, edit["path"], create=True)
                fd, temporary = tempfile.mkstemp(prefix=".si-edit-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(edit["after"].encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                    if path.exists():
                        os.chmod(temporary, path.stat().st_mode)
                    if (digest(path.read_bytes()) if path.exists() else None) != edit["before_hash"]:
                        raise ValueError("stale patch preview")
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            if edit["path"] not in completed:
                completed.append(edit["path"])
            with sessions.begin() as session:
                op = session.get(ToolOperation, oid)
                journal = json.loads(json.dumps(op.preview))
                journal["files"][index]["phase"] = "completed"
                op.preview = journal
        return ToolResult("succeeded", "Approved changes applied to linked workspace", completed_steps=tuple(completed),
                          data={"files": [{k: f[k] for k in ("path", "before_hash", "after_hash")} for f in preview["files"]]})
    except (ValueError, OSError, UnicodeError):
        return ToolResult("partial" if completed else "failed", "Workspace changed or edit interrupted; inspect the journal",
                          completed_steps=tuple(completed), error_code="patch_conflict",
                          pending_steps=tuple(f["path"] for f in preview.get("files", []) if f["path"] not in completed))


def patch_tool(sessions, Arguments, ToolSpec):
    class FileEdit(Arguments):
        path: str = Field(min_length=1, max_length=240)
        expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
        old_text: str = Field(default="", max_length=40000)
        new_text: str = Field(max_length=40000)

    class PatchArgs(Arguments):
        files: list[FileEdit] = Field(min_length=1, max_length=10)

    def prepare(args, ctx):
        return preview_patch(args, root_for(sessions, ctx, "write_files"))

    return ToolSpec("apply_project_patch", "Preview exact text edits. Read sha256 first; old_text must match once. Human approval required.",
                    PatchArgs, lambda args, ctx, oid: apply_patch(sessions, ctx, oid), writes=True, prepare=prepare)
