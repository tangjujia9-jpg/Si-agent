# Si-agent delivery milestones

The product retains Waku's local loop while building a project-scoped memory
service, inspectable evidence, and a recoverable runtime. Weeks 1 and 2 established
contracts and persistent document ingestion. Week 3 adds tier indexes, hybrid
retrieval and cited context. The remaining milestones are:

| Milestone | Deliverables | Reviewable outcome |
|---|---|---|
| Week 4: memory ingestion and consolidation | Long-document chunks, generated summaries, candidates/evidence/versions, conflicts, tombstones, bounded embedding retries | A changed decision retains auditable history; forgetting prevents resurrection |
| Week 5: runtime, providers and tracing | Provider-neutral loop integration, validated tools, outbox/operation IDs, LangGraph adapter, Langfuse export | One project turn has a complete tool/retrieval/model trace and recoverable states |
| Week 6: interactive console | Chat/SSE, memory inspector, run trace, model settings | Users answer a project question and open its source/trace from the reply |
| Week 7: adapters and evaluation | FTS5/hybrid comparison, external adapter experiments, retrieval/agent metrics, Eval Lab | Reproducible datasets report quality, scope leakage, staleness, latency and cost |
| Week 8: portfolio release | README/demo, CI, recovery/security/license review, benchmark report | A reviewer reproduces the demo in ten minutes |

## Langfuse integration decision

Langfuse is the preferred observability backend for the week 5 runtime milestone.
The domain event protocol and evaluation contracts remain owned by Si-agent.
Langfuse provides observation storage and exploration through an optional adapter;
running the product without Langfuse must continue to work.

The implementation must cover:

- One root per run, associated with project/user/session/run IDs and model identity.
- Real spans that begin before and end after each operation, including failed
  attempts, retries, cancellation and timeout. Completion-only event spans cannot
  represent operation durations accurately.
- Retrieval children for gate/scope, query embedding, L0 selection, L1 refinement,
  lexical/vector candidates, fusion/reranking, L2 expansion and context compilation.
- Index/ingestion jobs correlated by job IDs, node IDs, revision and evidence IDs.
- Provider calls outside the main loop, including summaries, consolidation and judges.
- Structured ToolResult states and operation IDs; estimated cost distinguished
  from measured usage/actual charges.
- Explicit collection policy and redaction before persistence/export. Raw source
  and prompt collection must be configurable; credentials never enter events.
- Bounded asynchronous export, flush on shutdown, and failures isolated from
  business execution. SDK or OTel transport/auth mapping must be tested against
  the pinned Langfuse integration rather than assuming any OTLP URL works.
- Scores attached to the correct run/evidence, with evaluator/version/model and
  dataset identity. Online checks remain separate from runtime budget enforcement.

Use fake/in-memory sinks for deterministic tests and an opt-in self-hosted
Langfuse Compose profile for integration tests. Acceptance checks must verify
parent/child correlation, actual duration ranges, redaction, concurrent runs,
exporter failure isolation, and token totals across auxiliary calls. Langfuse
credentials belong in backend configuration and never in React bundles.
