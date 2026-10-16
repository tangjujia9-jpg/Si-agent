"""Thread-to-run API. SSE reads durable events and disconnecting does not cancel work."""

import asyncio
import hashlib
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from waku.runtime.runner import TERMINAL, append_event, approve_operation, enqueue_run
from waku.server.schemas import ApprovalInput, RunInput, RunOutput
from waku.storage.models import Job, Message, Project, Run, RunEvent, Thread, ToolOperation
from waku.workers.distillation import append_message, enqueue_distillation


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
            history = session.scalars(
                select(Message)
                .where(Message.thread_id == thread_id, Message.role.in_(("user", "assistant")))
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(12)
            ).all()
            messages = [{"role": m.role, "content": m.content} for m in reversed(history)]
            messages.append({"role": "user", "content": payload.content})
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
                "source_nodes": [
                    ref for message in history for ref in message.event_data.get("source_nodes", [])
                ],
                "evidence_ids": list(
                    {
                        eid
                        for message in history
                        for eid in message.event_data.get("evidence_ids", [])
                    }
                ),
            }
            run = Run(
                project_id=thread.project_id,
                thread_id=thread_id,
                model=payload.model,
                trace_id=uuid4().hex,
                context=state,
            )
            session.add(run)
            session.flush()
            append_message(
                session,
                thread,
                role="user",
                content=payload.content,
                event_data={"run_id": run.id},
            )
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
    def stream(run_id: str, after: int = Query(0, ge=0)):
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
                if (
                    snapshot["status"] in TERMINAL | {"waiting_approval", "waiting_operation"}
                    and len(records) < 500
                ):
                    yield "event: snapshot\ndata: " + json.dumps(snapshot, default=str) + "\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
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
