from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from patch_code_agent.sources import is_ignored_source_path
from patch_code_agent.state import RunState, RunStatus
from patch_code_agent.verification import BaselineVerificationOutcome, BaselineVerifier

_BASELINE_STATUS: dict[BaselineVerificationOutcome, RunStatus] = {
    "failed": "baseline_failed",
    "passed": "issue_not_reproduced",
    "error": "error",
    "timeout": "budget_exceeded",
}


def validate_input(state: RunState) -> RunState:
    workspace = Path(state["workspace_path"]).resolve()
    if not workspace.is_dir():
        raise ValueError(f"Run Workspace directory does not exist: {workspace}")
    if not state["issue"].strip():
        raise ValueError("Issue must not be empty")
    return {"workspace_path": str(workspace), "status": "validated"}


def inspect_workspace(state: RunState) -> RunState:
    workspace = Path(state["workspace_path"])
    files = sorted(
        str(path.relative_to(workspace))
        for path in workspace.rglob("*.py")
        if not is_ignored_source_path(path.relative_to(workspace))
    )
    return {"inspected_files": files[:100], "status": "inspected"}


def run_baseline_verification(state: RunState, verifier: BaselineVerifier) -> RunState:
    summary = verifier.verify(
        run_id=state["run_id"],
        workspace=Path(state["workspace_path"]),
        argv=state["verification_argv"],
    )
    return {
        "baseline_verification": summary.model_dump(mode="json"),
        "status": _BASELINE_STATUS[summary.outcome],
    }


def route_after_baseline(state: RunState) -> str:
    if state["status"] == "baseline_failed":
        return "inspect_workspace"
    return "end"


def create_plan(state: RunState) -> RunState:
    files = state.get("inspected_files", [])
    scope = ", ".join(files[:5]) if files else "the selected repository"
    return {
        "plan": [
            f"Inspect the issue against {scope}",
            "Propose the smallest patch and verify it with pytest",
        ],
        "attempt": 0,
        "approved": False,
        "status": "planned",
        "report": {
            "success": False,
            "phase": "scaffold",
            "note": "Model, patch, approval, and verifier nodes are the next milestone.",
        },
    }


def build_graph(
    *,
    baseline_verifier: BaselineVerifier,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    selected_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    builder = StateGraph(RunState)
    builder.add_node("validate_input", validate_input)
    builder.add_node(
        "baseline_verification",
        lambda state: run_baseline_verification(state, baseline_verifier),
    )
    builder.add_node("inspect_workspace", inspect_workspace)
    builder.add_node("create_plan", create_plan)
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "baseline_verification")
    builder.add_conditional_edges(
        "baseline_verification",
        route_after_baseline,
        {"inspect_workspace": "inspect_workspace", "end": END},
    )
    builder.add_edge("inspect_workspace", "create_plan")
    builder.add_edge("create_plan", END)
    return builder.compile(checkpointer=selected_checkpointer)
