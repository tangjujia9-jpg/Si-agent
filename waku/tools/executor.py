"""Validated product tools. No legacy unrestricted registry is exposed to the API."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from waku.domain.contracts import ToolResult
from waku.memory.port import MemoryQuery


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)


class ReadArgs(Arguments):
    uri: str = Field(min_length=1, max_length=1000)


class ProposeArgs(Arguments):
    uri: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: type[BaseModel]
    execute: Callable
    writes: bool = False
    idempotent: bool = True

    def schema(self):
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.arguments.model_json_schema(),
        }


class Executor:
    def __init__(self, tools=()):
        self.tools = {tool.name: tool for tool in tools}

    def schemas(self, allowed):
        return [self.tools[name].schema() for name in allowed if name in self.tools]

    def validate(self, name, args, allowed):
        if name not in allowed or name not in self.tools:
            raise ValueError("tool is not available for this run")
        return self.tools[name].arguments.model_validate(args).model_dump()

    def execute(self, name, args, context, operation_id):
        try:
            data = self.tools[name].execute(args, context, operation_id)
            if isinstance(data, ToolResult):
                from dataclasses import replace

                return replace(data, operation_id=operation_id)
            return ToolResult(
                "succeeded",
                "Tool completed",
                operation_id=operation_id,
                resource_id=data.get("resource_id") if isinstance(data, dict) else None,
                data=data,
            )
        except (ValueError, ValidationError):
            return ToolResult(
                "failed",
                "Tool arguments, scope or evidence are invalid",
                operation_id=operation_id,
                error_code="invalid_operation",
            )
        except Exception as exc:
            # A transport failure after an external write does not prove it failed.
            status = "unknown" if self.tools[name].writes else "failed"
            return ToolResult(
                status,
                "Tool outcome requires inspection",
                operation_id=operation_id,
                error_code=type(exc).__name__,
                retryable=False,
            )


def project_executor(memory, sessions):
    def search(args, context, _operation_id):
        from dataclasses import asdict

        hits = memory.search(
            MemoryQuery(
                text=args["query"],
                user_id=context.user_id,
                project_id=context.project_id,
                top_k=args["top_k"],
                max_tokens=2000,
            )
        )
        return {"hits": [asdict(hit) for hit in hits]}

    def read(args, context, _operation_id):
        from waku.memory.postgres import validate_uri

        validate_uri(args["uri"], context.user_id, context.project_id)
        node = memory.get(args["uri"], user_id=context.user_id)
        if node is None or node.status != "active" or node.metadata.get("needs_review"):
            raise ValueError("node not active")
        from sqlalchemy import select

        from waku.storage.models import MemoryEvidence

        with sessions() as session:
            evidence_ids = list(
                session.scalars(
                    select(MemoryEvidence.id).where(
                        MemoryEvidence.node_id == node.metadata["id"],
                        MemoryEvidence.revision == node.metadata["revision"],
                        MemoryEvidence.redacted.is_(False),
                        MemoryEvidence.start_char < 4000,
                    )
                )
            )
        return {
            "node_id": node.metadata["id"],
            "uri": node.uri,
            "content": node.content[:4000],
            "revision": node.metadata["revision"],
            "evidence_ids": evidence_ids,
        }

    def propose(args, context, operation_id):
        from waku.memory.service import MemoryService

        service = memory if isinstance(memory, MemoryService) else MemoryService(sessions)
        return service.propose(
            dict(
                **args,
                action="ADD",
                kind="semantic",
                expected_revision=None,
                supersedes_id=None,
                idempotency_key="tool:" + operation_id,
            ),
            user_id=context.user_id,
            project_id=context.project_id,
            decisions=getattr(executor, "decisions", None),
        )

    executor = Executor(
        [
            ToolSpec("search_memory", "Find cited evidence in this project", SearchArgs, search),
            ToolSpec("read_memory", "Read an active si:// source in this project", ReadArgs, read),
            ToolSpec(
                "propose_memory",
                "Propose a source-grounded stable fact for human review",
                ProposeArgs,
                propose,
                writes=True,
                idempotent=True,
            ),
        ]
    )

    return executor
