"""Lifecycle boundaries: exact evidence, stale writers, conflicts and durable forgetting."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from evals.deterministic.test_copilot_backend import backend as backend_fixture
from evals.deterministic.test_copilot_backend import project, submit
from evals.deterministic.test_copilot_memory import SemanticFixture, imported
from waku.memory.embeddings import DemoEmbedder
from waku.memory.extraction import CompatibleExtractor
from waku.memory.lifecycle import chunks, record_version
from waku.memory.port import MemoryQuery
from waku.memory.postgres import PostgresMemory
from waku.memory.summaries import CompatibleSummarizer, ExtractiveSummarizer
from waku.storage.models import ContextIndex, ContextNode, Job, MemoryCandidate
from waku.workers.runner import claim, process, run_once

backend = backend_fixture


def drain(sessions):
    for _ in range(20):
        if not run_once(sessions, DemoEmbedder()):
            return
    raise AssertionError("worker failed to drain bounded jobs")


def propose(client, pid, leaf, *, action="ADD", key="first", content="Use SQLite", **kwargs):
    evidence = client.get(f"/api/memory/{leaf['id']}/evidence").json()[0]
    response = client.post(
        f"/api/projects/{pid}/memory/candidates",
        json={
            "uri": leaf["uri"].split("/resources/")[0] + "/decisions/database.md",
            "kind": "episodic",
            "title": "Database",
            "content": content,
            "action": action,
            "evidence_ids": [evidence["id"]],
            "idempotency_key": key,
            **kwargs,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_chunks_cover_source_and_semantic_tail_has_exact_evidence(backend):
    client, sessions = backend
    text = "# Source\n" + "Ordinary text\n" * 800 + "Replace access keys monthly."
    pid, leaf, _ = imported(backend, text)
    semantic = SemanticFixture()
    while run_once(sessions, semantic):
        pass
    memory = PostgresMemory(sessions, semantic)
    result = memory.retrieve(MemoryQuery("credential rotation", project_id=pid, tiers=("l2",)))
    hit = result.context.hits[0]
    assert hit.id == leaf["id"] and hit.start_char > 6000
    assert "access keys" in hit.snippet and text[hit.start_char : hit.end_char] == hit.snippet
    evidence = client.get(
        f"/api/memory/{hit.id}/evidence", params={"revision": hit.revision}
    ).json()
    record = next(e for e in evidence if e["id"] in hit.evidence_ids)
    assert text[record["start_char"] : record["end_char"]] == record["quote"]
    coverage = [False] * len(text)
    for chunk in chunks(text):
        assert len(chunk.text) <= 2400 and chunk.text == text[chunk.start : chunk.end]
        coverage[chunk.start : chunk.end] = [True] * (chunk.end - chunk.start)
    assert all(coverage)
    with sessions() as session:
        assert (
            max(
                len(r.body)
                for r in session.scalars(
                    select(ContextIndex).where(
                        ContextIndex.node_id == hit.id, ContextIndex.tier == "l2"
                    )
                )
            )
            <= 2400
        )


def test_resource_versions_preserve_old_content_and_evidence(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend, "# Decision\nUse SQLite")
    old = client.get(f"/api/memory/{leaf['id']}/evidence").json()
    submit(
        client,
        pid,
        key="update",
        docs=[{"path": "docs/design.md", "content": "# Decision\nUse PostgreSQL"}],
    )
    run_once(sessions)
    versions = client.get(f"/api/memory/{leaf['id']}/versions").json()
    assert [v["revision"] for v in versions] == [2, 1]
    assert versions[1]["snapshot"]["content"] == "# Decision\nUse SQLite"
    assert versions[0]["snapshot"]["content"].endswith("PostgreSQL")
    assert client.get(f"/api/memory/{leaf['id']}/evidence", params={"revision": 1}).json() == old
    assert not PostgresMemory(sessions).search(MemoryQuery("SQLite", project_id=pid, tiers=("l2",)))


def test_candidate_actions_idempotency_revision_conflict_and_retract(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend, "# Facts\nSQLite and PostgreSQL are database options.")
    first = propose(client, pid, leaf)
    assert propose(client, pid, leaf)["id"] == first["id"]
    drain(sessions)
    candidates = lambda: client.get(f"/api/projects/{pid}/memory/candidates").json()
    applied = next(c for c in candidates() if c["id"] == first["id"])
    assert applied["status"] == "applied"
    node_id = applied["node_id"]
    propose(client, pid, leaf, key="duplicate")
    propose(client, pid, leaf, key="bad-add", content="Use PostgreSQL")
    propose(
        client,
        pid,
        leaf,
        action="UPDATE",
        key="update",
        content="Use PostgreSQL",
        expected_revision=1,
    )
    drain(sessions)
    assert client.get(f"/api/memory/{node_id}").json()["content"] == "Use PostgreSQL"
    states = {c["reason"] for c in candidates()}
    assert {"duplicate_content", "target_exists", "update"} <= states
    propose(client, pid, leaf, action="UPDATE", key="stale", expected_revision=1)
    drain(sessions)
    assert any(c["reason"] == "revision_mismatch" for c in candidates())
    propose(client, pid, leaf, action="RETRACT", key="retract", expected_revision=2)
    propose(client, pid, leaf, action="SKIP", key="skip")
    drain(sessions)
    assert client.get(f"/api/memory/{node_id}").json()["status"] == "retracted"
    assert any(c["reason"] == "explicit_skip" for c in candidates())
    versions = client.get(f"/api/memory/{node_id}/versions").json()
    assert [v["revision"] for v in versions] == [3, 2, 1]


def test_supersedes_and_evidence_scope_validation(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend)
    first = propose(client, pid, leaf)
    drain(sessions)
    with sessions() as session:
        old_id = session.get(MemoryCandidate, first["id"]).node_id
    newer = propose(
        client,
        pid,
        leaf,
        action="SUPERSEDES",
        key="supersede",
        content="Use PostgreSQL",
        expected_revision=1,
        supersedes_id=old_id,
        uri=first["uri"].replace("database.md", "database-v2.md"),
    )
    drain(sessions)
    assert client.get(f"/api/memory/{old_id}").json()["status"] == "superseded"
    with sessions() as session:
        assert session.get(MemoryCandidate, newer["id"]).status == "applied"
    other = project(client, "Other")
    evidence = client.get(f"/api/memory/{leaf['id']}/evidence").json()[0]["id"]
    assert (
        client.post(
            f"/api/projects/{other}/memory/candidates",
            json={
                "uri": f"si://users/default/projects/{other}/memories/x.md",
                "title": "x",
                "content": "x",
                "evidence_ids": [evidence],
                "idempotency_key": "scope",
            },
        ).status_code
        == 409
    )


def test_pending_candidate_rejects_changed_source_and_conflicting_key(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend)
    candidate = propose(client, pid, leaf)
    different = client.post(
        f"/api/projects/{pid}/memory/candidates",
        json={"uri": candidate["uri"], "title": "Changed", "idempotency_key": "first"},
    )
    assert different.status_code == 409
    submit(
        client,
        pid,
        key="change-source",
        docs=[{"path": "docs/design.md", "content": "Changed evidence"}],
    )
    drain(sessions)
    with sessions() as session:
        result = session.get(MemoryCandidate, candidate["id"])
        assert result.status == "conflict" and result.reason == "stale_or_invalid_evidence"


def test_forget_redacts_history_and_derivatives_and_blocks_replay(backend):
    client, sessions = backend
    pid, leaf, import_job = imported(backend, "# Secret decision\nUse SQLite")
    first = propose(client, pid, leaf)
    drain(sessions)
    with sessions() as session:
        derived_id = session.get(MemoryCandidate, first["id"]).node_id
    assert client.post(f"/api/memory/{leaf['id']}/forget", json={}).json()["forgotten"]
    assert client.post(f"/api/memory/{leaf['id']}/forget", json={}).json()["forgotten"]
    assert client.get(f"/api/memory/{leaf['id']}").json()["content"] == ""
    assert client.get(f"/api/memory/{derived_id}").json()["content"] == ""
    assert all(
        not v["snapshot"]["content"]
        for v in client.get(f"/api/memory/{leaf['id']}/versions").json()
    )
    assert all(
        not e["quote"] and e["redacted"]
        for e in client.get(f"/api/memory/{leaf['id']}/evidence", params={"revision": 1}).json()
    )
    replay = submit(
        client,
        pid,
        key="replay",
        docs=[{"path": "docs/design.md", "content": "# Secret decision\nUse SQLite"}],
    ).json()
    alias = submit(
        client,
        pid,
        key="alias",
        docs=[{"path": "copy.md", "content": "# Secret decision\nUse SQLite"}],
    ).json()
    drain(sessions)
    for job in (replay, alias):
        assert client.get(f"/api/jobs/{job['id']}").json()["result"]["forgotten_skipped"] == 1
    assert not PostgresMemory(sessions, DemoEmbedder()).search(
        MemoryQuery("SQLite", project_id=pid)
    )
    with sessions() as session:
        assert session.get(Job, import_job["id"]).payload["documents"][0]["content"] == ""
        assert not session.scalar(
            select(ContextIndex).where(ContextIndex.node_id.in_([leaf["id"], derived_id]))
        )
    assert (
        client.post(
            f"/api/projects/{pid}/memory/candidates",
            json={
                "uri": first["uri"],
                "title": "Replay",
                "content": "Use SQLite",
                "evidence_ids": first["evidence_ids"],
                "idempotency_key": "after-forget",
            },
        ).status_code
        == 409
    )


def test_inflight_vectors_and_summary_cannot_restore_forgotten_source(backend):
    _, sessions = backend
    pid, leaf, _ = imported(backend)
    memory = PostgresMemory(sessions)
    job_id, token = claim(sessions)

    class ForgetDuringEmbedding(DemoEmbedder):
        def embed(self, texts):
            memory.forget(leaf["uri"])
            return super().embed(texts)

    assert process(sessions, job_id, token, ForgetDuringEmbedding())
    with sessions() as session:
        assert session.get(Job, job_id).result["stale_skipped"] > 0
    drain(sessions)
    assert not memory.search(MemoryQuery("pgvector", project_id=pid))


def test_extraction_requires_explicit_notation_and_application(backend):
    client, sessions = backend
    pid, _leaf, _ = imported(
        backend, "# Project\nDecision[database]: Use PostgreSQL\nOrdinary conversation"
    )
    drain(sessions)
    candidates = client.get(f"/api/projects/{pid}/memory/candidates").json()
    assert (
        len(candidates) == 1
        and candidates[0]["status"] == "pending"
        and candidates[0]["job_id"] is None
    )
    assert "decisions/database.md" in candidates[0]["uri"]
    approval = client.post(f"/api/memory/candidates/{candidates[0]['id']}/apply").json()
    again = client.post(f"/api/memory/candidates/{candidates[0]['id']}/apply").json()
    assert again["job_id"] == approval["job_id"]
    drain(sessions)
    with sessions() as session:
        assert session.get(MemoryCandidate, candidates[0]["id"]).status == "applied"


def test_summary_stale_snapshot_and_failure_are_fenced(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend)
    run_once(sessions)  # initial index
    job_id, token = claim(sessions)

    class Changed(ExtractiveSummarizer):
        def summarize(self, text, heartbeat):
            heartbeat()
            with sessions.begin() as session:
                node = session.get(ContextNode, leaf["id"])
                record_version(session, node)
                node.content, node.revision = "New source", node.revision + 1
            return {"abstract": "Old generated abstract", "overview": "Old generated overview"}

    assert process(sessions, job_id, token, summarizer=Changed())
    with sessions() as session:
        assert session.get(Job, job_id).result["stale_skipped"] == 1
        assert session.get(ContextNode, leaf["id"]).abstract != "Old generated abstract"
    submit(
        client,
        pid,
        key="summary-retry",
        docs=[{"path": "docs/design.md", "content": "Another source"}],
    )
    run_once(sessions)
    run_once(sessions)
    job_id, token = claim(sessions)

    class Broken(ExtractiveSummarizer):
        def summarize(self, text, heartbeat):
            raise RuntimeError("secret-token")

    assert not process(sessions, job_id, token, summarizer=Broken())
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "queued" and "secret-token" not in job.error


def test_compatible_summary_maps_long_documents_and_rejects_invalid_schema(monkeypatch):
    import httpx

    inputs = []

    def post(_client, url, *, headers, json):
        inputs.append(json["messages"][1]["content"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"abstract":"Summary","overview":"Grounded overview"}'
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    result = CompatibleSummarizer("http://localhost/v1", "fixture").summarize(
        "A" * 7000, lambda: None
    )
    assert result["abstract"] == "Summary" and len(inputs) > 2 and max(map(len, inputs)) <= 6000

    def invalid(_client, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "{}"}}]},
        )

    monkeypatch.setattr(httpx.Client, "post", invalid)
    with pytest.raises(ValueError, match="schema"):
        CompatibleSummarizer("http://localhost/v1", "fixture").summarize("Source", lambda: None)


def test_model_candidates_reject_fabricated_quotes_and_remain_pending(backend, monkeypatch):
    import json

    import httpx

    client, sessions = backend
    pid, leaf, _ = imported(backend, "# Database\nUse PostgreSQL for project storage.")
    proposal = {
        "key": "database",
        "kind": "episodic",
        "content": "Use PostgreSQL",
        "quote": "Use PostgreSQL for project storage.",
    }

    def post(_client, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": json.dumps({"candidates": [proposal]})}}]},
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    extractor = CompatibleExtractor("http://localhost/v1", "fixture")
    while run_once(sessions, extractor=extractor):
        pass
    candidates = client.get(f"/api/projects/{pid}/memory/candidates").json()
    assert len(candidates) == 1 and candidates[0]["job_id"] is None
    quotes = client.get(f"/api/memory/{leaf['id']}/evidence").json()
    assert any(
        e["quote"] == proposal["quote"] and e["id"] in candidates[0]["evidence_ids"] for e in quotes
    )
    proposal["quote"] = "Fabricated support"
    with pytest.raises(ValueError, match="grounded quote"):
        extractor.extract("# Database\nUse PostgreSQL for project storage.", lambda: None)


def test_populated_week3_migration_preserves_source_and_refuses_forget_downgrade(backend):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    client, sessions = backend
    engine = sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires real Alembic PostgreSQL migration")

    def migrate(revision, *, downgrade=False):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            config.attributes["version_table_schema"] = connection.scalar(
                text("SELECT current_schema()")
            )
            (command.downgrade if downgrade else command.upgrade)(config, revision)

    migrate("0003_si_namespace", downgrade=True)
    pid = project(client)
    with sessions.begin() as session:
        node = ContextNode(
            project_id=pid,
            uri=f"si://users/default/projects/{pid}/resources/legacy.md",
            title="Legacy",
            kind="resource",
            content="Preserved source",
            abstract="Old summary",
            overview="Preserved source",
        )
        session.add(node)
        session.flush()
        node_id = node.id
        session.execute(
            text(
                "INSERT INTO context_indexes(id,created_at,node_id,tier,revision,body) VALUES (:id,CURRENT_TIMESTAMP,:node,'l2',1,'Preserved source')"
            ),
            {"id": node_id, "node": node_id},
        )
    migrate("head")
    versions = client.get(f"/api/memory/{node_id}/versions").json()
    assert versions[0]["snapshot"]["content"] == "Preserved source" and versions[0]["revision"] == 1
    assert client.get(f"/api/memory/{node_id}").json()["content"] == "Preserved source"
    assert client.post(f"/api/projects/{pid}/context/reindex").status_code == 202
    assert client.get(f"/api/memory/{node_id}/evidence").json()[0]["quote"] == "Preserved source"
    assert client.post(f"/api/memory/{node_id}/forget", json={}).status_code == 200
    with pytest.raises(DBAPIError, match="forget barriers"):
        migrate("0003_si_namespace", downgrade=True)
    assert client.get(f"/api/memory/{node_id}").json()["content"] == ""
