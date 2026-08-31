import json
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_CHECKPOINT_EXCERPT_BYTES = 32 * 1024
_DEFAULT_TIMEOUT_SECONDS = 60.0
_PASSTHROUGH_ENVIRONMENT = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")
BaselineVerificationOutcome = Literal["failed", "passed", "error", "timeout"]


class BaselineVerificationSummary(BaseModel):
    """Bounded Checkpoint representation of baseline Verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: BaselineVerificationOutcome
    exit_code: int | None
    duration_seconds: float = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    output_excerpt: str
    output_truncated: bool
    artifact_path: str
    error_kind: str | None = None


class BaselineVerifier:
    """Executes baseline Verification and persists its complete Run Artifacts."""

    def __init__(
        self,
        data_root: Path,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._data_root = data_root.resolve()
        self._timeout_seconds = timeout_seconds

    def verify(
        self,
        *,
        run_id: str,
        workspace: Path,
        argv: list[str],
    ) -> BaselineVerificationSummary:
        baseline_root = self._data_root / run_id / "baseline"
        try:
            baseline_root.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            return self._load_persisted_summary(baseline_root)

        started = time.monotonic()
        exit_code: int | None = None
        error_kind: str | None = None
        outcome: BaselineVerificationOutcome
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                cwd=workspace,
                env=_minimal_environment(),
                capture_output=True,
                check=False,
                timeout=self._timeout_seconds,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
            outcome = _classify_exit_code(exit_code)
            if outcome == "error":
                error_kind = "verification_exit_code"
        except subprocess.TimeoutExpired as error:
            stdout = _as_bytes(error.stdout)
            stderr = _as_bytes(error.stderr)
            outcome = "timeout"
            error_kind = "verification_timeout"
        except OSError as error:
            stdout = b""
            stderr = str(error).encode("utf-8", errors="replace")
            outcome = "error"
            error_kind = "verification_process"

        duration_seconds = time.monotonic() - started
        artifact_path = "baseline/output.log"
        summary = BaselineVerificationSummary(
            outcome=outcome,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            timeout_seconds=self._timeout_seconds,
            output_excerpt=_output_excerpt(stdout, stderr),
            output_truncated=len(_combined_output(stdout, stderr)) > _CHECKPOINT_EXCERPT_BYTES,
            artifact_path=artifact_path,
            error_kind=error_kind,
        )
        self._persist_artifacts(baseline_root, stdout, stderr, summary)
        return summary

    def _load_persisted_summary(self, baseline_root: Path) -> BaselineVerificationSummary:
        result_path = baseline_root / "result.json"
        output_path = baseline_root / "output.log"
        if not result_path.is_file() or not output_path.is_file():
            raise RuntimeError(
                "Baseline Verification has an incomplete replay ledger; refusing to rerun"
            )
        return BaselineVerificationSummary.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )

    def _persist_artifacts(
        self,
        baseline_root: Path,
        stdout: bytes,
        stderr: bytes,
        summary: BaselineVerificationSummary,
    ) -> None:
        output = b"--- stdout ---\n" + stdout + b"\n--- stderr ---\n" + stderr
        (baseline_root / "output.log").write_bytes(output)
        (baseline_root / "result.json").write_text(
            json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _minimal_environment() -> dict[str, str]:
    environment = {
        key: value for key in _PASSTHROUGH_ENVIRONMENT if (value := os.environ.get(key))
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def _classify_exit_code(exit_code: int) -> Literal["failed", "passed", "error"]:
    if exit_code == 0:
        return "passed"
    if exit_code == 1:
        return "failed"
    return "error"


def _output_excerpt(stdout: bytes, stderr: bytes) -> str:
    return _combined_output(stdout, stderr)[:_CHECKPOINT_EXCERPT_BYTES].decode(
        "utf-8",
        errors="ignore",
    )


def _combined_output(stdout: bytes, stderr: bytes) -> bytes:
    return b"stdout:\n" + stdout + b"\nstderr:\n" + stderr


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")
