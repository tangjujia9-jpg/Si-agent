"""Read durable tool output by scoped operation ID, never by filesystem path."""

import json

from pydantic import Field
from sqlalchemy import select

from waku.runtime.context_store import dependencies_valid
from waku.storage.models import Project, Run, ToolOperation


def result_tool(sessions, Arguments, ToolSpec):
    class ReadResultArgs(Arguments):
        operation_id: str = Field(min_length=1, max_length=80)
        offset: int = Field(default=0, ge=0)
        limit: int = Field(default=2000, ge=1, le=4000)

    def read(args, context, _operation_id):
        with sessions() as session:
            project = session.get(Project, context.project_id)
            operation = session.scalar(select(ToolOperation).where(
                ToolOperation.id == args["operation_id"],
                ToolOperation.project_id == context.project_id,
            ))
            run = session.get(Run, operation.run_id) if operation else None
            if (not project or project.user_id != context.user_id or not run
                    or run.context.get("redacted")
                    or not dependencies_valid(session, context.project_id, run.context)
                    or not dependencies_valid(session, context.project_id, run.context.get("working_summary", {}).get("dependencies", {}))):
                raise ValueError("result unavailable")
            text = json.dumps(operation.result, ensure_ascii=False)
            page = text[args["offset"]:args["offset"] + args["limit"]]
            # Bound UTF-8 bytes as well as characters so a CJK page fits the model projection.
            page = page.encode()[:2500].decode("utf-8", errors="ignore")
            end = args["offset"] + len(page)
            return {"result_page": True, "resource_id": operation.id, "content": page,
                    "offset": args["offset"], "next_offset": end if end < len(text) else None,
                    "total_characters": len(text), "source_nodes": run.context.get("source_nodes", []),
                    "evidence_ids": run.context.get("evidence_ids", [])}

    return ToolSpec("read_operation_result", "Read a page of an earlier durable tool result in this project",
                    ReadResultArgs, read)
