"""Bounded Git inspection with external diff, textconv and hooks disabled."""

import os
import shutil
import subprocess

from pydantic import Field

from waku.runtime.workspace import root_for, safe_relative


def git_read(root, arguments):
    metadata = root / ".git"
    if not metadata.is_dir() or metadata.is_symlink() or getattr(metadata, "is_junction", lambda: False)():
        raise ValueError("linked Git metadata or non-repository")
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR") if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", LC_ALL="C.UTF-8")
    executable = shutil.which("git")
    if not executable:
        raise ValueError("git unavailable")
    command = [executable, "--no-pager", "--literal-pathspecs", "--git-dir=" + str(metadata),
               "--work-tree=" + str(root), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.attributesFile=" + os.devnull, "-c", "submodule.recurse=false", *arguments]
    # Stream to a file so an unexpectedly large diff cannot allocate unbounded pipe output.
    import tempfile

    with tempfile.TemporaryFile() as output:
        result = subprocess.run(command, cwd=root, env=env, stdout=output, stderr=subprocess.DEVNULL,
                                timeout=10, shell=False, check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise ValueError("git inspection failed")
        output.seek(0)
        data = output.read(200001)
    return data[:200000].decode("utf-8", errors="replace"), len(data) > 200000


def readable(path):
    try:
        safe_relative(path)
        return True
    except ValueError:
        return False


def git_tools(sessions, Arguments, ToolSpec):
    class StatusArgs(Arguments):
        offset: int = Field(default=0, ge=0)
        limit: int = Field(default=100, ge=1, le=200)

    class DiffArgs(Arguments):
        path: str | None = Field(default=None, max_length=240)
        staged: bool = False
        offset: int = Field(default=0, ge=0)
        limit: int = Field(default=3000, ge=1, le=4000)

    class LogArgs(Arguments):
        limit: int = Field(default=10, ge=1, le=30)

    def status(args, ctx, _oid):
        text, truncated = git_read(root_for(sessions, ctx), ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"])
        fields, rows, hidden = text.split("\x00"), [], 0
        i = 0
        while i < len(fields):
            item = fields[i]
            i += 1
            if not item:
                continue
            code, path = item[:2], item[3:]
            old = None
            if "R" in code or "C" in code:
                old = fields[i] if i < len(fields) else ""
                i += 1
            if readable(path) and (old is None or readable(old)):
                rows.append({"path": path, "status": code, "original_path": old})
            else:
                hidden += 1
        end = args["offset"] + args["limit"]
        return {"files": rows[args["offset"]:end], "next_offset": end if end < len(rows) else None,
                "truncated": truncated, "excluded_files": hidden, "scope": "supported non-sensitive text files"}

    def diff(args, ctx, _oid):
        root = root_for(sessions, ctx)
        flags = ["--cached"] if args["staged"] else []
        if args["path"]:
            safe_relative(args["path"])
            paths = [args["path"]]
        else:
            names, limited = git_read(root, ["diff", *flags, "--no-ext-diff", "--no-textconv", "--name-only", "-z", "--ignore-submodules=all"])
            if limited:
                raise ValueError("too many changed files; specify path")
            paths = [p for p in names.split("\x00") if p and readable(p)]
        if not paths:
            return {"diff": "", "next_offset": None, "scope": "supported text files; untracked content requires read_project_file"}
        text, truncated = git_read(root, ["diff", *flags, "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=all", "--", *paths[:200]])
        page = text[args["offset"]:args["offset"] + args["limit"]].encode()[:4000].decode("utf-8", errors="ignore")
        end = args["offset"] + len(page)
        return {"diff": page, "next_offset": end if end < len(text) else None, "truncated": truncated or len(paths) > 200,
                "total_characters": len(text), "staged": args["staged"], "scope": "tracked supported text files"}

    def log(args, ctx, _oid):
        text, truncated = git_read(root_for(sessions, ctx), ["log", "-" + str(args["limit"]), "--format=%h %as %s", "--no-decorate"])
        return {"commits": text.splitlines(), "truncated": truncated}

    return [ToolSpec("git_status", "Read current staged, unstaged and untracked file status; never discard edits", StatusArgs, status),
            ToolSpec("git_diff", "Read a bounded diff without executing external diff or textconv", DiffArgs, diff),
            ToolSpec("git_log", "Read recent commit subjects without changing Git state", LogArgs, log)]
