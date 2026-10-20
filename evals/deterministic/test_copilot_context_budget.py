"""Context growth, provider rejection recovery and protocol preservation, without keys."""

import json
from dataclasses import replace

import pytest

from waku.domain.contracts import MemoryHit
from waku.providers.adapters import estimated_size
from waku.providers.contracts import ModelRequest
from waku.runtime.context_budget import PROTOCOL_RESERVE, context_length_error, fit_request
from waku.runtime.decisions import LocalDecisions


def call(number, content):
    return [
        {
            "role": "assistant",
            "content": "Reading source",
            "tool_calls": [
                {"id": number, "name": "read_memory", "arguments": {"uri": "si://source"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": number,
            "content": json.dumps(
                {
                    "status": "succeeded",
                    "operation_id": number,
                    "data": {"uri": "si://source", "content": content},
                }
            ),
        },
    ]


def test_drops_complete_old_turn_but_keeps_current_question_and_canonical_history():
    messages = [
        {"role": "user", "content": "older " * 2000},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "Read current source"},
        *call("one", "current result"),
    ]
    original = json.dumps(messages)
    plan = fit_request(
        ModelRequest(messages, system="policy", max_tokens=512),
        [],
        context_window=4000,
        remaining=20000,
    )
    assert json.dumps(messages) == original
    assert plan.request.messages == messages[2:]
    assert plan.metrics["omitted_history_messages"] == 2
    assert "not a summary" in plan.request.system
    assert estimated_size(plan.request) + 512 + PROTOCOL_RESERVE <= 4000


def test_elides_older_tools_without_orphaning_calls_or_losing_newest_exchange():
    messages = [
        {"role": "user", "content": "Compare both sources"},
        *call("old", "旧证据" * 1800),
        *call("new", "latest exact result"),
    ]
    original = json.dumps(messages)
    plan = fit_request(
        ModelRequest(messages, system="policy", max_tokens=512),
        [],
        context_window=3500,
        remaining=20000,
    )
    assert json.dumps(messages) == original
    assert plan.request.messages[-2:] == messages[-2:]
    elided = json.loads(plan.request.messages[2]["content"])
    assert elided["context_elided"] and elided["status"] == "succeeded"
    assert elided["data"]["uri"] == "si://source" and "旧证据" not in str(elided)
    assert plan.metrics["elided_tool_results"] == 1
    calls = [c["id"] for m in plan.request.messages for c in m.get("tool_calls", [])]
    results = [m["tool_call_id"] for m in plan.request.messages if m["role"] == "tool"]
    assert calls == results == ["old", "new"]


def test_reduces_evidence_with_complete_citations_and_keeps_instructions_and_tools():
    hit = MemoryHit("n", "si://source", "l2", "中文依据" * 1200, 1.0, "test", evidence_ids=("e1",))
    request = ModelRequest(
        [{"role": "user", "content": "question"}],
        system="policy",
        max_tokens=512,
        tools=[{"name": "read_memory", "input_schema": {"type": "object"}}],
    )
    plan = fit_request(request, [hit], context_window=2400, remaining=20000)
    assert plan.request.system.startswith("policy")
    assert plan.request.tools == request.tools
    assert "[si://source]" in plan.request.system and "evidence: e1" in plan.request.system
    assert "�" not in plan.request.system
    assert (
        plan.metrics["compacted"] and estimated_size(plan.request) + 512 + PROTOCOL_RESERVE <= 2400
    )
    assert "中文依据" not in str(plan.metrics)


@pytest.mark.parametrize(
    "window,remaining,code", [(2000, 20000, "context_budget"), (20000, 2000, "token_budget")]
)
def test_cannot_discard_current_question_to_hide_budget_failure(window, remaining, code):
    request = ModelRequest([{"role": "user", "content": "current " * 2000}], max_tokens=512)
    with pytest.raises(ValueError, match="^" + code + "$"):
        fit_request(request, [], context_window=window, remaining=remaining)


def test_newest_parallel_results_stay_complete():
    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "name": "read", "arguments": {}},
                {"id": "b", "name": "read", "arguments": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "a" * 3000},
        {"role": "tool", "tool_call_id": "b", "content": "b" * 3000},
    ]
    request = ModelRequest(messages, max_tokens=512)
    with pytest.raises(ValueError, match="context_budget"):
        fit_request(request, [], context_window=4000, remaining=20000)
    assert (
        fit_request(
            replace(request, max_tokens=256), [], context_window=12000, remaining=20000
        ).request.messages
        == messages
    )


def test_incomplete_history_prefix_can_be_omitted_without_dropping_current_user():
    messages = [
        {"role": "assistant", "content": "old answer " * 1000},
        {"role": "user", "content": "current"},
    ]
    plan = fit_request(
        ModelRequest(messages, max_tokens=512), [], context_window=2500, remaining=20000
    )
    assert plan.request.messages == [messages[-1]]
    assert plan.metrics["omitted_history_messages"] == 1


def test_earlier_assistant_narration_can_be_elided_without_altering_call_arguments():
    messages = [{"role": "user", "content": "Compare"}, *call("a", "first"), *call("b", "last")]
    messages[1]["content"] = "Earlier narration " * 1000
    plan = fit_request(
        ModelRequest(messages, max_tokens=512), [], context_window=3500, remaining=30000
    )
    assert plan.metrics["elided_assistant_messages"] == 1
    assert plan.request.messages[1]["tool_calls"] == messages[1]["tool_calls"]
    assert plan.request.messages[-2:] == messages[-2:]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (400, {"code": "context_length_exceeded", "message": "PRIVATE"}, True),
        (422, {"error": {"type": "prompt_too_long"}}, True),
        (400, {"message": "Maximum context length exceeded: PRIVATE"}, True),
        (400, {"code": "invalid_tools", "message": "PRIVATE"}, False),
        (401, {"code": "context_length_exceeded"}, False),
        (500, {"code": "context_length_exceeded"}, False),
    ],
)
def test_only_specific_length_errors_are_classified(status, body, expected):
    class Error(Exception):
        status_code = status

    error = Error("PRIVATE")
    error.body = body
    assert context_length_error(error) == expected


def test_local_gate_is_explicitly_conservative_not_a_model_score():
    decisions = LocalDecisions()
    assert not decisions.decide("retrieve", {"text": "你好"}).value
    result = decisions.decide("retrieve", {"text": "1 + 1"})
    assert result.value and result.source == "local-rule" and result.reason == "conservative_recall"
    assert result.usage.total_tokens == 0
