"""Serializable control state shared by LangGraph nodes and SQLite checkpoints.

``RunState`` contains bounded orchestration data only. Complete source files, model transcripts,
diffs, and Verification logs belong in Run Artifacts; keeping them out of state makes checkpoints
small enough to persist and replay predictably. ``total=False`` reflects progressive construction:
later fields such as a Plan do not exist on earlier or terminal branches.
"""

from typing import Literal, TypedDict

from patch_code_agent.sources import RepositorySourceKind

RunStatus = Literal[
    "created",
    "validated",
    "baseline_failed",
    "issue_not_reproduced",
    "budget_exceeded",
    "error",
    "inspected",
    "planned",
    "pending_approval",
    "rejected",
    "workspace_changed",
    "verification_failed",
    "diagnosing",
    "diagnosed",
    "attempts_exhausted",
    "succeeded",
    "editing",
    "testing",
    "passed",
    "failed",
]


class RunState(TypedDict, total=False):
    """Serializable state shared by every node in a PatchCodeAgent run.

    Because ``total=False``, fields appear progressively as nodes complete; consumers must only
    require fields guaranteed by their position in the graph.

    Attributes:
        run_id: Public UUID and LangGraph thread identifier for one Patch Run.
        source_kind: Whether content came from a bundled fixture or explicitly trusted repository.
        source_id: Stable, human-readable Repository Source identifier.
        source_revision: SHA-256 identity of the initial copied workspace tree.
        issue: Validated natural-language problem statement from the Patch Run Contract.
        verification_argv: Controlled command arguments executed without a shell.
        editable_paths: Contract paths future patch application is allowed to modify.
        protected_paths: Issue, manifest, and Verification files that Candidate Patches cannot
            replace even if a malformed editable allowlist includes them.
        model_requests: Durable count of model calls made so far; initialized before baseline.
        workspace_path: Absolute path to this Run's isolated mutable workspace.
        baseline_verification: Bounded serialized ``BaselineVerificationSummary``.
        plan_artifact: Path and checksum of the immutable runtime-validated Plan.
        candidate_artifact: Paths and checksums of the pending Candidate Patch and exact diff.
        tool_executions: Host-counted bounded inspection operations.
        files_read: Stable paths successfully read through the bounded tool interface.
        attempt: Zero-based repair-attempt counter used by later workflow milestones.
        approved: Whether the current candidate patch has passed the Approval Gate.
        approval_decision: Durable decision returned when the Approval Gate resumes.
        apply_summary: Replay-safe before/after classification for the approved Candidate Patch.
        verification: Bounded post-apply Verification summary for the current Repair Attempt.
        diagnosis_artifact: Path/checksum reference for the latest failed-attempt Diagnosis.
        files_changed: Stable paths changed by approved Candidate Patches.
        cumulative_diff: Path/checksum reference for the aggregate workspace repair.
        error_kind: Stable machine-readable terminal error category, when applicable.
        budget_name: Specific Resource Budget that caused Budget Exceeded.
        budget_limit: Configured limit for the exceeded Resource Budget.
        budget_used: Observed usage when the limit was exceeded.
        active_duration_seconds: Host-controlled execution time, excluding Approval wait.
        active_measurements: Replay-stable elapsed seconds keyed by logical node execution.
        status: Current lifecycle phase or terminal outcome used for routing and CLI status.
        report: Bounded structured progress/report data; complete evidence remains in artifacts.

    Example:
        A newly created Run only needs the fields required by the first graph nodes::

            state: RunState = {
                "run_id": "123e4567-e89b-12d3-a456-426614174000",
                "source_kind": "fixture",
                "source_id": "cart-discount",
                "source_revision": "a3f5...",
                "issue": "Fix the incorrect discount calculation",
                "verification_argv": ["pytest", "-q"],
                "editable_paths": ["cart.py"],
                "model_requests": 0,
                "workspace_path": "/tmp/runs/123e4567/workspace",
                "status": "created",
            }
    """

    run_id: str
    source_kind: RepositorySourceKind
    source_id: str
    source_revision: str
    issue: str
    verification_argv: list[str]
    editable_paths: list[str]
    protected_paths: list[str]
    model_requests: int
    workspace_path: str
    baseline_verification: dict[str, object]
    plan_artifact: dict[str, object]
    candidate_artifact: dict[str, object]
    tool_executions: int
    files_read: list[str]
    attempt: int
    approved: bool
    approval_decision: Literal["approve", "reject"]
    apply_summary: dict[str, object]
    verification: dict[str, object]
    diagnosis_artifact: dict[str, object]
    files_changed: list[str]
    cumulative_diff: dict[str, object]
    error_kind: str
    budget_name: str
    budget_limit: int | float
    budget_used: int | float
    active_duration_seconds: float
    active_measurements: dict[str, float]
    status: RunStatus
    report: dict[str, object]
