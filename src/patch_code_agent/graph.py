"""Build the host-controlled Patch Run state machine.

Nodes return bounded state updates and LangGraph persists those updates through the supplied
checkpointer. Repository execution remains behind ``BaselineVerifier`` rather than being exposed
to the model. Candidate Patch persistence completes before ``interrupt`` pauses at the Approval
Gate, so a later process can inspect or reject the exact immutable proposal without changing the
workspace.
"""

from collections.abc import Callable
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from patch_code_agent.candidate import CandidatePatchBuilder, CandidatePatchReference
from patch_code_agent.diagnosis import DiagnosisArtifactReference, Diagnostician
from patch_code_agent.limits import (
    MAX_FILES_CHANGED,
    MAX_REPAIR_ATTEMPTS,
    RunLimitExceededError,
)
from patch_code_agent.model_output import InvalidModelOutputError, ModelInvocationError
from patch_code_agent.patching import PatchApplier
from patch_code_agent.planning import PlanArtifactReference, Planner
from patch_code_agent.reporting import RunAuditStore
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
    "timeout": "error",
}


def _audit_transition(
    state: RunState,
    action: Callable[[RunState], RunState],
    audit_store: RunAuditStore,
    transition: Callable[[RunState, RunState], str],
) -> RunState:
    """Append one deduplicated event after a graph action durably completes."""
    update = action(state)
    projected: RunState = {**state, **update}
    audit_store.append_event(projected, transition(state, update))
    return update


def _invalid_model_output(state: RunState, error: InvalidModelOutputError) -> RunState:
    """Persist actual usage and a stable Error after the one correction request fails."""
    return {
        "model_requests": state.get("model_requests", 0) + error.model_requests,
        "tool_executions": state.get("tool_executions", 0) + error.tool_executions,
        "files_read": sorted(set(state.get("files_read", [])) | set(error.files_read)),
        "status": "error",
        "error_kind": "invalid_model_output",
        "report": {
            "success": False,
            "phase": "error",
            "note": "Model output remained invalid after one schema-correction request.",
        },
    }


def _model_failure(state: RunState, error: ModelInvocationError) -> RunState:
    """Classify provider/transport failure separately from failed repair Verification."""
    provider_unavailable = bool(getattr(error.cause, "provider_unavailable", False))
    update: RunState = {
        "model_requests": state.get("model_requests", 0) + error.model_requests,
        "tool_executions": state.get("tool_executions", 0) + error.tool_executions,
        "files_read": sorted(set(state.get("files_read", [])) | set(error.files_read)),
        "status": "error",
        "error_kind": "provider_unavailable" if provider_unavailable else "model_failure",
        "report": {
            "success": False,
            "phase": "error",
            "note": "Model infrastructure failed before valid typed output was returned.",
        },
    }
    status_code = getattr(error.cause, "status_code", None)
    if provider_unavailable and isinstance(status_code, int):
        update["provider_status_code"] = status_code
    return update


def _run_limit_reached(
    state: RunState,
    error: RunLimitExceededError,
) -> RunState:
    """Persist usage observed before a fixed safety limit refused the next operation."""
    return {
        "model_requests": state.get("model_requests", 0) + error.model_requests,
        "tool_executions": state.get("tool_executions", 0) + error.tool_executions,
        "files_read": sorted(set(state.get("files_read", [])) | set(error.files_read)),
        "status": "error",
        "error_kind": "limit_exceeded",
        "report": {
            "success": False,
            "phase": "error",
            "note": f"Run limit reached: {error.limit_name} ({error.used}/{error.limit}).",
        },
    }


def _infrastructure_failure(error_kind: str, note: str) -> RunState:
    """Produce a stable Error without consuming or masquerading as a Repair Attempt."""
    return {
        "status": "error",
        "error_kind": error_kind,
        "report": {
            "success": False,
            "phase": "error",
            "note": note,
        },
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
    try:
        summary = verifier.verify(
            run_id=state["run_id"],
            workspace=Path(state["workspace_path"]),
            argv=state["verification_argv"],
        )
    except (OSError, RuntimeError):
        return _infrastructure_failure(
            "verification_failure",
            "Baseline Verification infrastructure failed before a result was persisted.",
        )
    update: RunState = {
        "baseline_verification": summary.model_dump(mode="json"),
        "status": _BASELINE_STATUS[summary.outcome],
    }
    if summary.outcome == "passed":
        update["report"] = {
            "success": False,
            "phase": "issue_not_reproduced",
            "note": "Baseline Verification passed; the Issue was not reproduced.",
        }
    elif summary.outcome == "error":
        update["error_kind"] = summary.error_kind or "verification_error"
        update["report"] = {
            "success": False,
            "phase": "error",
            "note": "Baseline Verification ended with an infrastructure Error.",
        }
    if summary.outcome == "timeout":
        update.update(
            {
                "error_kind": "verification_timeout",
            }
        )
    return update


def route_after_baseline(state: RunState) -> str:
    """Continue only when exit code 1 demonstrated a reproducible Issue.

    A passing baseline means there is nothing safe to repair. Timeout and execution errors are
    also terminal because they do not provide a trustworthy failing test for model planning.
    """
    if state["status"] == "baseline_failed":
        return "create_plan"
    return "finish_without_repair"


def create_plan(state: RunState, planner: Planner) -> RunState:
    """Create or replay one runtime-validated Plan and its immutable artifact.

    The Planner owns the model request and exposes the workspace only through bounded tools.
    """
    try:
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
            prior_model_requests=state.get("model_requests", 0),
            prior_tool_executions=state.get("tool_executions", 0),
            previously_read=tuple(state.get("files_read", [])),
        )
    except InvalidModelOutputError as error:
        return _invalid_model_output(state, error)
    except ModelInvocationError as error:
        return _model_failure(state, error)
    except RunLimitExceededError as error:
        return _run_limit_reached(state, error)
    except (OSError, RuntimeError):
        return _infrastructure_failure(
            "storage_failure",
            "Plan artifact storage failed before planning completed.",
        )
    update: RunState = {
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
    return update


def route_after_plan(state: RunState) -> str:
    """Stop on a planning error; otherwise create a Candidate."""
    if state["status"] != "planned":
        return "plan_failed"
    return "candidate"


def create_candidate(state: RunState, builder: CandidatePatchBuilder) -> RunState:
    """Persist one validated Candidate Patch before the Approval Gate can interrupt."""
    plan_reference = PlanArtifactReference.model_validate(state["plan_artifact"])
    candidate_attempt = state.get("attempt", 0) + 1
    current_candidate_reference = (
        CandidatePatchReference.model_validate(state["candidate_artifact"])
        if "candidate_artifact" in state
        else None
    )
    try:
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
                and current_candidate_reference.path
                == f"attempts/{candidate_attempt}/candidate.json"
                else None
            ),
            prior_model_requests=state.get("model_requests", 0),
            prior_tool_executions=state.get("tool_executions", 0),
            previously_read=tuple(state.get("files_read", [])),
        )
    except InvalidModelOutputError as error:
        return _invalid_model_output(state, error)
    except ModelInvocationError as error:
        return _model_failure(state, error)
    except RunLimitExceededError as error:
        return _run_limit_reached(state, error)
    except (OSError, RuntimeError):
        return _infrastructure_failure(
            "storage_failure",
            "Candidate Patch artifact storage failed before Approval.",
        )
    update: RunState = {
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
    return update


def route_after_candidate(state: RunState) -> str:
    """Stop on a Candidate-generation error; otherwise await Approval."""
    if state["status"] != "pending_approval":
        return "candidate_failed"
    return "wait_for_approval"


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
    try:
        candidate_paths = set(
            applier.candidate_paths(run_id=state["run_id"], reference=reference)
        )
    except (OSError, RuntimeError, ValueError):
        return _infrastructure_failure(
            "storage_failure",
            "Approved Candidate Patch artifact could not be loaded safely.",
        )
    projected_files_changed = set(state.get("files_changed", [])) | candidate_paths
    if len(projected_files_changed) > MAX_FILES_CHANGED:
        return _run_limit_reached(
            state,
            RunLimitExceededError(
                limit_name="files_changed",
                limit=MAX_FILES_CHANGED,
                used=len(projected_files_changed),
            ),
        )
    try:
        summary = applier.apply_once(
            run_id=state["run_id"],
            workspace=Path(state["workspace_path"]),
            reference=reference,
        )
    except (OSError, RuntimeError):
        return _infrastructure_failure(
            "patching_failure",
            "Patch application infrastructure failed before Verification.",
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
    return "apply_failed"


def run_repair_verification(
    state: RunState,
    verifier: RepairVerifier,
    applier: PatchApplier,
) -> RunState:
    """Execute one approved Repair Attempt and classify its Verification result."""
    attempt = state.get("attempt", 0) + 1
    try:
        summary = verifier.verify(
            run_id=state["run_id"],
            workspace=Path(state["workspace_path"]),
            argv=state["verification_argv"],
            attempt=attempt,
        )
    except (OSError, RuntimeError):
        return _infrastructure_failure(
            "verification_failure",
            "Verification infrastructure failed before a result could be persisted.",
        )
    update: RunState = {
        "attempt": attempt,
        "verification": summary.model_dump(mode="json"),
    }
    if summary.outcome == "passed":
        try:
            cumulative = applier.persist_cumulative_diff(
                run_id=state["run_id"],
                workspace=Path(state["workspace_path"]),
                reference=CandidatePatchReference.model_validate(state["candidate_artifact"]),
            )
        except (OSError, RuntimeError, ValueError):
            update.update(_infrastructure_failure(
                "storage_failure",
                "Cumulative diff storage failed after successful Verification.",
            ))
            return update
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
        update["status"] = "error"
        update["error_kind"] = "verification_timeout"
    else:
        update["status"] = "error"
        update["error_kind"] = summary.error_kind or "verification_error"
    return update


def route_after_repair_verification(state: RunState) -> str:
    """Create a Diagnosis only for a failed Repair Attempt with budget remaining."""
    if state["status"] == "diagnosing":
        return "diagnose"
    return "finish_verification"


def create_diagnosis(state: RunState, diagnostician: Diagnostician) -> RunState:
    """Persist one typed Diagnosis against the current failing Run Workspace."""
    verification = RepairVerificationSummary.model_validate(state["verification"])
    current_diagnosis_reference = (
        DiagnosisArtifactReference.model_validate(state["diagnosis_artifact"])
        if "diagnosis_artifact" in state
        else None
    )
    try:
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
            prior_model_requests=state.get("model_requests", 0),
            prior_tool_executions=state.get("tool_executions", 0),
            previously_read=tuple(state.get("files_read", [])),
        )
    except InvalidModelOutputError as error:
        return _invalid_model_output(state, error)
    except ModelInvocationError as error:
        return _model_failure(state, error)
    except RunLimitExceededError as error:
        return _run_limit_reached(state, error)
    except (OSError, RuntimeError):
        return _infrastructure_failure(
            "storage_failure",
            "Diagnosis artifact storage failed before retry planning completed.",
        )
    update: RunState = {
        "diagnosis_artifact": result.reference.model_dump(mode="json"),
        "model_requests": state["model_requests"] + result.artifact.model_requests,
        "tool_executions": state["tool_executions"] + result.artifact.tool_executions,
        "files_read": sorted(set(state["files_read"]) | set(result.artifact.files_read)),
    }
    if verification.attempt >= MAX_REPAIR_ATTEMPTS:
        update.update(
            {
                "status": "attempts_exhausted",
                "report": {
                    "success": False,
                    "phase": "attempts_exhausted",
                    "note": (
                        f"{MAX_REPAIR_ATTEMPTS} approved Repair Attempts failed Verification."
                    ),
                },
            }
        )
    else:
        update["status"] = "diagnosed"
    return update


def route_after_diagnosis(state: RunState) -> str:
    """Stop after the third Diagnosis; otherwise propose the next Candidate Patch."""
    if state["status"] != "diagnosed":
        return "cannot_retry"
    return "retry"


def finalize_report(state: RunState, audit_store: RunAuditStore) -> RunState:
    """Persist the immutable terminal Run Report and retain only its reference in state."""
    reference = audit_store.finalize(state)
    return {"report_artifact": reference.model_dump(mode="json")}


def build_graph(
    *,
    baseline_verifier: BaselineVerifier,
    planner: Planner,
    candidate_builder: CandidatePatchBuilder,
    diagnostician: Diagnostician,
    patch_applier: PatchApplier,
    repair_verifier: RepairVerifier,
    audit_store: RunAuditStore,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Compile the Patch Run graph with durable or in-memory checkpointing.

    Production injects SQLite while focused graph tests may accept the in-memory default. The
    verifier is mandatory because baseline execution is an application-owned side-effect seam,
    not a global dependency hidden inside a node.
    """
    selected_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    builder = StateGraph(RunState)

    def registered(
        action: Callable[[RunState], RunState],
        key: Callable[[RunState], str],
    ) -> Callable[[RunState], RunState]:
        return lambda state: _audit_transition(
            state,
            action,
            audit_store,
            lambda _state, _update: key(state),
        )

    def fixed_key(name: str) -> Callable[[RunState], str]:
        return lambda _state: name

    builder.add_node("validate_input", registered(validate_input, fixed_key("validate")))
    builder.add_node(
        "baseline_verification",
        registered(
            lambda state: run_baseline_verification(state, baseline_verifier),
            fixed_key("baseline"),
        ),
    )
    builder.add_node(
        "create_plan",
        registered(lambda state: create_plan(state, planner), fixed_key("plan")),
    )
    builder.add_node(
        "create_candidate",
        registered(
            lambda state: create_candidate(state, candidate_builder),
            lambda state: f"candidate:{state.get('attempt', 0) + 1}",
        ),
    )
    builder.add_node(
        "approval_gate",
        lambda state: _audit_transition(
            state,
            await_approval,
            audit_store,
            lambda current, update: (
                f"approval:{current.get('attempt', 0) + 1}:{update['approval_decision']}"
            ),
        ),
    )
    builder.add_node(
        "reject_candidate",
        registered(reject_candidate, fixed_key("reject")),
    )
    builder.add_node(
        "apply_candidate",
        registered(
            lambda state: apply_candidate(state, patch_applier),
            lambda state: f"apply:{state.get('attempt', 0) + 1}",
        ),
    )
    builder.add_node(
        "repair_verification",
        registered(
            lambda state: run_repair_verification(state, repair_verifier, patch_applier),
            lambda state: f"verification:{state.get('attempt', 0) + 1}",
        ),
    )
    builder.add_node(
        "create_diagnosis",
        registered(
            lambda state: create_diagnosis(state, diagnostician),
            lambda state: f"diagnosis:{state.get('attempt', 0)}",
        ),
    )
    builder.add_node(
        "finalize_report",
        lambda state: finalize_report(state, audit_store),
    )
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "baseline_verification")
    builder.add_conditional_edges(
        "baseline_verification",
        route_after_baseline,
        {
            "create_plan": "create_plan",
            "finish_without_repair": "finalize_report",
        },
    )
    builder.add_conditional_edges(
        "create_plan",
        route_after_plan,
        {"candidate": "create_candidate", "plan_failed": "finalize_report"},
    )
    builder.add_conditional_edges(
        "create_candidate",
        route_after_candidate,
        {
            "wait_for_approval": "approval_gate",
            "candidate_failed": "finalize_report",
        },
    )
    builder.add_conditional_edges(
        "approval_gate",
        route_after_approval,
        {"reject": "reject_candidate", "approve": "apply_candidate"},
    )
    builder.add_edge("reject_candidate", "finalize_report")
    builder.add_conditional_edges(
        "apply_candidate",
        route_after_apply,
        {"verify": "repair_verification", "apply_failed": "finalize_report"},
    )
    builder.add_conditional_edges(
        "repair_verification",
        route_after_repair_verification,
        {
            "diagnose": "create_diagnosis",
            "finish_verification": "finalize_report",
        },
    )
    builder.add_conditional_edges(
        "create_diagnosis",
        route_after_diagnosis,
        {"retry": "create_candidate", "cannot_retry": "finalize_report"},
    )
    builder.add_edge("finalize_report", END)
    return builder.compile(checkpointer=selected_checkpointer)
