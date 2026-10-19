"""Explicit, bounded vector cache for synthetic evaluations, never user memory."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from waku.memory.embeddings import DIMENSIONS, validate_vectors


class EmbeddingBudgetExceeded(RuntimeError):
    pass


class BudgetedEmbedder:
    """Reserve quota durably before each attempt; failed calls still consume quota.

    Limits apply cumulatively to this cache/model identity, across repeated runs.
    No raw inputs or credentials are stored. Synthetic-only vectors stay in an
    ignored artifact directory, separate from product forget semantics.
    """

    def __init__(
        self, delegate, cache: Path, *, max_requests=40, max_texts=200,
        max_characters=40000, batch_size=4,
    ):
        if min(max_requests, max_texts, max_characters, batch_size) < 1:
            raise ValueError("embedding limits must be positive")
        self.delegate, self.model_id = delegate, delegate.model_id
        self.dimensions = getattr(delegate, "dimensions", DIMENSIONS)
        self.cache = Path(cache)
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.limits = (max_requests, max_texts, max_characters)
        self.batch_size = min(batch_size, getattr(delegate, "batch_size", batch_size))
        self.stats = {"requests": 0, "texts": 0, "characters": 0, "cache_hits": 0, "network_ms": 0.0}
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS vectors (model TEXT, digest TEXT, value TEXT, PRIMARY KEY(model,digest))")
            db.execute("CREATE TABLE IF NOT EXISTS usage (model TEXT PRIMARY KEY, requests INTEGER, texts INTEGER, characters INTEGER)")

    def _connect(self):
        return sqlite3.connect(self.cache, timeout=15)

    def _digest(self, text):
        return hashlib.sha256(text.encode()).hexdigest()

    def totals(self):
        with self._connect() as db:
            row = db.execute("SELECT requests,texts,characters FROM usage WHERE model=?", (self.model_id,)).fetchone()
        return dict(zip(("requests", "texts", "characters"), row or (0, 0, 0), strict=True))

    def _reserve(self, texts):
        cost = (1, len(texts), sum(map(len, texts)))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT requests,texts,characters FROM usage WHERE model=?", (self.model_id,)).fetchone() or (0, 0, 0)
            total = tuple(a + b for a, b in zip(row, cost, strict=True))
            if any(a > b for a, b in zip(total, self.limits, strict=True)):
                raise EmbeddingBudgetExceeded("evaluation embedding quota reached; cached results remain available")
            db.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?)", (self.model_id, *total))
        for key, value in zip(("requests", "texts", "characters"), cost, strict=True):
            self.stats[key] += value

    def embed(self, texts):
        unique = dict.fromkeys(texts)
        results, missing = {}, []
        with self._connect() as db:
            for text in unique:
                row = db.execute("SELECT value FROM vectors WHERE model=? AND digest=?", (self.model_id, self._digest(text))).fetchone()
                if row:
                    results[text] = validate_vectors([json.loads(row[0])], 1, self.dimensions)[0]
                else:
                    missing.append(text)
        self.stats["cache_hits"] += len(texts) - len(missing)
        for offset in range(0, len(missing), self.batch_size):
            batch = missing[offset:offset + self.batch_size]
            self._reserve(batch)
            started = time.perf_counter()
            try:
                vectors = validate_vectors(self.delegate.embed(batch), len(batch), self.dimensions)
            finally:
                self.stats["network_ms"] += (time.perf_counter() - started) * 1000
            with self._connect() as db:
                for text, vector in zip(batch, vectors, strict=True):
                    db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?)", (self.model_id, self._digest(text), json.dumps(vector)))
                    results[text] = vector
        return [results[text] for text in texts]
