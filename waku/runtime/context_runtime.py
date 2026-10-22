"""Checkpointed semantic compaction; provider calls run outside DB transactions."""

import json
from dataclasses import asdict, replace

from sqlalchemy import select

from waku.providers.adapters import estimated_size
from waku.runtime.compaction import (
    POLICY,
    choose_cut,
    compile_working,
    cut_points,
    fingerprint,
    margin,
    summarization_request,
    summary_input,
    validate_summary,
)
from waku.runtime.context_store import dependencies_valid, summary_state
from waku.storage.models import ContextSummary, ToolOperation, now


class ContextManager:
    def __init__(self, runtime, rid, job_id, token, state, trace, provider, model, deadline):
        self.runtime, self.rid, self.job_id, self.token = runtime, rid, job_id, token
        self.state, self.trace, self.provider, self.model, self.deadline = state, trace, provider, model, deadline

    def save(self, kind, data):
        self.runtime._save(self.rid, self.job_id, self.token, self.state, event=(kind, data))

    def prepare(self, request, hits, window, *, force=False):
        with self.runtime.sessions() as session:
            operations = list(session.scalars(select(ToolOperation).where(
                ToolOperation.run_id == self.rid
            ).order_by(ToolOperation.created_at.desc(), ToolOperation.id)))
            self.state["working_state"] = {
                "goal": self.state["query"],
                "operations": [{"id": op.id, "tool": op.tool, "status": op.status,
                                "resource_id": op.result.get("resource_id"),
                                "error_code": op.result.get("error_code")} for op in reversed(operations)],
                "tool_count": self.state["tool_count"],
                "pending_call_ids": [c["id"] for c in self.state.get("pending_calls", [])],
            }
        available = window - request.max_tokens - margin(window)
        if available <= 0:
            raise ValueError("context_budget")
        before = estimated_size(compile_working(request, self.state, hits))
        for _ in range(64):
            compiled = compile_working(request, self.state, hits)
            size = estimated_size(compiled)
            if not force and size <= available * 0.70:
                break
            cut = choose_cut(self.state, request, window)
            if cut is None:
                if size > available:
                    raise ValueError("context_budget")
                break
            if not self.compact(cut, window):
                if size > available:
                    raise ValueError("context_budget")
                break
            force = False
            if estimated_size(compile_working(request, self.state, hits)) <= available * 0.50:
                break
        compiled = compile_working(request, self.state, hits)
        size = estimated_size(compiled)
        if size > available:
            raise ValueError("context_budget")
        remaining = self.state["budget"]["max_tokens"] - self.state["charged_tokens"]
        if size + request.max_tokens > remaining:
            raise ValueError("token_budget")
        metrics = {
            "estimated_input_tokens": size, "original_input_estimate": before,
            "context_window": window, "max_output_tokens": request.max_tokens,
            "remaining_run_budget": remaining, "protocol_margin": margin(window),
            "count_basis": "utf8_upper_bound", "estimated": True,
            "summary_id": self.state.get("working_summary", {}).get("id"),
            "covered_messages": self.state.get("compaction_cursor", 0),
            "compacted": size != before,
            "sections": ["fixed_policy", "task_state", "derived_summary", "recent_exchanges", "evidence"],
        }
        self.save("context_plan", metrics)
        return compiled, metrics

    def compact(self, cut, window):
        state = self.state
        points = [p for p in cut_points(state["messages"], state.get("compaction_cursor", 0)) if p <= cut]
        # The summarizer itself must fit. Bound batches before doing a paid call.
        for candidate in reversed(points):
            payload = summary_input(state, candidate)
            output = min(2048, max(512, window // 8))
            req = summarization_request(payload, output)
            if estimated_size(req) + output + margin(window) <= window:
                cut = candidate
                break
        else:
            raise ValueError("compaction_input_too_large")
        # A short prefix before the newest tool batch cannot pay for a structured summary.
        if len(json.dumps(payload["transcript"], ensure_ascii=False).encode()) < 1024:
            return False
        input_hash = fingerprint({"payload": payload, "cut": cut, "policy": POLICY})
        old = state.get("working_summary", {})
        refs = state.get("message_refs", [])
        dependencies = {
            "source_nodes": state.get("source_nodes", []),
            "evidence_ids": state.get("evidence_ids", []),
            "message_refs": list({r["id"]: r for r in [
                *old.get("dependencies", {}).get("message_refs", []), *refs[:cut]
            ]}.values()),
        }
        self.save("compaction_start", {"from_entry": state.get("compaction_cursor", 0), "to_entry": cut})
        body, response = None, None
        for attempt in range(2):
            if now() >= self.deadline:
                raise TimeoutError("run_deadline")
            req = summarization_request(payload, output)
            req = replace(req, metadata={
                "timeout_seconds": min(45, max(0.1, (self.deadline - now()).total_seconds()))
            })
            attempt_id = self.runtime._reserve(
                self.rid, self.job_id, self.token, state, estimated_size(req) + output,
                ("summary_call", {"attempt": attempt + 1}),
            )
            try:
                with self.trace.span("summarization", {"model": self.model.model}) as (_, attrs):
                    response = self.provider.complete(self.model, req)
                    attrs.update(asdict(response.usage))
                self.runtime._settle(self.rid, self.job_id, self.token, state, attempt_id, response.usage)
                if response.stop_reason != "stop" or response.tool_calls:
                    raise ValueError("compaction_invalid")
                body = validate_summary(response.text, payload)
                # A summary that grows the represented prefix is not usable compaction.
                if len(json.dumps(body, ensure_ascii=False).encode()) >= len(
                    json.dumps(payload, ensure_ascii=False).encode()
                ):
                    raise ValueError("compaction_invalid")
                break
            except Exception as exc:
                if str(exc) == "compaction_unavailable":
                    self.save("compaction_failed", {"error_code": "compaction_unavailable"})
                    raise
                if str(exc) in {"run_cancelled", "stale_lease", "source_evidence_changed", "token_budget"}:
                    raise
                body = None
                if attempt == 1:
                    self.save("compaction_failed", {"error_code": "compaction_failed"})
                    raise ValueError("compaction_failed") from None
        with self.runtime.sessions.begin() as session:
            run = self.runtime._locked(session, self.rid, self.job_id, self.token)
            if run.status == "cancelled":
                raise RuntimeError("run_cancelled")
            if not dependencies_valid(session, run.project_id, dependencies):
                raise ValueError("source_evidence_changed")
            # The lease and canonical-prefix digest fence stale or concurrent commits.
            if fingerprint(run.context["messages"][:cut]) != fingerprint(state["messages"][:cut]):
                raise ValueError("source_evidence_changed")
            row = session.scalar(select(ContextSummary).where(
                ContextSummary.run_id == self.rid, ContextSummary.input_hash == input_hash
            ))
            if row is None:
                row = ContextSummary(
                    project_id=run.project_id, thread_id=run.thread_id, run_id=self.rid,
                    input_hash=input_hash, covered_count=cut,
                    covered_sequence=max([old.get("covered_sequence", 0), *[r["sequence"] for r in refs[:cut]]]),
                    model=self.model.model, body=body, dependencies=dependencies,
                    metrics={"usage": asdict(response.usage), "count_basis": "utf8_upper_bound"},
                )
                session.add(row)
                session.flush()
            state["working_summary"], state["compaction_cursor"] = summary_state(row), cut
            run.context = json.loads(json.dumps(state))
        self.save("compaction_end", {"summary_id": row.id, "covered_messages": cut})
        return True
