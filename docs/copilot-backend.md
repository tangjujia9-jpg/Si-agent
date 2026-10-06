# Si-agent Copilot backend

Si-agent adds a single-user, multi-project product layer to the Waku core.
The week 2 release stores projects and documents in PostgreSQL, accepts durable
import jobs, and exposes a React context browser. It keeps the existing local
Waku CLI and SQLite memory path separate.

## Start the product

Docker Desktop must run Linux containers. From the repository root, run:

```bash
docker compose -f infra/compose.yaml up -d --build
```

Open `http://127.0.0.1:8080` for the console and
`http://127.0.0.1:8000/docs` for the OpenAPI interface. Compose starts
Postgres/pgvector, waits for database readiness, applies the migration once,
and then starts the API, worker, and web service. The API and web ports bind to
loopback because this release has no authentication. The database has no
published port in the product configuration.

The console can create a project, submit a document, show its job status,
browse the generated directories, and display source content and evidence.
No model credentials are required for this release.

Run the smoke test after startup:

```bash
python scripts/smoke_copilot.py
```

The test creates a fresh project. It checks readiness, duplicate submission,
worker completion, source inspection, and thread creation. It never clears
existing memory or projects.

Stop services with `docker compose -f infra/compose.yaml down`. The named
`copilot_db` volume retains the database. Do not add `-v` unless you intend to
delete that database.

## Develop without building containers

Install the optional backend and development dependencies:

```bash
uv sync --python 3.12 --extra copilot --extra dev
```

For an isolated local test database on port 55432:

```bash
docker compose -p si-agent-week2-test -f infra/compose.yaml -f infra/compose.test.yaml up -d db
```

In PowerShell, configure and migrate this test database:

```powershell
$env:SI_DATABASE_URL = 'postgresql+psycopg://si:si-local@127.0.0.1:55432/si_agent'
uv run --no-sync alembic upgrade head
uv run --no-sync uvicorn waku.server.app:create_app --factory --host 127.0.0.1 --port 8000
```

Run the worker in another terminal with the same `SI_DATABASE_URL`:

```powershell
uv run --no-sync python -m waku.workers.runner
```

Start the console in a third terminal:

```bash
cd apps/web
npm ci
npm run dev
```

The Vite development server proxies `/api` and `/ready` to port 8000.
The browser never receives database credentials.

## Storage and invariants

| Table | Purpose | Guard |
|---|---|---|
| `projects` | Workspace identity and ownership | Every API lookup checks the configured single user |
| `threads` | Project-scoped conversations | Foreign key to project |
| `messages` | Future chat and tool transcript | Composite foreign key prevents messages crossing project/thread boundaries |
| `runs` | Future execution state and trace metadata | Scoped thread reference and allowed status values |
| `context_nodes` | Inspectable directories and resources | Unique project/URI, confidence check, revision counter |
| `jobs` | Durable import requests | Unique project/idempotency key, lease token, bounded attempts |

Context nodes reserve nullable `vector(1536)` and `tsvector` columns. The current
importer does not generate embeddings or perform semantic retrieval. The vector
dimension is the initial schema choice; changing it requires a migration and a
matching embedding configuration in week 3.

The initial Alembic migration freezes its SQL. It does not import the current
ORM models, so a later model edit cannot silently rewrite migration history.
Migrations apply only to the Copilot database. They do not migrate or access
`.waku/state.db`. A downgrade removes Copilot tables and preserves the shared
vector extension.

## Import and recovery

```text
POST ingestion request
  -> validate paths, batch size, and idempotency key
  -> commit queued job (HTTP 202)
Worker
  -> claim eligible job with FOR UPDATE SKIP LOCKED
  -> commit lease token and attempt counter
  -> lock job and project
  -> create directories and upsert source documents
  -> commit documents and succeeded status together
```

The namespace is `waku://users/default/projects/{project_id}`. A project contains
`resources`, `decisions`, `tasks`, `memories`, and `sessions`. Imported document
paths extend `resources`. URI segments are encoded and ingestion never reads
arbitrary server filesystem paths.

An identical idempotency key and payload returns the existing job. Reusing a key
with different documents returns HTTP 409. A document checksum avoids repeated
updates. Changed content retains its URI and increments its revision. Revisions
are counters in this release; historical content snapshots arrive with the
memory version model in week 4.

Workers serialize imports in the same project. PostgreSQL workers skip locked
jobs so different workers can make progress. A lease expires after 60 seconds;
the processing transaction holds a row lock while applying changes. A stale
lease token cannot finish a job reclaimed by another worker.

A processing failure rolls back every document write, records a sanitized
error, and schedules an exponential retry. Jobs fail permanently after three
attempts. Killing a worker leaves its leased job reclaimable. Import, database
errors, and task state are inspectable through the API.

## API surface

| Endpoint | Behavior |
|---|---|
| `GET /health` | Process liveness without a database requirement |
| `GET /ready` | Database connectivity and migrated project table |
| `POST /api/projects` | Create a workspace |
| `GET /api/projects` | List this user's workspaces |
| `POST /api/projects/{id}/ingest` | Queue validated source documents |
| `GET /api/jobs/{id}` | Inspect status, attempts, and import result |
| `GET /api/projects/{id}/context/tree` | List scoped nodes and previews without full source content |
| `GET /api/memory/{id}` | Open the full source node |
| `POST /api/threads` | Create a scoped conversation record |

The API commits mutation transactions before sending a successful response.
HTTP validation rejects empty names, traversal paths, unsupported extensions,
duplicate paths, and document batches above 2 MB. Errors omit credentials, SQL,
and imported content. The gateway uses the fixed user `default`; callers cannot
choose another user by adding a payload field.

## Verification

```powershell
$env:SI_TEST_DATABASE_URL = 'postgresql+psycopg://si:si-local@127.0.0.1:55432/si_agent'
uv run --no-sync python -X utf8 -m pytest -q evals/deterministic/test_copilot_backend.py
uv run --no-sync ruff check waku evals scripts hosted
uv run --no-sync alembic check
```

Each PostgreSQL test creates a uniquely named test schema and applies the real
Alembic migration. Cleanup removes only that test schema. Tests verify both
SQLite and Postgres API behavior, foreign keys, atomic rollback, retry limits,
lease fencing, and PostgreSQL `SKIP LOCKED`. SQLite does not validate vector or
concurrent-locking behavior. Without `SI_TEST_DATABASE_URL`, Postgres cases skip.

The `copilot` workflow checks backend contracts against a pgvector service,
builds the React console, and runs a separate full Compose smoke test. Local
validation must distinguish a successful application test from an image pull
failure or a skipped database test.

## Next milestones

Week 3 adds semantic summaries, document chunks, hybrid retrieval, hierarchical
selection, and structured `MemoryHit` results. Week 4 adds memory candidates,
historical versions, evidence links, and forget tombstones. The current
`abstract` and `overview` values are source previews; they are not generated
semantic summaries. Messages and runs have schema support but are not yet wired
into the original Waku agent loop. Chat, SSE traces, and advanced controls
arrive after the runtime integration.
