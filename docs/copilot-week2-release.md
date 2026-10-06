# Week 2: persistent project context

Si-agent now has a working project/document import product boundary. The API
and worker use PostgreSQL, while the React console creates projects, queues
imports, observes task completion, and opens original source evidence.

## Changes

- The optional `copilot` extra adds FastAPI, SQLAlchemy, Alembic, psycopg,
  pgvector, Uvicorn, and HTTP testing support. The existing default Waku
  installation does not gain these dependencies.
- Six tables persist projects, threads, messages, runs, context nodes, and
  jobs. Foreign keys, unique constraints, and status checks enforce scope and
  lifecycle rules. Vector and lexical columns prepare the next retrieval step.
- An explicit Alembic migration owns schema changes. Frozen SQL preserves
  migration history, and the migration has no path to `.waku/state.db`.
- The FastAPI gateway supports project creation/listing, ingestion, job
  inspection, context listing, full source inspection, and thread creation.
  Storage readiness differs from process liveness.
- Import jobs use payload hashes and project-scoped idempotency keys. The
  worker uses leases, fencing tokens, PostgreSQL row locks, bounded retries,
  and a transaction spanning source writes and completion status.
- Source nodes have inspectable `waku://` URIs, directory parents, checksum
  deduplication, revision counters, and originating import job IDs. Imported
  source whitespace remains intact, including code indentation.
- The independent React/Vite/TypeScript console supports project switching,
  source submission, task polling, a context tree, and a source inspector.
  Full source content loads on demand. Fonts are bundled locally.
- Compose defines database, migration, API, worker, and web services. Persistent
  storage uses a named volume, and the product exposes only loopback web/API
  ports. The build context excludes runtime data and the separate hosted code.
- Dedicated CI validates PostgreSQL migrations and locking behavior, frontend
  behavior/build, and a full Compose smoke test.
- Ruff is now installed in the local development environment. This release
  also fixes week 1 import ordering and modern type imports found by Ruff.

## Verification on 2026-10-06

| Check | Result |
|---|---|
| New API/storage/worker tests plus domain, rulebook, memory, session, and graph regression | 118 passed, 49 skipped |
| PostgreSQL tests | Real pgvector/Postgres 16 database, real migrations, unique test schemas |
| Frontend import/poll/tree/source regression | 1 passed |
| TypeScript and production web build | Passed |
| Ruff across `waku`, `evals`, `scripts`, `hosted` | Passed |
| Alembic metadata comparison | No new upgrade operations detected |
| Compose configuration and Python lock consistency | Passed |
| Running API + worker + Postgres smoke test | Passed |
| Browser create/import/inspect and project switching | Passed |

The targeted regression skips hosted memory adapters without credentials and
the SQLite-only counterpart of the PostgreSQL locking test. PostgreSQL cases
were enabled; they were not silently replaced by SQLite tests.

Local full-image Compose startup could not complete because Docker Hub token
requests timed out. The verified local path used the cached pgvector image with
native API/worker processes and Vite. The full-image smoke job is included in CI,
but the local image build is not reported as passing.

The broader upstream deterministic suite still fails Windows coding-eval cases;
the focused run stopped after three such failures. Those pre-existing modules
were not changed in this release. No claim of a fully green upstream suite is
made here.

## What follows

This release provides storage and transport infrastructure. Semantic
summarization, embedding generation, vector/hybrid retrieval, reranking, and
the new `MemoryPort` implementation remain the next milestone. Original
assistant loop integration, SSE chat, memory consolidation, historical content
versions, and forget tombstones are also future milestones.

See [the backend guide](copilot-backend.md) for startup commands, API contracts,
job recovery details, and database testing instructions.
