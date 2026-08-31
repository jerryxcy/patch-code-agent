from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from patch_code_agent.sources import is_ignored_source_path
from patch_code_agent.state import RunState


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
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    selected_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    builder = StateGraph(RunState)
    builder.add_node("validate_input", validate_input)
    builder.add_node("inspect_workspace", inspect_workspace)
    builder.add_node("create_plan", create_plan)
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "inspect_workspace")
    builder.add_edge("inspect_workspace", "create_plan")
    builder.add_edge("create_plan", END)
    return builder.compile(checkpointer=selected_checkpointer)
