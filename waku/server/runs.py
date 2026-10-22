"""Thread-to-run API. SSE reads durable events and disconnecting does not cancel work."""

import asyncio
import hashlib
import json
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from waku.runtime.budget import totals
from waku.runtime.compaction import fingerprint
from waku.runtime.context_store import (
    dependencies_valid,
    latest_summary,
    summary_state,
    working_state,
)
from waku.runtime.runner import (
    TERMINAL,
    append_event,
    approve_operation,
    enqueue_run,
    validate_sources,
)
from waku.server.schemas import ApprovalInput, RunInput, RunOutput
from waku.storage.models import (
    Job,
    MemoryEvidence,
    Message,
    Project,
    Run,
    RunEvent,
    Thread,
    ToolOperation,
)
from waku.workers.distillation import append_message, enqueue_distillation, statement_source


def run_routes(sessions, user_id):
    router = APIRouter()

    @router.get("/api/models")
    def models():
        import os

        from waku.providers.catalog import configured_catalog

        return configured_catalog(os.environ).public()

    def scoped(session, model, identifier):
        row = session.get(model, identifier)
        project = session.get(Project, row.project_id) if row else None
        if project is None or project.user_id != user_id:
            raise HTTPException(404, "Resource not found")
        return row

    @router.get("/api/projects/{project_id}/threads")
    def threads(project_id: str):
        with sessions() as session:
            project = session.get(Project, project_id)
            if not project or project.user_id != user_id:
                raise HTTPException(404, "Project not found")
            return [
                {
                    "id": t.id,
                    "project_id": t.project_id,
                    "title": t.title,
                    "created_at": t.created_at,
                }
                for t in session.scalars(
                    select(Thread)
                    .where(Thread.project_id == project_id)
                    .order_by(Thread.created_at.desc(), Thread.id)
                    .limit(200)
                )
            ]

    @router.get("/api/threads/{thread_id}/messages")
    def messages(
        thread_id: str,
        before: int | None = Query(None, ge=1),
        limit: int = Query(100, ge=1, le=200),
    ):
        with sessions() as session:
            scoped(session, Thread, thread_id)
            query = select(Message).where(Message.thread_id == thread_id)
            if before is not None:
                query = query.where(Message.sequence < before)
            rows = session.scalars(query.order_by(Message.sequence.desc()).limit(limit)).all()
            return [
                {
                    "id": m.id,
                    "sequence": m.sequence,
                    "role": m.role,
                    "content": m.content,
                    "event_data": m.event_data,
                    "created_at": m.created_at,
                }
                for m in reversed(rows)
            ]

    @router.get("/api/threads/{thread_id}/runs")
    def runs(thread_id: str, limit: int = Query(50, ge=1, le=200)):
        with sessions() as session:
            scoped(session, Thread, thread_id)
            return [
                RunOutput.model_validate(r).model_dump()
                for r in session.scalars(
                    select(Run)
                    .where(Run.thread_id == thread_id)
                    .order_by(Run.created_at.desc(), Run.id)
                    .limit(limit)
                )
            ]

    @router.post("/api/threads/{thread_id}/messages", response_model=RunOutput, status_code=202)
    def start(thread_id: str, payload: RunInput):
        digest = hashlib.sha256(
            json.dumps(payload.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        with sessions.begin() as session:
            thread = scoped(session, Thread, thread_id)
            # Match workers' project -> thread lock order and refresh counters
            # after waiting; the identity map may contain a pre-lock snapshot.
            session.execute(
                select(Project).where(Project.id == thread.project_id).with_for_update()
            )
            # Thread lock prevents overlapping turns from observing inconsistent history.
            thread = session.scalar(
                select(Thread)
                .where(Thread.id == thread_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            key = "run:" + thread_id + ":" + payload.idempotency_key
            job = session.scalar(
                select(Job).where(Job.project_id == thread.project_id, Job.idempotency_key == key)
            )
            if job:
                if job.payload_hash != digest:
                    raise HTTPException(409, "Idempotency key already used with different input")
                return scoped(session, Run, job.payload["run_id"])
            busy = session.scalar(
                select(Run.id)
                .where(Run.thread_id == thread_id, Run.status.not_in(TERMINAL))
                .limit(1)
            )
            if busy:
                raise HTTPException(409, "Thread already has an active run")
            summary = latest_summary(session, thread)
            history = session.scalars(
                select(Message)
                .where(Message.thread_id == thread_id, Message.role.in_(("user", "assistant")),
                       Message.sequence > (summary.covered_sequence if summary else 0))
                .order_by(Message.sequence)
            ).all()

            def current_history(message):
                if message.event_data.get("redacted"):
                    return False
                try:
                    # Historical assistant answers remain auditable, but stale
                    # conclusions must not poison the next provider request.
                    validate_sources(session, thread, message.event_data)
                    return True
                except ValueError:
                    return False

            history = [m for m in history if current_history(m)]
            messages = [{"role": m.role, "content": m.content} for m in history]
            messages.append({"role": "user", "content": payload.content})
            if not history and thread.title in {"New conversation", "新会话"}:
                thread.title = payload.content[:48].replace("\n", " ")
            state = {
                "user_id": user_id,
                "provider": payload.provider,
                "model": payload.model,
                "query": payload.content,
                "messages": messages,
                "tools": list(dict.fromkeys(payload.tools)),
                "budget": {
                    k: getattr(payload, k)
                    for k in ("max_iterations", "max_tokens", "max_tool_calls", "timeout_seconds")
                },
                "iterations": 0,
                "tool_count": 0,
                "charged_tokens": 0,
                "pending_calls": [],
                "compaction_cursor": 0,
                "working_summary": summary_state(summary) if summary else {},
                "working_state": {"goal": payload.content, "operations": []},
                "max_output_tokens": payload.max_output_tokens,
                "message_refs": [
                    {"id": m.id, "sequence": m.sequence, "hash": fingerprint(m.content)}
                    for m in history
                ],
                "source_nodes": [
                    *(summary.dependencies.get("source_nodes", []) if summary else []),
                    *[ref for message in history for ref in message.event_data.get("source_nodes", [])],
                ],
                "evidence_ids": list(
                    {
                        eid
                        for message in history
                        for eid in message.event_data.get("evidence_ids", [])
                    } | set(summary.dependencies.get("evidence_ids", []) if summary else [])
                ),
            }
            run = Run(
                project_id=thread.project_id,
                thread_id=thread_id,
                model=payload.model,
                trace_id=uuid4().hex,
                context=json.loads(json.dumps(state)),
            )
            session.add(run)
            session.flush()
            message = append_message(
                session,
                thread,
                role="user",
                content=payload.content,
                event_data={"run_id": run.id},
            )
            source = statement_source(
                session, session.get(Project, thread.project_id), thread_id, message
            )
            message.event_data = {**message.event_data, "source_nodes": [{"id": source.id, "revision": source.revision}]}
            state["message_refs"].append(
                {"id": message.id, "sequence": message.sequence, "hash": fingerprint(message.content)}
            )
            state["statement_evidence_ids"] = list(
                session.scalars(
                    select(MemoryEvidence.id).where(
                        MemoryEvidence.node_id == source.id,
                        MemoryEvidence.revision == source.revision,
                    )
                )
            )
            state["evidence_ids"] = list(
                set(state["evidence_ids"]) | set(state["statement_evidence_ids"])
            )
            state["source_nodes"].append({"id": source.id, "revision": source.revision})
            run.context = state
            enqueue_distillation(session, thread)
            job = enqueue_run(session, run, key)
            job.payload_hash = digest
            append_event(session, run, "run_queued", {"run_id": run.id})
            return run

    @router.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        with sessions() as session:
            run = scoped(session, Run, run_id)
            ops = session.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)).all()
            return {
                **RunOutput.model_validate(run).model_dump(),
                "reply": run.context.get("reply", ""),
                "iterations": run.context.get("iterations", 0),
                "charged_tokens": run.context.get("charged_tokens", 0),
                "usage_tokens": run.context.get("usage_tokens", 0),
                "budget_usage": totals(run.context),
                "error_code": run.context.get("error_code"),
                "operations": [
                    {
                        "id": op.id,
                        "tool": op.tool,
                        "status": op.status,
                        "arguments": op.arguments,
                        "result": op.result,
                    }
                    for op in ops
                ],
            }

    @router.get("/api/runs/{run_id}/context")
    def context_inspection(run_id: str):
        with sessions() as session:
            run = scoped(session, Run, run_id)
            state = run.context
            summary = state.get("working_summary", {})
            valid = not state.get("redacted") and all(dependencies_valid(session, run.project_id, d)
                for d in (state, summary.get("dependencies", {})))
            return {
                "summary": summary if valid else {},
                "working_state": working_state(session, run.id, state) if valid else {},
                "budget_usage": totals(state),
                "budget_ledger": state.get("budget_ledger", []),
                "compaction_cursor": state.get("compaction_cursor", 0),
                "canonical_message_count": len(state.get("messages", [])),
                "sources_valid": valid,
            }

    @router.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(0, ge=0)):
        with sessions() as session:
            scoped(session, Run, run_id)
            return [
                {"id": e.sequence, "kind": e.kind, "data": e.data, "created_at": e.created_at}
                for e in session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
                    .order_by(RunEvent.sequence)
                    .limit(500)
                )
            ]

    @router.get("/api/runs/{run_id}/events/stream")
    def stream(run_id: str, after: int = Query(0, ge=0), last_event_id: str | None = Header(None)):
        if last_event_id is not None:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                raise HTTPException(422, "Invalid Last-Event-ID") from None
        with sessions() as session:
            scoped(session, Run, run_id)

        async def generate():
            cursor = after
            while True:
                records = await asyncio.to_thread(events, run_id, cursor)
                for event in records:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event['data'])}\n\n"
                snapshot = await asyncio.to_thread(get_run, run_id)
                if len(records) < 500:
                    yield "event: snapshot\ndata: " + json.dumps(snapshot, default=str) + "\n\n"
                    if snapshot["status"] in TERMINAL:
                        return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/operations/{operation_id}/approval")
    def approve(operation_id: str, payload: ApprovalInput):
        with sessions.begin() as session:
            op = scoped(session, ToolOperation, operation_id)
            op = approve_operation(session, op, payload.approved)
            return {"operation_id": op.id, "status": op.status}

    @router.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        with sessions.begin() as session:
            run = scoped(session, Run, run_id)
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.status not in TERMINAL:
                run.status = "cancelled"
                append_event(session, run, "run_end", {"status": "cancelled"})
            return {"run_id": run_id, "status": run.status}

    return router
