"""Exercise a running API and worker. Creates a fresh demo project, never deletes data."""

import argparse
import json
import time
from urllib.parse import urlencode
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
    index_id = status["result"]["index_job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        indexed = request(f"/api/jobs/{index_id}")
        if indexed["status"] == "failed":
            raise RuntimeError(indexed["error"])
        if indexed["status"] == "succeeded":
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Worker did not finish indexing within 60 seconds")
    search = request(
        "/api/memory/search?"
        + urlencode({"project_id": project["id"], "q": "pgvector", "tiers": "l2"})
    )
    if not search["hits"] or f"[{node['uri']}]" not in search["context"]:
        raise RuntimeError("retrieval did not return cited source evidence")
    if search["estimated_tokens"] > 3000:
        raise RuntimeError("retrieval exceeded its context budget")
    hit = search["hits"][0]
    evidence = request(f"/api/memory/{node['id']}/evidence?revision={hit['revision']}")
    if not evidence or hit["evidence_ids"][0] not in {e["id"] for e in evidence}:
        raise RuntimeError("retrieval evidence did not resolve to an exact source span")
    if detail["content"][hit["start_char"] : hit["end_char"]] != hit["snippet"]:
        raise RuntimeError("retrieval offsets did not match source content")
    enrich_id = status["result"]["enrich_job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        enriched = request(f"/api/jobs/{enrich_id}")
        if enriched["status"] == "failed":
            raise RuntimeError(enriched["error"])
        if enriched["status"] == "succeeded":
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Worker did not finish enrichment within 60 seconds")
    evidence = request(f"/api/memory/{node['id']}/evidence")
    candidate_payload = {
        "uri": node["uri"].split("/resources/")[0] + "/decisions/storage.md",
        "title": "Storage",
        "content": "Use PostgreSQL and pgvector.",
        "evidence_ids": [evidence[0]["id"]],
        "idempotency_key": "smoke-decision",
    }
    candidate = request(path + "/memory/candidates", candidate_payload)
    if request(path + "/memory/candidates", candidate_payload)["id"] != candidate["id"]:
        raise RuntimeError("candidate retry was not idempotent")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        consolidated = request(f"/api/jobs/{candidate['job_id']}")
        if consolidated["status"] == "succeeded":
            break
        if consolidated["status"] == "failed":
            raise RuntimeError(consolidated["error"])
        time.sleep(0.5)
    else:
        raise RuntimeError("Worker did not consolidate candidate within 30 seconds")
    if consolidated["result"]["status"] != "applied":
        raise RuntimeError("candidate was not applied")
    versions = request(f"/api/memory/{consolidated['result']['node_id']}/versions")
    if not versions or versions[0]["snapshot"]["content"] != candidate_payload["content"]:
        raise RuntimeError("memory history did not preserve accepted decision")
    thread = request("/api/threads", {"project_id": project["id"]})
    print(
        json.dumps(
            {
                "status": "passed",
                "project_id": project["id"],
                "job_id": job["id"],
                "uri": node["uri"],
                "thread_id": thread["id"],
                "retrieval_hits": len(search["hits"]),
                "embedding_model": search["embedding_model"],
                "candidate_id": candidate["id"],
                "history_revisions": len(versions),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
