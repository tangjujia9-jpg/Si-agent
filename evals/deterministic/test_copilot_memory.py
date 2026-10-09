"""Memory retrieval invariants, with real pgvector and an explicit offline fallback."""

from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from sqlalchemy import select

from evals.deterministic.test_copilot_backend import backend as backend_fixture
from evals.deterministic.test_copilot_backend import project, submit
from waku.domain.contracts import MemoryHit, MemoryNode
from waku.memory.context import compile_context
from waku.memory.embeddings import CompatibleEmbedder, DemoEmbedder, validate_vectors
from waku.memory.indexing import refresh_indexes
from waku.memory.port import MemoryPort, MemoryQuery, MemoryWrite
from waku.memory.postgres import PostgresMemory
from waku.storage.models import ContextIndex, ContextNode, Job
from waku.workers.runner import claim, process, run_once

backend = backend_fixture


def imported(backend, content="# Architecture\nUse PostgreSQL and pgvector.", name="Alpha"):
    client, sessions = backend
    pid = project(client, name)
    job = submit(client, pid, docs=[{"path": "docs/design.md", "content": content}]).json()
    run_once(sessions)
    nodes = client.get(f"/api/projects/{pid}/context/tree").json()
    leaf = next(n for n in nodes if n["kind"] == "resource")
    return pid, leaf, job


def test_scope_validity_status_and_evidence(backend):
    client, sessions = backend
    a, leaf, _ = imported(backend)
    b, _, _ = imported(backend, name="Other")
    memory = PostgresMemory(sessions)
    hits = memory.search(MemoryQuery("pgvector", project_id=a))
    assert len(hits) == 1 and hits[0].id == leaf["id"]
    assert hits[0].project_scope == a and hits[0].evidence_ids
    assert hits[0].citation() in memory.retrieve(MemoryQuery("pgvector", project_id=a)).context.text
    with pytest.raises(ValueError, match="project not found"):
        memory.search(MemoryQuery("pgvector", project_id=a, user_id="someone_else"))
    assert b != a
    with sessions.begin() as session:
        node = session.get(ContextNode, leaf["id"])
        node.valid_to = datetime(2026, 1, 2, tzinfo=UTC)
    assert not memory.search(MemoryQuery("pgvector", project_id=a))
    assert memory.search(
        MemoryQuery("pgvector", project_id=a, as_of=datetime(2026, 1, 1, tzinfo=UTC))
    )
    with sessions.begin() as session:
        session.get(ContextNode, leaf["id"]).status = "retracted"
    assert not memory.search(
        MemoryQuery("pgvector", project_id=a, as_of=datetime(2026, 1, 1, tzinfo=UTC))
    )
    assert (
        client.get("/api/memory/search", params={"project_id": "missing", "q": "x"}).status_code
        == 404
    )


class SemanticFixture:
    """A scripted semantic oracle proves vector routing without buying an API call."""

    model_id = "test:semantic-v1"

    def embed(self, texts):
        return [
            [1.0, 0.0] + [0.0] * 1534
            if any(t in s.lower() for t in ("access keys", "credential"))
            else [0.0, 1.0] + [0.0] * 1534
            for s in texts
        ]


def test_vector_only_paraphrase_and_model_isolation(backend):
    _, sessions = backend
    pid, leaf, _ = imported(backend, "# Security\nReplace access keys monthly.")
    semantic = SemanticFixture()
    while run_once(sessions, semantic):
        pass
    query = MemoryQuery("credential rotation", project_id=pid, tiers=("l2",))
    assert not PostgresMemory(sessions).search(query)
    result = PostgresMemory(sessions, semantic).retrieve(query)
    assert [h.id for h in result.context.hits] == [leaf["id"]]
    assert any(s.get("vector", 0) for s in result.stages)
    # Different embedding weights must never be mixed, even at the same dimension.
    semantic.model_id = "test:semantic-v2"
    assert not PostgresMemory(sessions, semantic).search(query)


def test_import_revisions_invalidate_vectors_and_old_keywords(backend):
    client, sessions = backend
    pid, leaf, _ = imported(backend, "# Storage\nUse SQLite.")
    while run_once(sessions, DemoEmbedder()):
        pass
    submit(
        client,
        pid,
        key="new",
        docs=[{"path": "docs/design.md", "content": "# Storage\nUse PostgreSQL."}],
    )
    run_once(sessions)
    memory = PostgresMemory(sessions, DemoEmbedder())
    assert not memory.search(MemoryQuery("SQLite", project_id=pid, tiers=("l2",)))
    assert memory.search(MemoryQuery("PostgreSQL", project_id=pid, tiers=("l2",)))
    with sessions() as session:
        rows = list(session.scalars(select(ContextIndex).where(ContextIndex.node_id == leaf["id"])))
        assert len(rows) == 3 and all(r.embedding is None and r.revision == 2 for r in rows)


def test_directory_rescue_chinese_and_overview_only(backend):
    client, sessions = backend
    pid, leaf, _ = imported(
        backend, "# 项目说明\n" + "普通内容\n" * 1500 + "使用向量数据库检索长期记忆。"
    )
    result = PostgresMemory(sessions).retrieve(
        MemoryQuery("向量数据库", project_id=pid, tiers=("l2",), max_tokens=2000)
    )
    assert len(result.context.hits) == 1 and result.context.hits[0].id == leaf["id"]
    assert "向量数据库" in result.context.text
    assert any(s["stage"] == "l2_leaves" for s in result.stages)
    response = client.get(
        "/api/memory/search",
        params={"project_id": pid, "q": "项目说明", "include_details": "false"},
    )
    assert response.status_code == 200
    assert all(h["tier"] != "l2" for h in response.json()["hits"])


def test_budget_never_breaks_citations_or_utf8():
    hit = MemoryHit(
        "n",
        "waku://users/default/projects/p/resources/a.md",
        "l2",
        "记忆" * 500,
        0.1,
        "resource",
        evidence_ids=("job",),
    )
    compiled = compile_context([hit], 160)
    assert compiled.estimated_tokens <= 160 and compiled.truncated
    assert hit.citation() in compiled.text and "�" not in compiled.text
    assert compiled.hits[0].snippet in compiled.text
    tiny = compile_context([hit], 1)
    assert tiny.text == "" and tiny.hits == () and tiny.truncated


def test_index_fencing_and_changed_snapshot(backend):
    _, sessions = backend
    pid, leaf, _ = imported(backend)
    job_id, token = claim(sessions)
    assert not process(sessions, job_id, "old-worker", DemoEmbedder())

    class MutatingEmbedder(DemoEmbedder):
        def embed(self, texts):
            with sessions.begin() as session:
                node = session.get(ContextNode, leaf["id"])
                node.content, node.revision = "New evidence", node.revision + 1
                refresh_indexes(session, pid)
            return super().embed(texts)

    assert process(sessions, job_id, token, MutatingEmbedder())
    with sessions() as session:
        assert session.get(Job, job_id).result["stale_skipped"] >= 1
        rows = list(session.scalars(select(ContextIndex).where(ContextIndex.node_id == leaf["id"])))
        assert all(row.embedding is None for row in rows)


def test_failed_embeddings_retry_and_lexical_fallback(backend):
    _, sessions = backend
    pid, _, _ = imported(backend)

    class Broken:
        model_id = "broken"

        def embed(self, texts):
            raise RuntimeError("secret must not escape")

    job_id, token = claim(sessions)
    assert not process(sessions, job_id, token, Broken())
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "queued" and "secret" not in job.error
        assert not session.scalar(select(ContextIndex).where(ContextIndex.embedding.is_not(None)))
    result = PostgresMemory(sessions, Broken()).retrieve(MemoryQuery("pgvector", project_id=pid))
    assert result.context.hits and "Embedding unavailable" in result.warnings[0]


def test_memory_port_write_idempotency_scope_and_supersession(backend):
    _, sessions = backend
    pid, _, _ = imported(backend)
    memory = PostgresMemory(sessions)
    assert isinstance(memory, MemoryPort)
    uri = f"waku://users/default/projects/{pid}/decisions/database.md"
    node = MemoryNode(uri, "episodic", "Database decision", content="Use SQLite", project_id=pid)
    request = MemoryWrite(node, "decision-1", evidence_ids=("event-1",))
    first = memory.write(request)
    assert memory.write(request) == first
    assert memory.get(uri).source_event_ids == ("event-1",)
    assert memory.get(uri, user_id="other") is None
    assert any(n.uri == uri for n in memory.list_children(uri.rsplit("/", 1)[0]))
    with pytest.raises(ValueError, match="conflicts"):
        memory.write(
            MemoryWrite(MemoryNode(uri, "episodic", "Changed", project_id=pid), "decision-1")
        )
    new_uri = uri.replace("database.md", "database-v2.md")
    memory.write(
        MemoryWrite(
            MemoryNode(
                new_uri,
                "episodic",
                "Database decision",
                content="Use PostgreSQL",
                project_id=pid,
                supersedes_id=first.id,
            ),
            "decision-2",
        )
    )
    assert memory.get(uri).status == "superseded"
    assert not memory.search(MemoryQuery("SQLite", project_id=pid, tiers=("l2",)))
    assert memory.search(MemoryQuery("PostgreSQL", project_id=pid, tiers=("l2",)))
    with pytest.raises(ValueError, match="namespace"):
        memory.write(
            MemoryWrite(
                MemoryNode(uri.replace(pid, "another"), "semantic", "bad", project_id=pid), "bad"
            )
        )
    with pytest.raises(NotImplementedError, match="tombstones"):
        memory.forget(uri)


@pytest.mark.parametrize(
    "params",
    [
        {"q": " "},
        {"q": "x", "top_k": 0},
        {"q": "x", "tiers": "wrong"},
        {"q": "x", "as_of": "2026-01-01T00:00:00"},
        {"q": "x", "max_tokens": 100000},
    ],
)
def test_api_rejects_invalid_search(backend, params):
    client, _ = backend
    pid = project(client)
    assert client.get("/api/memory/search", params={"project_id": pid, **params}).status_code == 422


def test_embedding_validation():
    for vectors in ([[float("nan")] * 1536], [[0.0] * 1536], [[1.0]], []):
        with pytest.raises(ValueError):
            validate_vectors(vectors, 1)
    assert DemoEmbedder().embed(["长期记忆"])[0] == DemoEmbedder().embed(["长期记忆"])[0]


def test_compatible_embeddings_batch_order_and_headers(monkeypatch):
    import httpx

    requests = []

    def post(_client, url, *, headers, json):
        requests.append((url, headers, json))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "data": [
                    {"index": i, "embedding": [float(i + 1)] + [0.0] * 1535}
                    for i in reversed(range(len(json["input"])))
                ]
            },
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    embedder = CompatibleEmbedder("http://localhost:1234/v1", "test", "backend-key")
    vectors = embedder.embed(["doc"] * 17)
    assert len(vectors) == 17 and vectors[0][0] == 1 and vectors[15][0] == 16
    assert len(requests) == 2 and requests[0][1] == {"Authorization": "Bearer backend-key"}
