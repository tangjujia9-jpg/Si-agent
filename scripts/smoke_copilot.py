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
    run = request(
        f"/api/threads/{thread['id']}/messages",
        {
            "content": "What storage does this project use? pgvector?",
            "idempotency_key": "smoke-turn",
            "provider": "demo",
            "model": "evidence-demo",
        },
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        snapshot = request(f"/api/runs/{run['id']}")
        if snapshot["status"] in {"failed", "unknown", "cancelled"}:
            raise RuntimeError("Runtime did not complete: " + str(snapshot["error_code"]))
        if snapshot["status"] == "succeeded":
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Worker did not finish offline run within 60 seconds")
    if "[si://" not in snapshot["reply"]:
        raise RuntimeError("Offline run did not cite its project evidence")
    events = request(f"/api/runs/{run['id']}/events")
    if not any(e["kind"] == "span" and e["data"]["name"] == "llm" for e in events):
        raise RuntimeError("Model trace is missing")
    with urlopen(
        args.url.rstrip("/") + f"/api/runs/{run['id']}/events/stream", timeout=5
    ) as stream:
        if "event: snapshot" not in stream.read().decode():
            raise RuntimeError("Durable SSE did not return a terminal snapshot")

    def finished_job(job_id):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            row = request(f"/api/jobs/{job_id}")
            if row["status"] == "succeeded":
                return row
            if row["status"] == "failed":
                raise RuntimeError("Unified memory job failed: " + str(row["error"]))
            time.sleep(0.5)
        raise RuntimeError("Unified memory job did not finish within 60 seconds")

    # Chat-derived facts enter the same evidence/candidate pipeline as documents.
    chat_run = request(
        f"/api/threads/{thread['id']}/messages",
        {
            "content": "Decision[session-policy]: SessionPolicyAlpha uses pgvector for recall.",
            "idempotency_key": "smoke-memory-turn",
            "tools": [],
        },
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        chat_snapshot = request(f"/api/runs/{chat_run['id']}")
        if chat_snapshot["status"] in {"failed", "unknown", "cancelled"}:
            raise RuntimeError("Conversation run failed")
        if chat_snapshot["status"] == "succeeded":
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Conversation run did not finish")
    distilled = request(f"/api/threads/{thread['id']}/memory/distill", {})
    finished_job(distilled["id"])
    proposals = request(path + "/memory/candidates")
    chat_candidate = next(c for c in proposals if c["title"] == "session-policy")
    if chat_candidate["status"] != "pending":
        raise RuntimeError("Automatically distilled memory bypassed review")
    accepted = request(f"/api/memory/candidates/{chat_candidate['id']}/apply", {})
    applied = finished_job(accepted["job_id"])
    if applied["result"]["status"] != "applied":
        raise RuntimeError("Chat-derived memory was not accepted")
    accepted_node = request(f"/api/memory/{applied['result']['node_id']}")
    if "SessionPolicyAlpha" not in accepted_node["content"]:
        raise RuntimeError("Chat-derived fact did not round-trip")
    print(
        json.dumps(
            {
                "status": "passed",
                "project_id": project["id"],
                "job_id": job["id"],
                "uri": node["uri"],
                "thread_id": thread["id"],
                "run_id": run["id"],
                "run_events": len(events),
                "retrieval_hits": len(search["hits"]),
                "embedding_model": search["embedding_model"],
                "candidate_id": candidate["id"],
                "history_revisions": len(versions),
                "distillation_job_id": distilled["id"],
                "chat_memory_uri": accepted_node["uri"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
