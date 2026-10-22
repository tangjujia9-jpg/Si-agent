"""Durable per-attempt reservations. Provider-reported usage is not a verified bill."""

from dataclasses import asdict
from uuid import uuid4


def reserve(state, amount, kind):
    if amount < 0 or state["charged_tokens"] + amount > state["budget"]["max_tokens"]:
        raise ValueError("token_budget")
    entry = {"id": uuid4().hex, "kind": kind, "reserved": amount, "charged": amount,
             "status": "reserved", "usage": None}
    state.setdefault("budget_ledger", []).append(entry)
    state["charged_tokens"] += amount
    return entry["id"]


def settle(state, attempt_id, usage):
    entry = next(e for e in state["budget_ledger"] if e["id"] == attempt_id)
    if entry["status"] == "settled":
        return False
    values = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
    if any(type(v) is not int or v < 0 for v in values):
        entry["status"] = "uncertain"
        return False
    # A zero/partial report cannot safely release a reservation.
    if usage.total_tokens <= 0 or usage.total_tokens < usage.input_tokens + usage.output_tokens:
        entry["status"] = "uncertain"
        return False
    state["charged_tokens"] += usage.total_tokens - entry["charged"]
    state["usage_tokens"] = state.get("usage_tokens", 0) + usage.total_tokens
    entry.update(charged=usage.total_tokens, status="settled", usage=asdict(usage))
    return True


def resume_reservations(state):
    for entry in state.get("budget_ledger", []):
        if entry["status"] == "reserved":
            entry["status"] = "uncertain"


def totals(state):
    ledger = state.get("budget_ledger", [])
    return {
        "charged_tokens": state.get("charged_tokens", 0),
        "reported_tokens": state.get("usage_tokens", 0),
        "unsettled_tokens": sum(e["charged"] for e in ledger if e["status"] != "settled"),
        "attempts": len(ledger),
        "basis": "provider_report_with_conservative_unsettled_reservations",
    }
