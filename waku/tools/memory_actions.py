"""Project memory mutations use human-approved operations and reviewable candidates."""

from typing import Literal

from pydantic import Field

from waku.memory.postgres import validate_uri
from waku.memory.service import MemoryService


def action_tools(memory, sessions, arguments, tool_spec):
    class ChangeArgs(arguments):
        uri: str = Field(min_length=1, max_length=1000)
        action: Literal["UPDATE", "RETRACT", "SUPERSEDES"]
        expected_revision: int = Field(ge=1)
        supersedes_id: str | None = None
        title: str = Field(min_length=1, max_length=240)
        kind: Literal["semantic", "episodic", "procedural"] = "semantic"
        content: str = Field(default="", max_length=4000)
        evidence_ids: list[str] = Field(min_length=1, max_length=30)

    class ForgetArgs(arguments):
        uri: str = Field(min_length=1, max_length=1000)
        reason: str = Field(min_length=1, max_length=240)
        expected_revision: int = Field(ge=1)

    def change(args, context, operation_id):
        from sqlalchemy import select

        from waku.storage.models import ContextNode

        validate_uri(args["uri"], context.user_id, context.project_id)
        with sessions() as session:
            target = (
                session.get(ContextNode, args["supersedes_id"])
                if args["action"] == "SUPERSEDES"
                else session.scalar(
                    select(ContextNode).where(
                        ContextNode.uri == args["uri"], ContextNode.project_id == context.project_id
                    )
                )
            )
            if (
                not target
                or target.project_id != context.project_id
                or target.kind not in {"semantic", "episodic", "procedural"}
            ):
                raise ValueError("only accepted memories can be changed")
            if target.status != "active" or target.revision != args["expected_revision"]:
                raise ValueError("target changed")
        service = memory if isinstance(memory, MemoryService) else MemoryService(sessions)
        # Explicit corrections are reviewed as mutations, not classified as new facts.
        return service.propose(
            {**args, "idempotency_key": "tool:" + operation_id},
            user_id=context.user_id,
            project_id=context.project_id,
        )

    def forget(args, context, operation_id):
        validate_uri(args["uri"], context.user_id, context.project_id)
        if not memory.forget(
            args["uri"],
            user_id=context.user_id,
            reason=args["reason"],
            expected_revision=args["expected_revision"],
        ):
            raise ValueError("source not found")
        return {
            "forgotten": True,
            "resource_id": args["uri"],
            "message": "Source, historical evidence and known derived memories were redacted",
        }

    return [
        tool_spec(
            "change_memory",
            "Propose a user-requested UPDATE, SUPERSEDES or RETRACT for review; requires current evidence and expected revision",
            ChangeArgs,
            change,
            writes=True,
        ),
        tool_spec(
            "forget_memory",
            "Forget one si:// source only when explicitly requested by the user; approval required; redacts dependent evidence",
            ForgetArgs,
            forget,
            writes=True,
        ),
    ]
