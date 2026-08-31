"""Build the host-controlled Patch Run state machine.

Nodes return bounded state updates and LangGraph persists those updates through the supplied
checkpointer. Repository execution remains behind ``BaselineVerifier`` rather than being exposed
to the model. The current milestone creates one typed, checksummed Plan through bounded inspection;
later milestones add Candidate Patch generation, approval, application, and post-change Verification.
"""

from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from patch_code_agent.planning import PlanArtifactReference, Planner
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
        return "create_plan"
    return "end"


def create_plan(state: RunState, planner: Planner) -> RunState:
    """Create or replay one runtime-validated Plan and its immutable artifact.

    The Planner owns the model request and exposes the workspace only through bounded tools.
    """
    result = planner.create_once(
        run_id=state["run_id"],
        workspace=Path(state["workspace_path"]),
        issue=state["issue"],
        verification=state["verification_argv"],
        expected_reference=(
            PlanArtifactReference.model_validate(state["plan_artifact"])
            if "plan_artifact" in state
            else None
        ),
    )
    return {
        "plan_artifact": result.reference.model_dump(mode="json"),
        "model_requests": result.artifact.model_requests,
        "tool_executions": result.artifact.tool_executions,
        "files_read": list(result.artifact.files_read),
        "attempt": 0,
        "approved": False,
        "status": "planned",
        "report": {
            "success": False,
            "phase": "planned",
            "note": "Candidate Patch generation and Approval are the next milestone.",
        },
    }


def build_graph(
    *,
    baseline_verifier: BaselineVerifier,
    planner: Planner,
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
    builder.add_node("create_plan", lambda state: create_plan(state, planner))
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "baseline_verification")
    builder.add_conditional_edges(
        "baseline_verification",
        route_after_baseline,
        {"create_plan": "create_plan", "end": END},
    )
    builder.add_edge("create_plan", END)
    return builder.compile(checkpointer=selected_checkpointer)
