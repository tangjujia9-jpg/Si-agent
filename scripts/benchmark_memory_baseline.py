"""Measure the current SQLite FTS5 memory baseline without touching user data.

Run from a checkout with ``python scripts/benchmark_memory_baseline.py``.
The benchmark uses an in-memory database and intentionally captures today's
keyword-only behavior before the pgvector memory is introduced.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Executing a script by path puts ``scripts/`` on sys.path, not the checkout
# root. Add the root explicitly so this benchmark works without installation.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from waku.db import SCHEMA
from waku.memory.semantic.store import SqliteFactStore


CASES = (
    ("exact keywords", "morning meetings", True),
    ("paraphrase", "early-day syncs", False),
    ("unicode exact", "Сергей", True),
    ("empty query", "", False),
)


def run_baseline() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    store = SqliteFactStore(conn)
    store.add("alex", "Alex prefers morning meetings.")
    store.add("sergey", "Сергей любит плавать.")

    results = []
    for name, query, expected_hit in CASES:
        hits = store.search(query, top_k=4)
        results.append({
            "name": name,
            "query": query,
            "hit_count": len(hits),
            "expected_hit": expected_hit,
            "hits": hits,
        })
    return {"backend": "sqlite-fts5", "cases": results}


def main() -> None:
    print(json.dumps(run_baseline(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
