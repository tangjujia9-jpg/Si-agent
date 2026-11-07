"""Model-managed task steps, separate from executor-confirmed operation state."""

from typing import Literal

from pydantic import Field


def plan_tool(Arguments, ToolSpec):
    class Step(Arguments):
        title: str = Field(min_length=1, max_length=200)
        status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"

    class PlanArgs(Arguments):
        steps: list[Step] = Field(min_length=1, max_length=12)
        explanation: str = Field(default="", max_length=500)

    def update(args, ctx, oid):
        if sum(step["status"] == "in_progress" for step in args["steps"]) > 1:
            raise ValueError("only one active plan step")
        return {"plan": args, "authority": "model_plan_not_execution_proof"}

    return ToolSpec("update_plan", "Track task steps. Plan completion is your assessment, not proof of execution or approval.", PlanArgs, update)
