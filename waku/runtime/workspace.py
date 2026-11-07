"""Project-scoped paths and text snapshots; these checks are not a process sandbox."""

import hashlib
import os
from pathlib import Path, PurePosixPath

from waku.server.workspace import EXTENSIONS, folder, visible
from waku.storage.models import Project

SPECIAL = {".gitignore", ".dockerignore", "Dockerfile", "Makefile", "LICENSE"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(value):
    path = PurePosixPath(value)
    if (not value or len(value) > 240 or path.is_absolute() or "\\" in value
            or any(c in value for c in ":%?#\x00") or any(ord(c) < 32 for c in value)
            or any(p in {"", ".", ".."} or p.rstrip(" .") != p for p in value.split("/"))):
        raise ValueError("invalid relative path")
    for part in path.parts:
        if (part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(10)}, *{f"LPT{i}" for i in range(10)}}
                or not (visible(Path(part)) or part in {".github", ".gitignore", ".dockerignore"})):
            raise ValueError("excluded path")
    if path.suffix.lower() not in EXTENSIONS and path.name not in SPECIAL:
        raise ValueError("unsupported file")
    return path


def root_for(sessions, context, capability=None):
    from waku.storage.models import Run

    with sessions() as session:
        project = session.get(Project, context.project_id)
        if (not project or project.user_id != context.user_id or not project.root_path
                or (capability and not project.permissions.get(capability))):
            raise ValueError("workspace capability unavailable")
        if capability:
            run = session.get(Run, context.run_id)
            if (os.environ.get("SI_DEVELOPMENT_ENABLED") != "true" or not run
                    or run.context.get("mode") != "development" or run.status in {"cancelled", "failed", "unknown"}):
                raise ValueError("development run unavailable")
        return folder(project.root_path)


def target(root, relative, *, create=False):
    path = root
    for part in safe_relative(relative).parts:
        path = path / part
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("linked path")
        if path.exists() and path.is_file() and path.stat().st_nlink != 1:
            raise ValueError("hard-linked file")
    resolved = path.resolve()
    if root not in resolved.parents or (path.exists() and not path.is_file()):
        raise ValueError("not a project file")
    if not create and not path.is_file():
        raise ValueError("file unavailable")
    if path.exists() and path.stat().st_size > 200000:
        raise ValueError("file too large")
    return path


def scan(root, *, max_files=5000, max_bytes=20_000_000):
    files, skipped, visited, total = [], 0, 0, 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        visited += 1
        if visited > 5000:
            return files, skipped, True
        dirs[:] = sorted(d for d in dirs if (visible(Path(d)) or d == ".github")
                         and not (Path(parent) / d).is_symlink()
                         and not getattr(Path(parent) / d, "is_junction", lambda: False)())
        for name in sorted(names):
            relative = (Path(parent) / name).relative_to(root).as_posix()
            try:
                path = target(root, relative)
                data = path.read_bytes()
                data.decode("utf-8-sig")
            except (ValueError, OSError, UnicodeError):
                skipped += 1
                continue
            if len(files) >= max_files or total + len(data) > max_bytes:
                return files, skipped, True
            total += len(data)
            files.append({"path": relative, "size": len(data), "sha256": digest(data)})
    return files, skipped, False


def manifest_hash(files):
    import json

    return digest(json.dumps(sorted(files, key=lambda f: f["path"]), sort_keys=True).encode())
