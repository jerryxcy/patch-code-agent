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
    "editing",
    "testing",
    "passed",
    "failed",
]


class RunState(TypedDict, total=False):
    """Serializable state shared by every node in a PatchCodeAgent run."""

    run_id: str
    source_kind: RepositorySourceKind
    source_id: str
    source_revision: str
    issue: str
    verification_argv: list[str]
    editable_paths: list[str]
    model_requests: int
    workspace_path: str
    baseline_verification: dict[str, object]
    inspected_files: list[str]
    plan: list[str]
    attempt: int
    approved: bool
    status: RunStatus
    report: dict[str, object]
