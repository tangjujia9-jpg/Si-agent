# Project Context Copilot: Week 1 Contracts

This document records the first implementation step of the Project Context
Copilot redesign. It is intentionally narrower than the final product plan:
the goal of week 1 is to make subsystem boundaries testable before changing
the existing loop or SQLite memory path.

## Product boundary

The product is a single-user, multi-project knowledge copilot. A project can
ingest documents, answer questions with inspectable evidence, and retain
versioned facts and decisions across sessions. The first release uses
PostgreSQL and pgvector as its product storage, while the current SQLite
implementation remains the local baseline and compatibility path.

## Contract layers

The new dependency-free contracts live in four places:

| Contract | Location | Responsibility |
|---|---|---|
| Domain values | `waku/domain/contracts.py` | Run identity, budgets, memory evidence, structured tool results |
| Memory port | `waku/memory/port.py` | Scoped search, inspectable nodes, idempotent writes, forget |
| Provider port | `waku/providers/contracts.py` | Model references, capabilities, completion, streaming and usage |
| FTS5 baseline | `scripts/benchmark_memory_baseline.py` | Repeatable measurements of today's local keyword retrieval |

The existing `FactStore` contract stays in place while adapters are migrated.
The richer `MemoryPort` is deliberately separate because a Project Copilot
needs URI, tier, evidence, scope, and version metadata that a formatted fact
string cannot carry.

## Harness rules

`RunContext` owns scope and hard limits. A model may suggest a tool call, but it
cannot remove the iteration, token, tool-count, deadline, approval, or retry
policy carried by the context. `ToolResult` records partial and unknown states
so later work can add an outbox without changing the public shape again.

Provider adapters return provider-neutral values. The runtime must not depend
on Anthropic or OpenAI response classes; those belong behind adapters.

## Current FTS5 baseline

Run:

```bash
python scripts/benchmark_memory_baseline.py
```

The benchmark uses an in-memory database and does not read or clear `.waku/`.
The expected baseline is:

| Case | Expected result |
|---|---|
| Exact keywords (`morning meetings`) | Hit |
| Paraphrase (`early-day syncs`) | Miss: semantic similarity is unavailable |
| Unicode exact (`Сергей`) | Hit |
| Empty query | No results |

This baseline is a measurement, not a desired product behavior. The pgvector
implementation should improve paraphrase recall while preserving scope,
evidence, and deterministic empty-query behavior.

## Next step

Week 2 added the PostgreSQL schema, project API, import worker, and source
console; see [the product guide](copilot-backend.md). The full `MemoryPort`
adapter and hybrid retrieval follow in week 3. The new product will not replace
the current Waku turn path until its memory adapter passes conformance tests
and the baseline comparison.
