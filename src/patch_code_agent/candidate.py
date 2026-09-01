"""Validate, diff, and persist one immutable Candidate Patch per Repair Attempt."""

import difflib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from patch_code_agent.diagnosis import DiagnosisArtifactReference, load_diagnosis_artifact
from patch_code_agent.inspection import WorkspaceInspector
from patch_code_agent.model import CandidatePatch, CandidateRequest, ModelGateway
from patch_code_agent.model_output import (
    InvalidModelOutputError,
    ModelInvocationError,
    request_typed_output,
)
from patch_code_agent.planning import PlanArtifactReference, load_plan_artifact

_MAX_REPLACEMENT_BYTES = 100 * 1024


class CandidatePatchArtifact(BaseModel):
    """Validated structured replacement plus host-computed diff identity and counters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    attempt: int = Field(ge=1, le=3)
    candidate: CandidatePatch
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str
    model_requests: int = Field(ge=1)
    tool_executions: int = Field(ge=0)
    files_read: tuple[str, ...]


class CandidatePatchReference(BaseModel):
    """Bounded Checkpoint pointer to immutable Candidate Patch JSON and exact diff files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(pattern=r"^attempts/[1-3]/candidate\.json$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diff_path: str = Field(pattern=r"^attempts/[1-3]/candidate\.diff$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _CandidateArtifactPaths:
    """Keep one attempt's Candidate Artifact path family together."""

    attempt_root: Path

    @classmethod
    def for_attempt(cls, run_root: Path, attempt: int) -> "_CandidateArtifactPaths":
        return cls(run_root / "attempts" / str(attempt))

    @classmethod
    def from_reference(
        cls,
        run_root: Path,
        reference: CandidatePatchReference,
    ) -> "_CandidateArtifactPaths":
        return cls((run_root / reference.path).parent)

    @property
    def candidate(self) -> Path:
        return self.attempt_root / "candidate.json"

    @property
    def diff(self) -> Path:
        return self.attempt_root / "candidate.diff"

    @property
    def completion(self) -> Path:
        return self.attempt_root / ".candidate-complete"


class CandidatePatchResult(BaseModel):
    """Candidate artifact, exact diff, and durable reference returned by creation or replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: CandidatePatchArtifact
    diff: str
    reference: CandidatePatchReference


class CandidatePatchBuilder:
    """Own Candidate model invocation, host validation, diffing, and replay-safe persistence."""

    def __init__(self, data_root: Path, model_gateway: ModelGateway) -> None:
        self._data_root = data_root.resolve()
        self._model_gateway = model_gateway

    def create_once(
        self,
        *,
        run_id: str,
        workspace: Path,
        issue: str,
        editable_paths: list[str],
        protected_paths: list[str] | tuple[str, ...] = (),
        plan_reference: PlanArtifactReference,
        attempt: int,
        diagnosis_reference: DiagnosisArtifactReference | None = None,
        expected_reference: CandidatePatchReference | None = None,
    ) -> CandidatePatchResult:
        """Create one Candidate Patch or load the completed artifact during graph replay."""
        run_root = self._data_root / run_id
        paths = _CandidateArtifactPaths.for_attempt(run_root, attempt)
        if paths.attempt_root.exists():
            return _load_completed_result(paths, expected_reference)

        paths.attempt_root.parent.mkdir(exist_ok=True)
        try:
            paths.attempt_root.mkdir(exist_ok=False)
        except FileExistsError:
            return _load_completed_result(paths, expected_reference)

        inspector = WorkspaceInspector(workspace)
        plan = load_plan_artifact(self._data_root, run_id, plan_reference).plan
        request = CandidateRequest(
            issue=issue,
            plan=plan,
            editable_paths=tuple(editable_paths),
            attempt=attempt,
            diagnosis=(
                load_diagnosis_artifact(
                    self._data_root, run_id, diagnosis_reference
                ).artifact.diagnosis
                if diagnosis_reference is not None
                else None
            ),
        )
        try:
            candidate, model_requests = request_typed_output(
                lambda: self._model_gateway.create_candidate(request, inspector),
                CandidatePatch,
            )
        except (InvalidModelOutputError, ModelInvocationError) as error:
            error.record_inspection(
                tool_executions=inspector.tool_executions,
                files_read=inspector.files_read,
            )
            raise
        candidate, exact_diff = _validate_and_diff(
            workspace=workspace,
            editable_paths=editable_paths,
            protected_paths=protected_paths,
            candidate=candidate,
            inspector=inspector,
        )
        diff_bytes = exact_diff.encode("utf-8")
        diff_checksum = hashlib.sha256(diff_bytes).hexdigest()
        artifact = CandidatePatchArtifact(
            attempt=attempt,
            candidate=candidate,
            diff_sha256=diff_checksum,
            model_id=self._model_gateway.model_id,
            model_requests=model_requests,
            tool_executions=inspector.tool_executions,
            files_read=inspector.files_read,
        )
        artifact_bytes = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
        candidate_checksum = hashlib.sha256(artifact_bytes).hexdigest()
        paths.diff.write_bytes(diff_bytes)
        paths.candidate.write_bytes(artifact_bytes)
        paths.completion.write_text(
            json.dumps(
                {"candidate_sha256": candidate_checksum, "diff_sha256": diff_checksum},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return CandidatePatchResult(
            artifact=artifact,
            diff=exact_diff,
            reference=CandidatePatchReference(
                path=f"attempts/{attempt}/candidate.json",
                sha256=candidate_checksum,
                diff_path=f"attempts/{attempt}/candidate.diff",
                diff_sha256=diff_checksum,
            ),
        )


def load_candidate_patch(
    data_root: Path,
    run_id: str,
    reference: CandidatePatchReference,
) -> CandidatePatchResult:
    """Load a completed Candidate Patch and verify both durable artifact checksums."""
    run_root = data_root.resolve() / run_id
    paths = _CandidateArtifactPaths.from_reference(run_root, reference)
    return _load_completed_result(paths, reference)


def _validate_and_diff(
    *,
    workspace: Path,
    editable_paths: list[str],
    protected_paths: list[str] | tuple[str, ...],
    candidate: CandidatePatch,
    inspector: WorkspaceInspector,
) -> tuple[CandidatePatch, str]:
    editable = set(editable_paths)
    protected = set(protected_paths)
    read_hashes = inspector.read_hashes
    observed_paths: set[str] = set()
    validated_replacements = []
    diff_parts: list[str] = []
    for replacement in sorted(candidate.replacements, key=lambda item: item.path):
        if replacement.path in observed_paths:
            raise ValueError(f"Candidate Patch contains duplicate path: {replacement.path}")
        observed_paths.add(replacement.path)
        if replacement.path in protected or _is_test_path(replacement.path):
            raise ValueError(f"Candidate Patch path is protected: {replacement.path}")
        if replacement.path not in editable:
            raise ValueError(f"Candidate Patch path is not editable: {replacement.path}")
        if replacement.path not in read_hashes:
            raise ValueError(
                f"Candidate Patch path was not explicitly read by the model: {replacement.path}"
            )
        if replacement.expected_sha256 != read_hashes[replacement.path]:
            raise ValueError(
                f"Candidate Patch expected hash does not match the model read: {replacement.path}"
            )

        current = WorkspaceInspector(workspace).read_file(replacement.path).content
        current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if current_hash != replacement.expected_sha256:
            raise ValueError(f"Run Workspace changed after the model read: {replacement.path}")
        new_bytes = replacement.new_content.encode("utf-8")
        if not new_bytes:
            raise ValueError(f"Candidate Patch cannot delete file content: {replacement.path}")
        if len(new_bytes) > _MAX_REPLACEMENT_BYTES:
            raise ValueError(f"Candidate Patch replacement exceeds 100 KiB: {replacement.path}")
        if b"\x00" in new_bytes:
            raise ValueError(f"Candidate Patch replacement must be text: {replacement.path}")
        if replacement.new_content == current:
            raise ValueError(f"Candidate Patch replacement must change content: {replacement.path}")
        diff_parts.append(
            render_unified_diff(
                path=replacement.path,
                before=current,
                after=replacement.new_content,
            )
        )
        validated_replacements.append(replacement)
    return CandidatePatch(replacements=tuple(validated_replacements)), "".join(diff_parts)


def _is_test_path(path: str) -> bool:
    """Recognize conventional test paths that must stay outside Candidate Patches."""
    parsed = PurePosixPath(path)
    name = parsed.name.lower()
    return (
        any(part.lower() in {"test", "tests", "spec", "specs"} for part in parsed.parts[:-1])
        or name == "conftest.py"
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def render_unified_diff(*, path: str, before: str, after: str) -> str:
    """Return an applicable unified diff, including missing-newline markers."""
    rendered: list[str] = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ):
        if line.endswith("\n"):
            rendered.append(line)
            continue
        rendered.append(line + "\n")
        rendered.append("\\ No newline at end of file\n")
    return "".join(rendered)


def _load_completed_result(
    paths: _CandidateArtifactPaths,
    expected_reference: CandidatePatchReference | None,
) -> CandidatePatchResult:
    if not paths.candidate.is_file() or not paths.diff.is_file() or not paths.completion.is_file():
        raise RuntimeError(
            "Candidate Patch has an incomplete replay ledger; refusing a new request"
        )
    artifact_bytes = paths.candidate.read_bytes()
    diff_bytes = paths.diff.read_bytes()
    artifact = CandidatePatchArtifact.model_validate_json(artifact_bytes)
    candidate_checksum = hashlib.sha256(artifact_bytes).hexdigest()
    diff_checksum = hashlib.sha256(diff_bytes).hexdigest()
    recorded = json.loads(paths.completion.read_text(encoding="utf-8"))
    if recorded != {
        "candidate_sha256": candidate_checksum,
        "diff_sha256": diff_checksum,
    }:
        raise RuntimeError("Candidate Patch does not match its replay completion checksums")
    if artifact.diff_sha256 != diff_checksum:
        raise RuntimeError("Candidate Patch Artifact does not identify its exact diff")
    attempt = artifact.attempt
    reference = CandidatePatchReference(
        path=f"attempts/{attempt}/candidate.json",
        sha256=candidate_checksum,
        diff_path=f"attempts/{attempt}/candidate.diff",
        diff_sha256=diff_checksum,
    )
    if expected_reference is not None and reference != expected_reference:
        raise RuntimeError("Candidate Patch does not match its durable Checkpoint reference")
    return CandidatePatchResult(
        artifact=artifact,
        diff=diff_bytes.decode("utf-8"),
        reference=reference,
    )
