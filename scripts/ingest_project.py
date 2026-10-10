"""Import explicitly selected local text files. Dry-run lists paths without sending content."""

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

EXTENSIONS = {".md", ".txt", ".py", ".ts", ".tsx", ".js", ".json"}
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", ".waku", "dist", "build", "__pycache__"}
EXCLUDED_NAMES = {"credentials.json", "client_secret*.json", "*token*.json", ".env*"}


def documents(root: Path):
    if root.is_symlink():
        raise ValueError("source root must not be a symlink")
    root = root.resolve(strict=True)
    if root.is_file():
        paths = [(root, root.name)]
    else:
        paths = []
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(
                n
                for n in names
                if n not in EXCLUDED_DIRS and not (Path(directory) / n).is_symlink()
            )
            paths.extend(
                (Path(directory) / name, (Path(directory) / name).relative_to(root).as_posix())
                for name in sorted(files)
            )
    for path, relative in paths:
        if (
            path.is_symlink()
            or path.suffix.lower() not in EXTENSIONS
            or any(fnmatch.fnmatch(path.name.lower(), p) for p in EXCLUDED_NAMES)
        ):
            continue
        resolved = path.resolve(strict=True)
        boundary = root.parent if root.is_file() else root
        if not resolved.is_relative_to(boundary):
            raise ValueError("source resolves outside selected root")
        if path.stat().st_size > 800000:
            raise ValueError(f"file exceeds text import limit: {relative}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue
        if len(text) > 200000 or "\x00" in text:
            raise ValueError(f"invalid or oversized text source: {relative}")
        yield {"path": relative, "content": text}


def batches(docs):
    batch, size = [], 0
    for document in docs:
        length = len(document["content"].encode("utf-8"))
        if batch and (len(batch) == 50 or size + length > 2000000):
            yield batch
            batch, size = [], 0
        batch.append(document)
        size += length
    if batch:
        yield batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a UTF-8 text file or project directory")
    parser.add_argument("--project", help="existing project ID; required except during dry-run")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.project:
        parser.error("--project is required")
    # Validate every source before the first submission, avoiding half imports
    # caused by an invalid later file. The selected directory must fit in memory.
    docs = list(documents(args.source))
    if args.dry_run:
        print(
            json.dumps(
                {"files": [d["path"] for d in docs], "count": len(docs)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for batch in batches(docs):
        canonical = json.dumps(batch, ensure_ascii=False, sort_keys=True).encode()
        payload = {
            "documents": batch,
            "idempotency_key": "files:" + hashlib.sha256(canonical).hexdigest(),
        }
        request = Request(
            args.url.rstrip("/") + f"/api/projects/{args.project}/ingest",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=20) as response:
            job = json.load(response)
        print(json.dumps({"job_id": job["id"], "status": job["status"], "files": len(batch)}))


if __name__ == "__main__":
    main()
