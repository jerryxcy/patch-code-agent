"""Build the host-controlled Patch Run state machine.

Nodes return bounded state updates and LangGraph persists those updates through the supplied
checkpointer. Repository execution remains behind ``BaselineVerifier`` rather than being exposed
to the model. Candidate Patch persistence completes before ``interrupt`` pauses at the Approval
Gate, so a later process can inspect or reject the exact immutable proposal without changing the
workspace.
"""

from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from patch_code_agent.candidate import CandidatePatchBuilder, CandidatePatchReference
from patch_code_agent.diagnosis import DiagnosisArtifactReference, Diagnostician
from patch_code_agent.patching import PatchApplier
from patch_code_agent.planning import PlanArtifactReference, Planner
from patch_code_agent.state import RunState, RunStatus
from patch_code_agent.verification import (
    BaselineVerificationOutcome,
    BaselineVerifier,
    RepairVerificationSummary,
    RepairVerifier,
)

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
            "note": "Plan is complete; Candidate Patch generation follows.",
        },
    }


def create_candidate(state: RunState, builder: CandidatePatchBuilder) -> RunState:
    """Persist one validated Candidate Patch before the Approval Gate can interrupt."""
    plan_reference = PlanArtifactReference.model_validate(state["plan_artifact"])
    candidate_attempt = state.get("attempt", 0) + 1
    current_candidate_reference = (
        CandidatePatchReference.model_validate(state["candidate_artifact"])
        if "candidate_artifact" in state
        else None
    )
    result = builder.create_once(
        run_id=state["run_id"],
        workspace=Path(state["workspace_path"]),
        issue=state["issue"],
        editable_paths=state["editable_paths"],
        protected_paths=state.get("protected_paths", []),
        plan_reference=plan_reference,
        attempt=candidate_attempt,
        diagnosis_reference=(
            DiagnosisArtifactReference.model_validate(state["diagnosis_artifact"])
            if "diagnosis_artifact" in state
            else None
        ),
        expected_reference=(
            current_candidate_reference
            if current_candidate_reference is not None
            and current_candidate_reference.path == f"attempts/{candidate_attempt}/candidate.json"
            else None
        ),
    )
    return {
        "candidate_artifact": result.reference.model_dump(mode="json"),
        "model_requests": state["model_requests"] + result.artifact.model_requests,
        "tool_executions": state["tool_executions"] + result.artifact.tool_executions,
        "files_read": sorted(set(state["files_read"]) | set(result.artifact.files_read)),
        "approved": False,
        "status": "pending_approval",
        "report": {
            "success": False,
            "phase": "pending_approval",
            "note": "Candidate Patch is immutable and awaiting an Approval decision.",
        },
    }


def await_approval(state: RunState) -> RunState:
    """Pause after persistence and expose only the immutable Candidate Patch reference."""
    decision = interrupt(
        {
            "run_id": state["run_id"],
            "candidate_artifact": state["candidate_artifact"],
        }
    )
    if decision not in {"approve", "reject"}:
        raise ValueError(f"Unknown Approval decision: {decision}")
    return {"approval_decision": decision}


def route_after_approval(state: RunState) -> str:
    """Send the host-supplied decision to rejection or replay-safe application."""
    return state["approval_decision"]


def reject_candidate(state: RunState) -> RunState:
    """Finish a rejected Patch Run without applying or consuming a Repair Attempt."""
    return {
        "approved": False,
        "status": "rejected",
        "report": {
            "success": False,
            "phase": "rejected",
            "note": "The immutable Candidate Patch was rejected without modifying the workspace.",
        },
    }


def apply_candidate(state: RunState, applier: PatchApplier) -> RunState:
    """Revalidate and apply the approved Candidate Patch without trusting model state."""
    reference = CandidatePatchReference.model_validate(state["candidate_artifact"])
    summary = applier.apply_once(
        run_id=state["run_id"],
        workspace=Path(state["workspace_path"]),
        reference=reference,
    )
    update: RunState = {
        "approved": True,
        "apply_summary": summary.model_dump(mode="json"),
        "files_changed": sorted(set(state.get("files_changed", [])) | set(summary.files_changed)),
    }
    if summary.outcome in {"applied", "already_applied"}:
        update["status"] = "testing"
    elif summary.outcome == "workspace_changed":
        update["status"] = "workspace_changed"
        update["error_kind"] = "workspace_changed"
    else:
        update["status"] = "error"
        update["error_kind"] = "partial_apply"
    return update


def route_after_apply(state: RunState) -> str:
    """Run Verification only after an all-before or all-after apply state."""
    if state["status"] == "testing":
        return "verify"
    return "end"


def run_repair_verification(
    state: RunState,
    verifier: RepairVerifier,
    applier: PatchApplier,
) -> RunState:
    """Execute one approved Repair Attempt and classify its Verification result."""
    attempt = state.get("attempt", 0) + 1
    summary = verifier.verify(
        run_id=state["run_id"],
        workspace=Path(state["workspace_path"]),
        argv=state["verification_argv"],
        attempt=attempt,
    )
    update: RunState = {
        "attempt": attempt,
        "verification": summary.model_dump(mode="json"),
    }
    if summary.outcome == "passed":
        cumulative = applier.persist_cumulative_diff(
            run_id=state["run_id"],
            workspace=Path(state["workspace_path"]),
            reference=CandidatePatchReference.model_validate(state["candidate_artifact"]),
        )
        update.update(
            {
                "status": "succeeded",
                "cumulative_diff": cumulative.model_dump(mode="json"),
                "report": {
                    "success": True,
                    "phase": "succeeded",
                    "note": "The approved Candidate Patch passed Verification.",
                },
            }
        )
    elif summary.outcome == "failed":
        update["status"] = "diagnosing"
    elif summary.outcome == "timeout":
        update["status"] = "budget_exceeded"
        update["error_kind"] = "verification_timeout"
    else:
        update["status"] = "error"
        update["error_kind"] = summary.error_kind or "verification_error"
    return update


def route_after_repair_verification(state: RunState) -> str:
    """Create a Diagnosis only for a failed Repair Attempt with budget remaining."""
    if state["status"] == "diagnosing":
        return "diagnose"
    return "end"


def create_diagnosis(state: RunState, diagnostician: Diagnostician) -> RunState:
    """Persist one typed Diagnosis against the current failing Run Workspace."""
    verification = RepairVerificationSummary.model_validate(state["verification"])
    current_diagnosis_reference = (
        DiagnosisArtifactReference.model_validate(state["diagnosis_artifact"])
        if "diagnosis_artifact" in state
        else None
    )
    result = diagnostician.create_once(
        run_id=state["run_id"],
        workspace=Path(state["workspace_path"]),
        issue=state["issue"],
        plan_reference=PlanArtifactReference.model_validate(state["plan_artifact"]),
        verification=verification,
        expected_reference=(
            current_diagnosis_reference
            if current_diagnosis_reference is not None
            and current_diagnosis_reference.path
            == f"attempts/{verification.attempt}/diagnosis.json"
            else None
        ),
    )
    update: RunState = {
        "diagnosis_artifact": result.reference.model_dump(mode="json"),
        "model_requests": state["model_requests"] + result.artifact.model_requests,
        "tool_executions": state["tool_executions"] + result.artifact.tool_executions,
        "files_read": sorted(set(state["files_read"]) | set(result.artifact.files_read)),
    }
    if verification.attempt >= 3:
        update.update(
            {
                "status": "attempts_exhausted",
                "report": {
                    "success": False,
                    "phase": "attempts_exhausted",
                    "note": "Three approved Repair Attempts failed Verification.",
                },
            }
        )
    else:
        update["status"] = "diagnosed"
    return update


def route_after_diagnosis(state: RunState) -> str:
    """Stop after the third Diagnosis; otherwise propose the next Candidate Patch."""
    if state["status"] == "attempts_exhausted":
        return "end"
    return "candidate"


def build_graph(
    *,
    baseline_verifier: BaselineVerifier,
    planner: Planner,
    candidate_builder: CandidatePatchBuilder,
    diagnostician: Diagnostician,
    patch_applier: PatchApplier,
    repair_verifier: RepairVerifier,
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
    builder.add_node(
        "create_candidate",
        lambda state: create_candidate(state, candidate_builder),
    )
    builder.add_node("approval_gate", await_approval)
    builder.add_node("reject_candidate", reject_candidate)
    builder.add_node("apply_candidate", lambda state: apply_candidate(state, patch_applier))
    builder.add_node(
        "repair_verification",
        lambda state: run_repair_verification(state, repair_verifier, patch_applier),
    )
    builder.add_node(
        "create_diagnosis",
        lambda state: create_diagnosis(state, diagnostician),
    )
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "baseline_verification")
    builder.add_conditional_edges(
        "baseline_verification",
        route_after_baseline,
        {"create_plan": "create_plan", "end": END},
    )
    builder.add_edge("create_plan", "create_candidate")
    builder.add_edge("create_candidate", "approval_gate")
    builder.add_conditional_edges(
        "approval_gate",
        route_after_approval,
        {"reject": "reject_candidate", "approve": "apply_candidate"},
    )
    builder.add_edge("reject_candidate", END)
    builder.add_conditional_edges(
        "apply_candidate",
        route_after_apply,
        {"verify": "repair_verification", "end": END},
    )
    builder.add_conditional_edges(
        "repair_verification",
        route_after_repair_verification,
        {"diagnose": "create_diagnosis", "end": END},
    )
    builder.add_conditional_edges(
        "create_diagnosis",
        route_after_diagnosis,
        {"candidate": "create_candidate", "end": END},
    )
    return builder.compile(checkpointer=selected_checkpointer)
