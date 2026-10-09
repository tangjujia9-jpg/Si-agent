# Week 3: cited project retrieval

Si-agent now searches its project context through tier indexes and returns
inspectable, budgeted evidence. The React console exposes the same search and
source-opening workflow.

## Changes

- The PostgreSQL migration adds tier indexes and idempotent memory-write receipts.
  Existing documents remain intact and can be indexed through the reindex API.
- `PostgresMemory` implements scoped search, read, child listing and transactional
  writes. Forget explicitly waits for week 4 tombstones.
- L0/L1 directory routing prioritizes branches; global L2 rescue preserves matches
  missing from truncated previews. RRF fuses PostgreSQL full-text and cosine rankings.
- Import transactions update lexical indexes and enqueue vector jobs. External
  embeddings run outside transactions; lease/revision checks reject stale writes.
- The backend supports no-embedding mode, explicitly labeled hash demo vectors,
  and configured OpenAI-compatible 1,536-dimensional embeddings.
- Context compilation preserves complete URI citations, evidence IDs and UTF-8
  boundaries within a conservative byte budget. Retrieval responses expose stage
  counts/durations and fallback warnings.
- The React search panel displays source hits, tiers, citations, compiled context
  and stage metadata. Project changes cancel pending searches.
- The roadmap schedules Langfuse for the week 5 Harness milestone, with real span
  durations, run/job/evidence correlation, redaction, and exporter failure isolation.

## Verification

On 2026-10-09, targeted Copilot/domain/rulebook and original memory/session/graph
regressions passed locally: 98 passed, 29 skipped. After the final lease renewal
change, the new memory tests passed again: 15 passed, 12 skipped. The PostgreSQL
counterparts skip locally because Docker Desktop cannot start its inference
socket; its runtime data was not reset. CI runs these cases against real pgvector.

The React regression suite passed three cases, including cited retrieval/source
opening and cancellation on project switch. TypeScript, production Vite build,
Ruff, Compose configuration and the live API/worker/import/index/search smoke
passed. The local smoke used an isolated SQLite database with demo vectors, so
it is not evidence of PostgreSQL deployment. Browser verification confirmed the
URI citation, source inspector, evidence IDs and compiled context.

The project-scope test was mutation-checked: temporarily removing the candidate
project filter returned a second project's document and failed the test. The
filter was restored and the normal tests passed.

[GitHub CI run 37939206709](https://github.com/tangjujia9-jpg/Si-agent/actions/runs/37939206709)
passed both jobs for code commit `bbfb8eb`: `contracts-api-storage` ran the real
PostgreSQL/pgvector migration, retrieval, scope, vector-only paraphrase and worker
tests plus frontend checks; `compose-smoke` built and started the complete
container stack and passed the import/index/cited-search smoke. The local Docker
failure therefore limits local reproduction, while Linux CI verified deployment.

## Limits

Extractive previews are not generated summaries. Hash demo vectors do not measure
semantic quality. The scripted semantic test verifies vector-only routing but does
not benchmark a real embedding model. Long documents have full lexical coverage
and only their first 6,000 representation characters embedded until chunking arrives.

Historical snapshots, durable forget, autonomous consolidation, original loop/chat
integration, and persistent Langfuse export remain later milestones. Current active
records are validity-filtered; `as_of` does not reconstruct overwritten history.

See [memory retrieval](copilot-memory.md), [startup](copilot-backend.md), and
[remaining milestones](copilot-roadmap.md).
