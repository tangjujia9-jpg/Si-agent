# Project memory retrieval

Si-agent retrieves project-scoped evidence through `PostgresMemory`, the week 3
read/write adapter for `MemoryPort`. The existing local Waku memory and loop
keep their behavior. Agent chat integration follows the runtime milestone.

## Import and indexing

An import transaction writes original documents, refreshes directory previews
and lexical tier indexes, and enqueues a vector index job. Its result includes
`index_job_id`; a completed import does not mean embeddings have finished.
The worker embeds outside database transactions, then checks its lease token
and each representation's revision/body before committing vectors. A newer
document revision invalidates previous vectors immediately. Failed embedding
jobs retry through the existing leased job runner; lexical search remains usable.
The worker renews its lease between bounded embedding batches and stops making
requests if another worker owns the job.

`context_indexes` stores one representation per node/tier:

| Tier | Leaf representation | Directory representation |
|---|---|---|
| L0 | First nonempty source line, at most 240 characters | Child abstracts/titles, at most 600 characters |
| L1 | First 1,200 source characters | Child overviews, at most 6,000 characters |
| L2 | Full original source | No detail index |

These are extractive previews, not model-generated semantic summaries. Vector
inputs include the title and at most 6,000 characters of each representation.
Long-document chunking and generated summaries belong to week 4. Lexical L2
search indexes the full source and centers returned snippets around query terms.

## Configure embeddings

Native API/worker processes default to `SI_EMBEDDING_BACKEND=none`, which enables
lexical retrieval without making external requests. Compose defaults to
`hash-demo`, which exercises pgvector/RRF without credentials. Hash vectors
match token overlap; they do not understand paraphrases. The API and console
display this limitation. Tests use a separate scripted semantic oracle to prove
that a vector-only match reaches the result; this is not a quality benchmark.

To use an explicitly configured backend, set the same values for API and worker:

```powershell
$env:SI_EMBEDDING_BACKEND = 'openai-compatible'
$env:SI_EMBEDDING_BASE_URL = 'http://127.0.0.1:11434/v1'
$env:SI_EMBEDDING_MODEL = 'your-1536-dimensional-embedding-model'
$env:SI_EMBEDDING_API_KEY = 'your-backend-key-if-required'
```

The endpoint must support `POST /embeddings` and return 1,536-dimensional,
finite, nonzero vectors. The current schema deliberately rejects other dimensions.
The adapter sends batches of at most 16 texts with a 20-second request timeout.
Credentials stay on the backend. Configuring a remote endpoint sends source
previews and search queries to that endpoint. No embedding request runs at import
or installation time. Model identity includes the endpoint and model name, so
vectors from different providers cannot silently mix.

Changing models requires reindexing existing projects. After upgrading week 2,
existing sources need a reindex too; migrations preserve their original content:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/PROJECT_ID/context/reindex
```

Reindex rebuilds extractive previews/lexical rows and queues vector work. Check
the returned job through `GET /api/jobs/{id}`. Matching vectors are reused;
model/endpoint changes regenerate them. A backend that changes weights while
retaining both endpoint and model name should publish a new model identifier.

## Retrieval stages

`GET /api/memory/search?project_id=PROJECT_ID&q=pgvector&tiers=l2`
returns structured hits and compiled context. Scope is mandatory: the adapter
checks project ownership before a remote query embedding call. Every candidate
query filters project, active status, index revision, and the half-open validity
interval `[valid_from, valid_to)` at `as_of` or now.

1. The backend embeds the query when configured; failure falls back to lexical.
2. L0 hybrid retrieval selects up to six directories.
3. L1 retrieval refines those directories and their children; an empty branch
   falls back to global directory overviews.
4. Requested leaf tiers search selected parents and the full project. Global
   rescue prevents a truncated overview from hiding matching L2 content.
5. PostgreSQL combines `tsvector`/`ts_rank_cd` and exact pgvector cosine search.
   Lexical terms use Unicode words and CJK unigrams/bigrams. This is PostgreSQL
   full-text ranking, not BM25. Cosine similarity must exceed 0.2.
6. Reciprocal rank fusion uses `1 / (60 + rank)` per branch and a small directory
   bonus. The backend deduplicates by URI/node and prefers a deeper tier on ties.
7. The context compiler selects snippets within a conservative UTF-8 byte budget.
   It preserves whole URI headers and UTF-8 boundaries, and returns only evidence
   that actually fits. Provider-specific token/window checks remain future work.

Exact vector search avoids ANN's filtered-recall pitfalls at this project size.
The lexical index uses GIN. Larger corpora need measured latency/recall before
introducing HNSW, rerankers, or a different segmentation strategy. RRF scores
are ranking signals, not probabilities. There is no paid cross-encoder reranker
or LLM retrieval gate in this milestone.

The response includes `hits`, `context`, `estimated_tokens`, `truncated`,
`embedding_model`, `warnings`, and `stages`. A hit carries its URI, tier, source,
scope, validity and originating event IDs. Stage metadata contains candidate
counts and observed durations, without query/document bodies. These values
prepare later tracing; they are not persistent Langfuse traces.

`as_of` filters currently active records. It cannot reconstruct overwritten
content or historical supersession states; version snapshots arrive in week 4.

## Memory writes

`write(MemoryWrite)` validates canonical project-local URIs, creates missing
directory parents, serializes writes with a project lock, and persists an
idempotency receipt. Reusing a key with another payload fails. Evidence IDs are
retained. A same-URI update increments revision; an explicit `supersedes_id`
must refer to another node in the same project and retires that node from search.

`get()` and `list_children()` respect user scope. `forget()` deliberately raises
`NotImplementedError`: setting a retracted flag alone could permit old chat or
imports to restore forgotten data. Week 4 implements durable tombstones before
exposing a forget API. Full historical versions, evidence tables, candidate
extraction, and metadata persistence beyond IDs/revision are also week 4 work.

SQLite supports the same public retrieval behavior for offline tests, but its
lexical/vector calculations are Python fallbacks. Tests against PostgreSQL run
real migrations, `tsvector` and pgvector SQL in isolated schemas.
