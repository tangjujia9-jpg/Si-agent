# Status

**What is true right now.** Rewritten whole when it changes, never appended to
— a status file that grows is a changelog, and git already is one.

Read this before opening a PR: it records known problems and fixes in flight.

**Last updated:** 2026-10-22

---

## What works

Si-agent's optional Copilot API, PostgreSQL schema, import worker, and React
context console run through project/import/index/search/inspection. Scoped tier
retrieval combines full-text ranking and pgvector, with configured embeddings
or explicitly labeled demo vectors. Chunk evidence, historical revisions, candidate
actions/conflicts, optional model enrichment and durable logical forget are implemented.
The project Worker now runs an independent provider-neutral Loop with scoped
memory, budgets, operation approvals, outbox and durable checkpoints. OpenAI,
Anthropic and compatible adapters, optional Jev decisions, Langfuse OTLP export
and a callable LangGraph workflow adapter are implemented. React chat/SSE, operation
approvals, candidate review, memory inspection and Run Trace are implemented.
The Chinese project/thread workspace now supports durable model text streaming,
inline tool progress/approval, folder-linked read-only tools and snapshot imports.
Sources and memory open in drawers; `/monitor` provides an independent project
trace/worker view. Working context now uses checkpointed semantic summaries, exact
constraint quotes, durable result paging and per-attempt usage settlement. Migration 0009
stores source-dependent summaries; [acceptance](copilot-context-continuity-release.md) records limits. Development tools remain pending.
The local API/worker now use PostgreSQL 16 / pgvector 0.8.2;
the original Copilot SQLite database remains backed up. Migration 0008 adds folder
association. See [copilot-workspace-release.md](copilot-workspace-release.md).
The product console defaults to Chinese; [copilot-console.zh-CN.md](copilot-console.zh-CN.md)
describes its controls and current limits. Model identifiers, source content and raw trace fields retain their original values.
Live hy3 text/tool/vision/stream probes and Qwen 1024-dimensional embeddings
have passed small-sample checks. Import, evidence-backed correction and cross-thread
recall passed locally. A reproducible synthetic SQLite lexical/hybrid baseline is
available in [copilot-week7a-release.md](copilot-week7a-release.md); independent label
review, PostgreSQL quality measurement, retention scoring and live Langfuse acceptance
remain pending. Jev is disabled for this baseline. Mixed-dimension vector storage
uses migration 0007 with model/dimension guards; production ANN indexes remain absent.
The unified-memory milestone adds per-run retrieval reuse, scoped flat hybrid search,
incremental user-message proposals, separate representation revisions and derived-memory
review flags. Context-assisted proposals and user-evidenced corrections are implemented;
legacy CLI migration and semantic conflict automation remain pending.
See [copilot-unified-memory-release.md](copilot-unified-memory-release.md) for scope.
See [copilot-week5-release.md](copilot-week5-release.md) for verification limits
and [copilot-backend.md](copilot-backend.md) for startup commands.

The four pillars run: the loop, memory (semantic + episodic + procedural with
a retrieval gate), tools, and both eval tiers. `waku`, `waku dashboard`,
`waku voice`, `waku telegram`, `waku discord`, `waku brief` and
`waku connect google` all start.

The upstream baseline reports **950 deterministic evals offline**, with no API key; 60 more are live
evals that skip without one. CI runs the offline tier on every PR along with
ruff, the skills validator, and a check that `.env.example` still matches the
integrations registry.

**0.1.8 is on PyPI and on GitHub Releases.** Pushing a `v*` tag publishes to
both, so the repo's "Latest" release always matches `pip install waku-agent`.

**The dashboard uses the Waku Memory design system**, and
`test_design_system.py` keeps it from drifting. See
[context/design-system.md](context/design-system.md).

## Known broken

Nothing here is a surprise. If you hit one of these, the issue exists.

| What | Where | Fix in flight |
|---|---|---|
| The model picker offers OpenAI models that 404 on use | #137 | #178 |
| GPT-5.6 tool calls fail on Chat Completions | — | #146 |
| OpenCode Zen fails with a rate-limit error | #112 | #113 |
| Google Calendar sign-in has no bundled OAuth client, so `waku connect google` needs your own `.waku/credentials.json` | — | — |

**Providers are the recurring theme.** Three of the items above are one
provider or another, and there is no single place that says which providers
are known-good today. Until there is, treat the model picker as a list of
things that *might* work.

## What is deliberately not built

Not a framework, not multi-agent, not production — true of `waku/`, see
[architecture.md](architecture.md). `hosted/` runs that same code as a
deployment instead; spec 001 designs it, and none of it exists in this repo
yet.

Additionally, and worth stating because people ask:

- **No Windows CI.** The Windows bugs so far (#140, #141, both fixed) were
  found by contributors, not by us. Every Windows claim in this repo is
  untested in CI. The Copilot backend and console have local Windows validation.
- **The judge evals are not in CI.** `make gate` runs deterministic evals at
  100% plus a judge threshold, and CI runs only the first half. The judge tier
  needs an API key, which CI does not have.
- **No upstream picker smoke check.** The optional Copilot layer now has an
  explicit synthetic model probe script; it does not validate the upstream picker
  or silently discover provider capabilities.

## Open questions

1. **Where the memory pillar ends and Waku Memory begins.** This repo's memory
   is local, single-machine, and yours. Waku Memory is the same memory across
   several agents, and it is a paid hosted service. Both are true and the
   README does not yet say either plainly, so a reader has to work out the
   difference alone. Hosted waku (`hosted/`, spec 001) adds a third case: a
   tenant's memory lives on the operator's VM, not the person's own machine,
   but it is still that one tenant's own directory, not Waku Memory. What
   hosted waku keeps there is not written down yet; a later task in spec 001
   records it once `hosted/` exists.

## Not in the repo

Deliberately absent, so nobody goes looking:

- Filming and demo notes — production material, not product documentation
- Session handoffs — this file replaces them
- Plans and specs — they belong with the work, not in `docs/`
