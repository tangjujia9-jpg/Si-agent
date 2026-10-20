"""Bound provider input without mutating canonical messages or tool outcomes."""

import json
from dataclasses import dataclass, replace

from waku.memory.context import compile_context
from waku.providers.adapters import estimated_size
from waku.providers.contracts import ModelRequest

PROTOCOL_RESERVE = 256
OMISSION = (
    "Some earlier conversation or tool content was omitted to fit the context budget. "
    "Omitted content is unavailable, not a summary. Re-read sources if needed; do not invent it.\n"
)


@dataclass(frozen=True)
class ContextPlan:
    request: ModelRequest
    metrics: dict


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _reference_result(content):
    """Keep outcome identity and bounded locators; never invent a result summary."""
    try:
        original = json.loads(content)
    except (ValueError, TypeError):
        original = {}
    if not isinstance(original, dict):
        original = {}
    result = {
        key: original[key]
        for key in ("status", "operation_id", "resource_id", "retryable", "error_code")
        if key in original
    }
    result["context_elided"] = True
    result["message"] = "Earlier tool body omitted. Re-read the source for its contents."
    data = original.get("data", {})
    if isinstance(data, dict):
        result["data"] = {
            key: data[key]
            for key in ("path", "uri", "node_id", "revision", "next_line", "next_offset")
            if key in data and isinstance(data[key], (str, int, type(None)))
        }
        # An operation ID always locates the full durable outcome; keep a few source locators too.
        hits = data.get("hits", [])
        result["data"]["sources"] = [
            {key: hit[key] for key in ("id", "uri", "revision") if key in hit}
            for hit in (hits[:3] if isinstance(hits, list) else [])
            if isinstance(hit, dict)
        ]
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded.encode()) > 2000:
        result.pop("data", None)
    return json.dumps(result, ensure_ascii=False)


def fit_request(request, hits, *, context_window, remaining, evidence_budget=3000):
    """Preserve the current question and newest complete tool exchange, or stop.

    The input estimate is deliberately a UTF-8 upper bound, not billed token usage.
    System instructions and tool schemas are never truncated to make a request fit.
    """
    window_limit = context_window - PROTOCOL_RESERVE
    ceiling = min(window_limit, remaining)
    error = "context_budget" if window_limit <= remaining else "token_budget"
    input_limit = ceiling - request.max_tokens
    if input_limit <= 0:
        raise ValueError(error)
    messages = _copy(request.messages)
    evidence = compile_context(hits, evidence_budget).text if hits else ""
    omitted_history = elided_tools = elided_assistant = 0

    def build():
        changed = omitted_history or elided_tools or elided_assistant
        return replace(
            request,
            messages=messages,
            system=request.system
            + ("\n" + OMISSION if changed else "")
            + "\nEvidence:\n"
            + evidence,
        )

    before = estimated_size(build())
    # Remove complete prior user turns, never the current user question or current Loop.
    while estimated_size(build()) > input_limit:
        users = [i for i, message in enumerate(messages) if message["role"] == "user"]
        if users and users[0] > 0:
            cut = users[0]
        elif len(users) >= 2:
            cut = users[1]
        else:
            break
        omitted_history += cut
        messages = messages[cut:]

    latest_call = max(
        (i for i, message in enumerate(messages) if message.get("tool_calls")), default=-1
    )
    for i, message in enumerate(messages):
        if estimated_size(build()) <= input_limit:
            break
        if message["role"] != "tool" or i >= latest_call:
            continue
        content = _reference_result(message.get("content", ""))
        if len(content.encode()) < len(message.get("content", "").encode()):
            message["content"] = content
            elided_tools += 1
    for i, message in enumerate(messages):
        if estimated_size(build()) <= input_limit:
            break
        if i < latest_call and message.get("tool_calls") and len(message.get("content", "")) > 240:
            message["content"] = (
                "Earlier assistant narration omitted; tool call identities retained."
            )
            elided_assistant += 1

    while estimated_size(build()) > input_limit and evidence:
        evidence_budget //= 2
        evidence = compile_context(hits, evidence_budget).text if evidence_budget >= 256 else ""
    final = build()
    after = estimated_size(final)
    if after > input_limit:
        raise ValueError(error)
    return ContextPlan(
        final,
        {
            "estimated_input_tokens": after,
            "original_input_estimate": before,
            "max_output_tokens": request.max_tokens,
            "context_window": context_window,
            "remaining_run_budget": remaining,
            "omitted_history_messages": omitted_history,
            "elided_tool_results": elided_tools,
            "elided_assistant_messages": elided_assistant,
            "compacted": before != after,
            "estimated": True,
        },
    )


def context_length_error(exc):
    """Classify known provider rejections without persisting their possibly private text."""
    if getattr(exc, "status_code", None) not in (400, 413, 422):
        return False
    body = getattr(exc, "body", {})
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    codes = {str(getattr(exc, "code", ""))}
    if isinstance(body, dict):
        codes.update(str(body.get(key, "")) for key in ("code", "type"))
    if codes & {"context_length_exceeded", "prompt_too_long", "input_too_long"}:
        return True
    text = str(body.get("message", "") if isinstance(body, dict) else "").lower()
    return any(
        marker in text
        for marker in ("maximum context length", "context length exceeded", "prompt is too long")
    )
