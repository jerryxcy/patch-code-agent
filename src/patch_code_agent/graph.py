from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from patch_code_agent.state import RunState


def validate_input(state: RunState) -> RunState:
    repo = Path(state["repo_path"]).resolve()
    if not repo.is_dir():
        raise ValueError(f"Repository directory does not exist: {repo}")
    if not state["issue"].strip():
        raise ValueError("Issue must not be empty")
    return {"repo_path": str(repo), "status": "validated"}


def inspect_repo(state: RunState) -> RunState:
    repo = Path(state["repo_path"])
    files = sorted(
        str(path.relative_to(repo))
        for path in repo.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(repo).parts)
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


def build_graph():
    builder = StateGraph(RunState)
    builder.add_node("validate_input", validate_input)
    builder.add_node("inspect_repo", inspect_repo)
    builder.add_node("create_plan", create_plan)
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "inspect_repo")
    builder.add_edge("inspect_repo", "create_plan")
    builder.add_edge("create_plan", END)
    return builder.compile(checkpointer=InMemorySaver())
