"""Quota, mixed-dimension isolation, and reference labels need no live services."""

import json
from copy import deepcopy

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("pgvector")

from sqlalchemy import select

from evals.deterministic.test_copilot_backend import backend  # noqa: F401
from evals.embedding_budget import BudgetedEmbedder, EmbeddingBudgetExceeded
from evals.project_memory import evaluate, index_corpus, load_dataset, ranking_metrics, seed
from waku.memory.embeddings import CompatibleEmbedder, DemoEmbedder, validate_vectors
from waku.memory.port import MemoryQuery
from waku.memory.postgres import PostgresMemory
from waku.storage.models import ContextIndex


class Fake1024:
    dimensions = 1024
    model_id = "fixture:1024"
    batch_size = 4

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0] + [0.0] * 1023 for _ in texts]


def test_quota_survives_restart_and_duplicate_texts_do_not_spend(tmp_path):
    fake = Fake1024()
    cache = tmp_path / "vectors.sqlite"
    limited = BudgetedEmbedder(fake, cache, max_requests=1, max_texts=2)
    assert len(limited.embed(["a", "a", "b"])) == 3
    assert fake.calls == [["a", "b"]]
    restarted = BudgetedEmbedder(fake, cache, max_requests=1, max_texts=2)
    assert restarted.embed(["a"])[0][0] == 1
    with pytest.raises(EmbeddingBudgetExceeded):
        restarted.embed(["c"])
    assert len(fake.calls) == 1
    assert restarted.totals()["texts"] == 2


def test_failed_attempt_reserves_budget_before_network_call(tmp_path):
    class Broken(Fake1024):
        def embed(self, texts):
            raise RuntimeError("unavailable")

    limited = BudgetedEmbedder(Broken(), tmp_path / "cache.sqlite", max_requests=1)
    with pytest.raises(RuntimeError, match="unavailable"):
        limited.embed(["x"])
    with pytest.raises(EmbeddingBudgetExceeded):
        limited.embed(["x"])
    assert limited.totals()["requests"] == 1


def test_models_do_not_share_cached_vectors_and_character_limit_is_enforced(tmp_path):
    cache = tmp_path / "cache.sqlite"
    fake = Fake1024()
    limited = BudgetedEmbedder(fake, cache, max_characters=3)
    limited.embed(["abc"])
    with pytest.raises(EmbeddingBudgetExceeded):
        limited.embed(["d"])
    other = Fake1024()
    other.model_id = "different-model:1024"
    BudgetedEmbedder(other, cache).embed(["abc"])
    assert other.calls == [["abc"]]


def test_vectors_reject_silent_padding_and_dimension_changes():
    assert len(validate_vectors([[1] * 1024], 1, 1024)[0]) == 1024
    with pytest.raises(ValueError, match="1024"):
        validate_vectors([[1] * 1536], 1, 1024)
    with pytest.raises(ValueError):
        CompatibleEmbedder("http://local/v1", "model", dimensions=0)
    assert CompatibleEmbedder("http://local/v1", "model").model_id != CompatibleEmbedder("http://local/v1", "model", dimensions=1536).model_id


def test_embedding_dimensions_are_sent_and_wrong_endpoint_output_rejected(monkeypatch):
    import httpx

    captured = []

    def post(_client, url, *, headers, json):
        captured.append(json)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": [{"index": i, "embedding": [1.0] + [0.0] * 1023} for i in range(len(json["input"]))]})

    monkeypatch.setattr(httpx.Client, "post", post)
    configured = CompatibleEmbedder("http://local/v1", "model", dimensions=1024, batch_size=1)
    assert len(configured.embed(["a", "b"])) == 2
    assert len(captured) == 2 and all(p["dimensions"] == 1024 for p in captured)
    with pytest.raises(ValueError, match="1536"):
        CompatibleEmbedder("http://local/v1", "model", dimensions=1536).embed(["a"])


def test_reference_dataset_and_ranking_denominators(tmp_path):
    data, digest = load_dataset()
    assert len(data["queries"]) == 48 and len(data["retention"]) == 16 and len(digest) == 64
    assert {q["split"] for q in data["queries"]} == {"calibration", "test"}
    assert ranking_metrics(["correct"], ["wrong", "correct"], 5)["mrr"] == 0.5
    assert ranking_metrics([], ["wrong"], 5)["recall_at_k"] is None
    invalid = deepcopy(data)
    invalid["queries"][0]["gold_quotes"] = {"database": "fabricated quote"}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="gold quote"):
        load_dataset(path)


def test_1024_index_retrieval_and_mixed_vectors_are_fenced(backend):  # noqa: F811
    _, sessions = backend
    data, _ = load_dataset()
    projects, refs = seed(sessions, data)
    fake = Fake1024()
    index_corpus(sessions, projects, fake)
    with sessions.begin() as session:
        row = session.scalar(select(ContextIndex).where(ContextIndex.node_id == refs["database"], ContextIndex.tier == "l0"))
        # Deliberately corrupt the dimension under the same model identity to
        # ensure the distance operand is fenced, not merely a WHERE predicate.
        row.embedding = DemoEmbedder().embed(["old"])[0]
    result = PostgresMemory(sessions, fake).retrieve(MemoryQuery("PostgreSQL", user_id="baseline", project_id=projects["si"]))
    assert result.embedding_model == fake.model_id and result.context.hits
    assert not result.warnings
    lexical = evaluate(sessions, data, projects, refs)
    assert lexical["summary"]["queries"] == 48
    assert lexical["summary"]["scope_violation_queries"] == 0
    assert lexical["summary"]["forbidden_violation_queries"] == 0
    assert lexical["summary"]["stale_violation_queries"] == 0
    assert lexical["summary"]["evidence_integrity"] == 1


def test_dimension_migration_preserves_vectors_and_refuses_lossy_downgrade(backend):  # noqa: F811
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    _, sessions = backend
    with sessions() as session:
        engine = session.get_bind()
    if engine.dialect.name != "postgresql":
        pytest.skip("real PostgreSQL migration semantics")
    data, _ = load_dataset()
    projects, _ = seed(sessions, data)
    index_corpus(sessions, projects, DemoEmbedder())

    def migrate(target, *, downgrade=False):
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["version_table_schema"] = connection.scalar(text("SELECT current_schema()"))
            config.attributes["connection"] = connection
            (command.downgrade if downgrade else command.upgrade)(config, target)

    with sessions() as session:
        before = session.scalar(select(ContextIndex.embedding).where(ContextIndex.embedding.is_not(None)).order_by(ContextIndex.id).limit(1))
    migrate("0006_unified_memory", downgrade=True)
    migrate("head")
    with sessions() as session:
        after = session.scalar(select(ContextIndex.embedding).where(ContextIndex.embedding.is_not(None)).order_by(ContextIndex.id).limit(1))
        assert list(before) == list(after)
    index_corpus(sessions, projects, Fake1024())
    with pytest.raises(DBAPIError):
        migrate("0006_unified_memory", downgrade=True)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008_project_workspace"
        assert connection.scalar(text("SELECT vector_dims(embedding) FROM context_indexes WHERE embedding IS NOT NULL LIMIT 1")) == 1024
