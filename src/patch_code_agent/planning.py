"""Create one validated, checksummed Plan artifact through bounded inspection."""

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from patch_code_agent.inspection import WorkspaceInspector
from patch_code_agent.model import ModelGateway, Plan, PlanningRequest


class PlanArtifact(BaseModel):
    """Human-inspectable Plan plus counters needed for replay-safe state.

    Attributes:
        schema_version: Artifact contract version, currently ``1``.
        plan: Runtime-validated structured model output.
        model_id: Adapter identity responsible for the Plan.
        model_requests: Actual requests consumed while producing this artifact.
        tool_executions: Host-counted list/read/search operations.
        files_read: Stable paths successfully returned by ``read_file``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    plan: Plan
    model_id: str
    model_requests: int = Field(ge=1)
    tool_executions: int = Field(ge=0)
    files_read: tuple[str, ...]


class PlanArtifactReference(BaseModel):
    """Bounded Checkpoint pointer to an immutable Plan artifact.

    Attributes:
        path: Run-relative artifact location, fixed to ``plan.json``.
        sha256: Digest of the exact persisted artifact bytes.

    Example:
        >>> reference = PlanArtifactReference(sha256="a" * 64)
        >>> reference.path
        'plan.json'
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Literal["plan.json"] = "plan.json"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanningResult(BaseModel):
    """Validated artifact and reference returned after planning or replay.

    Attributes:
        artifact: Parsed Plan and inspection/model counters.
        reference: Small checksummed pointer suitable for the Checkpoint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: PlanArtifact
    reference: PlanArtifactReference


class Planner:
    """Own model invocation, inspection authority, validation, and persistence."""

    def __init__(self, data_root: Path, model_gateway: ModelGateway) -> None:
        self._data_root = data_root.resolve()
        self._model_gateway = model_gateway

    def create_once(
        self,
        *,
        run_id: str,
        workspace: Path,
        issue: str,
        verification: list[str],
        expected_reference: PlanArtifactReference | None = None,
    ) -> PlanningResult:
        """Create one Plan or load its completed artifact during graph replay."""
        run_root = self._data_root / run_id
        artifact_path = run_root / "plan.json"
        completion_path = run_root / ".plan-complete"
        if artifact_path.is_file():
            return _load_completed_result(artifact_path, completion_path, expected_reference)
        if completion_path.exists():
            raise RuntimeError("Plan replay ledger is complete but its artifact is missing")

        marker = run_root / ".plan-in-progress"
        try:
            marker.touch(exist_ok=False)
        except FileExistsError as error:
            raise RuntimeError("Plan replay ledger is incomplete; refusing a second model request") from error

        inspector = WorkspaceInspector(workspace)
        request = PlanningRequest(issue=issue, verification=tuple(verification))
        raw_plan = self._model_gateway.create_plan(request, inspector)
        plan = Plan.model_validate(raw_plan)
        artifact = PlanArtifact(
            plan=plan,
            model_id=self._model_gateway.model_id,
            model_requests=1,
            tool_executions=inspector.tool_executions,
            files_read=inspector.files_read,
        )
        artifact_bytes = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
        artifact_path.write_bytes(artifact_bytes)
        checksum = hashlib.sha256(artifact_bytes).hexdigest()
        marker.write_text(checksum + "\n", encoding="utf-8")
        marker.replace(completion_path)
        return PlanningResult(
            artifact=artifact,
            reference=PlanArtifactReference(sha256=checksum),
        )


def load_plan_artifact(
    data_root: Path,
    run_id: str,
    reference: PlanArtifactReference,
) -> PlanArtifact:
    """Read an artifact and verify that it still matches its durable checksum."""
    result = _load_result(data_root.resolve() / run_id / reference.path)
    if result.reference.sha256 != reference.sha256:
        raise ValueError("Plan Artifact checksum does not match durable state")
    return result.artifact


def _load_result(path: Path) -> PlanningResult:
    artifact_bytes = path.read_bytes()
    return PlanningResult(
        artifact=PlanArtifact.model_validate_json(artifact_bytes),
        reference=PlanArtifactReference(sha256=hashlib.sha256(artifact_bytes).hexdigest()),
    )


def _load_completed_result(
    artifact_path: Path,
    completion_path: Path,
    expected_reference: PlanArtifactReference | None,
) -> PlanningResult:
    if not completion_path.is_file():
        raise RuntimeError("Plan replay ledger is incomplete; refusing to trust its artifact")
    result = _load_result(artifact_path)
    recorded_checksum = completion_path.read_text(encoding="utf-8").strip()
    if recorded_checksum != result.reference.sha256:
        raise RuntimeError("Plan Artifact does not match its replay completion checksum")
    if expected_reference is not None and expected_reference != result.reference:
        raise RuntimeError("Plan Artifact does not match its durable Checkpoint reference")
    return result
