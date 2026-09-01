"""Create and replay one typed Diagnosis after a failed Repair Attempt."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from patch_code_agent.inspection import WorkspaceInspector
from patch_code_agent.model import Diagnosis, DiagnosisRequest, ModelGateway
from patch_code_agent.model_output import (
    InvalidModelOutputError,
    ModelInvocationError,
    request_typed_output,
)
from patch_code_agent.planning import PlanArtifactReference, load_plan_artifact
from patch_code_agent.verification import RepairVerificationSummary


class DiagnosisArtifact(BaseModel):
    """Checksummed model Diagnosis plus the bounded evidence and counters that produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    attempt: int = Field(ge=1, le=3)
    diagnosis: Diagnosis
    verification_output_excerpt: str
    verification_artifact_path: str
    model_id: str
    model_requests: int = Field(ge=1)
    tool_executions: int = Field(ge=0)
    files_read: tuple[str, ...]


class DiagnosisArtifactReference(BaseModel):
    """Bounded Checkpoint pointer to an immutable Diagnosis artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(pattern=r"^attempts/[1-3]/diagnosis\.json$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiagnosisResult(BaseModel):
    """Validated Diagnosis artifact and its durable reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: DiagnosisArtifact
    reference: DiagnosisArtifactReference


class Diagnostician:
    """Own the bounded model request and replay-safe Diagnosis artifact persistence."""

    def __init__(self, data_root: Path, model_gateway: ModelGateway) -> None:
        self._data_root = data_root.resolve()
        self._model_gateway = model_gateway

    def create_once(
        self,
        *,
        run_id: str,
        workspace: Path,
        issue: str,
        plan_reference: PlanArtifactReference,
        verification: RepairVerificationSummary,
        expected_reference: DiagnosisArtifactReference | None = None,
    ) -> DiagnosisResult:
        """Create one Diagnosis or validate and replay its completed ledger."""
        attempt_root = self._data_root / run_id / "attempts" / str(verification.attempt)
        artifact_path = attempt_root / "diagnosis.json"
        completion_path = attempt_root / ".diagnosis-complete"
        marker_path = attempt_root / ".diagnosis-in-progress"
        if artifact_path.exists() or completion_path.exists():
            return _load_completed_diagnosis(
                artifact_path,
                completion_path,
                expected_reference,
            )
        try:
            marker_path.touch(exist_ok=False)
        except FileExistsError as error:
            raise RuntimeError(
                "Diagnosis has an incomplete replay ledger; refusing a second model request"
            ) from error

        plan = load_plan_artifact(self._data_root, run_id, plan_reference).plan
        inspector = WorkspaceInspector(workspace)
        request = DiagnosisRequest(
            issue=issue,
            plan=plan,
            attempt=verification.attempt,
            verification_output_excerpt=verification.output_excerpt,
            verification_artifact_path=verification.artifact_path,
        )
        try:
            diagnosis, model_requests = request_typed_output(
                lambda: self._model_gateway.create_diagnosis(request, inspector),
                Diagnosis,
            )
        except (InvalidModelOutputError, ModelInvocationError) as error:
            error.record_inspection(
                tool_executions=inspector.tool_executions,
                files_read=inspector.files_read,
            )
            raise
        artifact = DiagnosisArtifact(
            attempt=verification.attempt,
            diagnosis=diagnosis,
            verification_output_excerpt=verification.output_excerpt,
            verification_artifact_path=verification.artifact_path,
            model_id=self._model_gateway.model_id,
            model_requests=model_requests,
            tool_executions=inspector.tool_executions,
            files_read=inspector.files_read,
        )
        artifact_bytes = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
        checksum = hashlib.sha256(artifact_bytes).hexdigest()
        artifact_path.write_bytes(artifact_bytes)
        marker_path.write_text(
            json.dumps({"diagnosis_sha256": checksum}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker_path.replace(completion_path)
        return DiagnosisResult(
            artifact=artifact,
            reference=DiagnosisArtifactReference(
                path=f"attempts/{verification.attempt}/diagnosis.json",
                sha256=checksum,
            ),
        )


def load_diagnosis_artifact(
    data_root: Path,
    run_id: str,
    reference: DiagnosisArtifactReference,
) -> DiagnosisResult:
    """Load a completed Diagnosis and validate its checkpoint identity."""
    artifact_path = data_root.resolve() / run_id / reference.path
    return _load_completed_diagnosis(
        artifact_path,
        artifact_path.parent / ".diagnosis-complete",
        reference,
    )


def _load_completed_diagnosis(
    artifact_path: Path,
    completion_path: Path,
    expected_reference: DiagnosisArtifactReference | None,
) -> DiagnosisResult:
    if not artifact_path.is_file() or not completion_path.is_file():
        raise RuntimeError("Diagnosis has an incomplete replay ledger; refusing a new request")
    artifact_bytes = artifact_path.read_bytes()
    checksum = hashlib.sha256(artifact_bytes).hexdigest()
    recorded = json.loads(completion_path.read_text(encoding="utf-8"))
    if recorded != {"diagnosis_sha256": checksum}:
        raise RuntimeError("Diagnosis does not match its replay completion checksum")
    artifact = DiagnosisArtifact.model_validate_json(artifact_bytes)
    reference = DiagnosisArtifactReference(
        path=f"attempts/{artifact.attempt}/diagnosis.json",
        sha256=checksum,
    )
    if expected_reference is not None and reference != expected_reference:
        raise RuntimeError("Diagnosis no longer matches its Checkpoint reference")
    return DiagnosisResult(artifact=artifact, reference=reference)
