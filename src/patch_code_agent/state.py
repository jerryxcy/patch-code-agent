from typing import Literal, TypedDict


class RunState(TypedDict, total=False):
    """Serializable state shared by every node in a PatchCodeAgent run."""

    issue: str
    repo_path: str
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
