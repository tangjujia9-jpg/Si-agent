"""Offline contract tests for the Project Copilot architecture."""

from datetime import datetime, timezone

import pytest

from waku.domain import Budget, MemoryHit, MemoryNode, RunContext, ToolResult
from waku.memory.port import MemoryQuery
from waku.providers import ModelCapabilities, ModelRef
from scripts.benchmark_memory_baseline import run_baseline


def test_run_context_carries_scope_and_hard_limits():
    context = RunContext(
        run_id="run-1",
        user_id="me",
        project_id="waku",
        session_id="session-1",
        model="anthropic/claude",
        budget=Budget(max_iterations=3, max_tokens=900, max_tool_calls=4),
    )

    assert context.scope() == {"user_id": "me", "project_id": "waku"}
    assert context.budget.max_tool_calls == 4


@pytest.mark.parametrize("kwargs", [
    {"max_iterations": 0},
    {"max_tokens": 0},
    {"max_tool_calls": -1},
    {"timeout_seconds": 0},
])
def test_budget_rejects_unusable_limits(kwargs):
    with pytest.raises(ValueError):
        Budget(**kwargs)


def test_tool_result_is_serializable_and_preserves_partial_state():
    result = ToolResult(
        status="partial",
        message="local write succeeded; calendar sync is pending",
        operation_id="op-1",
        resource_id="event-1",
        completed_steps=("sqlite",),
        pending_steps=("ics", "google"),
        retryable=True,
    )

    payload = result.to_dict()
    assert payload["status"] == "partial"
    assert payload["completed_steps"] == ["sqlite"]
    assert payload["pending_steps"] == ["ics", "google"]


def test_memory_nodes_and_hits_are_citable():
    node = MemoryNode(
        uri="waku://users/me/projects/waku/decisions/provider.md",
        kind="decision",
        title="Provider decision",
        abstract="The project uses a provider-neutral model port.",
        project_id="waku",
        confidence=0.95,
        created_at=datetime.now(timezone.utc),
    )
    hit = MemoryHit(
        id="memory-1",
        uri=node.uri,
        tier="l1",
        snippet=node.abstract,
        score=0.87,
        source="project-import",
        project_scope="waku",
        evidence_ids=("event-1",),
    )

    assert hit.citation() == f"[{node.uri}]"


def test_memory_query_requires_a_real_question():
    with pytest.raises(ValueError):
        MemoryQuery(text=" ")


def test_provider_contract_is_provider_neutral():
    model = ModelRef(provider="openai-compatible", model="qwen3", kind="chat")
    capabilities = ModelCapabilities(tool_calling=True, streaming=True, context_window=32768)

    assert model.provider == "openai-compatible"
    assert capabilities.tool_calling is True
    assert capabilities.context_window == 32768


def test_fts5_baseline_is_explicit_about_keyword_and_paraphrase_behavior():
    report = run_baseline()
    cases = {case["name"]: case for case in report["cases"]}

    assert cases["exact keywords"]["hit_count"] == 1
    assert cases["paraphrase"]["hit_count"] == 0
    assert cases["unicode exact"]["hit_count"] == 1
    assert cases["empty query"]["hit_count"] == 0
