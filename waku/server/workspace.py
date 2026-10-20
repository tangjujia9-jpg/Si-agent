"""Explicitly enabled, bounded read access to linked local project folders."""

import os
from pathlib import Path

EXTENSIONS = {
    ".md",
    ".txt",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".css",
    ".html",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".rs",
    ".go",
    ".java",
    ".cpp",
    ".c",
    ".h",
    ".sh",
}
IGNORED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".artifacts",
    ".waku",
    ".idea",
    ".vscode",
    ".ssh",
    ".aws",
    ".codex",
    "hosted",
}


def roots():
    return [
        Path(p).resolve()
        for p in os.environ.get("SI_WORKSPACE_ROOTS", "").split(os.pathsep)
        if p.strip() and Path(p).is_dir()
    ]


def visible(path):
    name = path.name.lower()
    return (
        name not in IGNORED
        and not name.startswith(".")
        and not any(
            t in name for t in ("credential", "secret", "token", "private_key", "id_rsa", ".env")
        )
        and path.suffix.lower() not in {".pem", ".key", ".p12"}
    )


def folder(value):
    original = Path(value)
    target = original.resolve(strict=True)
    if not target.is_dir() or not any(target == root or root in target.parents for root in roots()):
        raise ValueError("folder outside configured workspace roots")
    allowed = next(root for root in roots() if target == root or root in target.parents)
    if any(not visible(Path(part)) for part in target.relative_to(allowed).parts):
        raise ValueError("folder is excluded")
    # Resolve containment and reject links/junctions, including intermediate ones.
    for part in (original, *original.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("linked directories are not supported")
    return target


def directories(value=None):
    if not value:
        return {
            "path": None,
            "parent": None,
            "directories": [{"name": r.name, "path": str(r)} for r in roots()],
        }
    current = folder(value)
    parent = str(current.parent) if current not in roots() else None
    return {
        "path": str(current),
        "parent": parent,
        "directories": [
            {"name": p.name, "path": str(p)}
            for p in sorted(current.iterdir(), key=lambda p: p.name.lower())
            if p.is_dir()
            and visible(p)
            and not p.is_symlink()
            and not getattr(p, "is_junction", lambda: False)()
        ][:200],
    }


def resolve_file(root, relative):
    from waku.server.schemas import DocumentInput

    DocumentInput.safe_path(relative)
    current = root
    for part in Path(relative).parts:
        current = current / part
        if (
            not visible(current)
            or current.is_symlink()
            or getattr(current, "is_junction", lambda: False)()
        ):
            raise ValueError("file is excluded")
    target = current.resolve(strict=True)
    if root not in target.parents or not target.is_file() or target.stat().st_size > 200000:
        raise ValueError("file outside workspace or too large")
    return target


def manifest(root):
    files, skipped, visited = [], 0, 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        visited += 1
        if visited > 2000:
            return {"files": files, "skipped": skipped, "truncated": True}
        dirs[:] = sorted(
            d
            for d in dirs
            if visible(Path(d))
            and not (Path(parent) / d).is_symlink()
            and not getattr(Path(parent) / d, "is_junction", lambda: False)()
        )
        for name in sorted(names):
            path = Path(parent) / name
            if (
                not visible(path)
                or path.suffix.lower() not in EXTENSIONS
                or path.is_symlink()
                or path.stat().st_size > 200000
            ):
                skipped += 1
                continue
            files.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
            if len(files) >= 500:
                return {"files": files, "skipped": skipped, "truncated": True}
    return {"files": files, "skipped": skipped, "truncated": False}
