"""Build the host-controlled Patch Run state machine.

Nodes return bounded state updates and LangGraph persists those updates through the supplied
checkpointer. Repository execution remains behind ``BaselineVerifier`` rather than being exposed
to the model. The current milestone stops at a starter Plan; later milestones add model planning,
approval, patch application, and post-change Verification.
"""

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
    """Validate the workspace and Issue before any external execution.

    Resolving the workspace once gives downstream nodes a canonical absolute path. Contract-level
    validation happens earlier in the source adapters; this node checks the runtime assumptions
    required to begin graph execution.
    """
    workspace = Path(state["workspace_path"]).resolve()
    if not workspace.is_dir():
        raise ValueError(f"Run Workspace directory does not exist: {workspace}")
    if not state["issue"].strip():
        raise ValueError("Issue must not be empty")
    return {"workspace_path": str(workspace), "status": "validated"}


def inspect_workspace(state: RunState) -> RunState:
    """Record a bounded list of visible Python files for the planning scaffold.

    Hidden caches, virtual environments, and compiled files use the same source-view policy as
    workspace copying. Sorting makes state deterministic, while the 100-file cap prevents a large
    repository from inflating the Checkpoint.
    """
    workspace = Path(state["workspace_path"])
    files = sorted(
        str(path.relative_to(workspace))
        for path in workspace.rglob("*.py")
        if not is_ignored_source_path(path.relative_to(workspace))
    )
    return {"inspected_files": files[:100], "status": "inspected"}


def run_baseline_verification(state: RunState, verifier: BaselineVerifier) -> RunState:
    """Execute baseline Verification and translate its outcome into Run status.

    The full process output stays in Run Artifacts. Only the verifier's bounded Pydantic summary
    enters state, and the outcome is converted into the domain-level status used for routing.
    """
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
    """Continue only when exit code 1 demonstrated a reproducible Issue.

    A passing baseline means there is nothing safe to repair. Timeout and execution errors are
    also terminal because they do not provide a trustworthy failing test for model planning.
    """
    if state["status"] == "baseline_failed":
        return "inspect_workspace"
    return "end"


def create_plan(state: RunState) -> RunState:
    """Create the temporary starter Plan used before model planning is implemented.

    This node deliberately makes no Model Gateway request. It demonstrates the durable planning
    transition while later tickets implement model-generated structured output.
    """
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
    """Compile the Patch Run graph with durable or in-memory checkpointing.

    Production injects SQLite while focused graph tests may accept the in-memory default. The
    verifier is mandatory because baseline execution is an application-owned side-effect seam,
    not a global dependency hidden inside a node.
    """
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
