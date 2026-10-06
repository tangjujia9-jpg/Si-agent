"""Exercise a running API and worker. Creates a fresh demo project, never deletes data."""

import argparse
import json
import time
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    def request(path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = Request(
            args.url.rstrip("/") + path, data=body, headers={"Content-Type": "application/json"}
        )
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    deadline = time.monotonic() + 60
    while True:
        try:
            request("/ready")
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("API did not become ready within 60 seconds") from None
            time.sleep(1)
    project = request("/api/projects", {"name": f"Smoke demo {uuid4().hex[:8]}"})
    path = f"/api/projects/{project['id']}"
    payload = {
        "idempotency_key": "smoke-import",
        "documents": [
            {
                "path": "docs/architecture.md",
                "content": "# Project Context\nUse PostgreSQL and pgvector.",
            }
        ],
    }
    job = request(path + "/ingest", payload)
    if request(path + "/ingest", payload)["id"] != job["id"]:
        raise RuntimeError("duplicate ingestion was not idempotent")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = request(f"/api/jobs/{job['id']}")
        if status["status"] == "failed":
            raise RuntimeError(status["error"])
        if status["status"] == "succeeded":
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Worker did not finish ingestion within 30 seconds")
    nodes = request(path + "/context/tree")
    node = next(n for n in nodes if n["kind"] == "resource")
    detail = request(f"/api/memory/{node['id']}")
    if detail["content"] != payload["documents"][0]["content"]:
        raise RuntimeError("source content did not round-trip")
    thread = request("/api/threads", {"project_id": project["id"]})
    print(
        json.dumps(
            {
                "status": "passed",
                "project_id": project["id"],
                "job_id": job["id"],
                "uri": node["uri"],
                "thread_id": thread["id"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
