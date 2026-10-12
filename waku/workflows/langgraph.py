"""Optional bounded workflow with checkpointed approval; callers own the checkpointer."""

from typing import TypedDict


class WorkflowState(TypedDict, total=False):
    run_id: str
    status: str
    operation_id: str
    steps: int
    result: dict


def build_workflow(advance, approve, checkpointer, max_steps=20):
    """advance uses Si's Runtime, approve uses its scoped operation API.

    The graph never performs tools or decides authorization itself. A caller
    must supply a durable checkpointer for process-restart guarantees.
    """
    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
    except ImportError as exc:
        raise RuntimeError("install the copilot-workflow extra") from exc

    def execute(state):
        if state.get("steps", 0) >= max_steps:
            raise ValueError("workflow step budget exhausted")
        return {**advance(state), "steps": state.get("steps", 0) + 1}

    def approval(state):
        decision = interrupt({"run_id": state["run_id"], "operation_id": state["operation_id"]})
        if (
            not isinstance(decision, dict)
            or set(decision) != {"approved"}
            or type(decision["approved"]) is not bool
        ):
            raise ValueError("approval requires an explicit boolean")
        approve(state["operation_id"], decision["approved"])
        return {"status": "resuming"}

    graph = StateGraph(WorkflowState)
    graph.add_node("advance", execute)
    graph.add_node("approval", approval)
    graph.add_edge(START, "advance")
    graph.add_conditional_edges(
        "advance", lambda s: "approval" if s["status"] == "waiting_approval" else END
    )
    graph.add_edge("approval", "advance")
    return graph.compile(checkpointer=checkpointer)
