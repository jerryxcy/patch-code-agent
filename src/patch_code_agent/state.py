from typing import Literal, TypedDict

from patch_code_agent.sources import RepositorySourceKind


class RunState(TypedDict, total=False):
    """Serializable state shared by every node in a PatchCodeAgent run."""

    run_id: str
    source_kind: RepositorySourceKind
    source_id: str
    source_revision: str
    issue: str
    verification_argv: list[str]
    editable_paths: list[str]
    workspace_path: str
    inspected_files: list[str]
    plan: list[str]
    attempt: int
    approved: bool
    status: Literal[
        "created",
        "validated",
        "inspected",
        "planned",
        "editing",
        "testing",
        "passed",
        "failed",
    ]
    report: dict[str, object]
