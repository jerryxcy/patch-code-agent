"""Execute the Patch Run Contract's baseline Verification command.

Verification runs repository-controlled code with host authority, so this module owns the
entire subprocess boundary: argv execution without a shell, a deliberately small environment,
the working directory, timeout handling, and exit-code classification. Complete stdout and
stderr remain filesystem Run Artifacts; only a bounded summary is returned to LangGraph for
SQLite checkpointing.

The artifact directory also acts as a replay ledger. A completed result is reused on replay,
while a partially written ledger fails closed instead of executing repository code twice.
"""

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
    """Small, serializable result stored in the LangGraph Checkpoint.

    ``output_excerpt`` is intentionally capped at 32 KiB. Consumers that need complete process
    output must follow ``artifact_path`` rather than expanding durable control state.

    Attributes:
        outcome: Normalized result used by graph routing: ``failed``, ``passed``, ``error``, or
            ``timeout``.
        exit_code: Subprocess return code, or ``None`` when the command timed out or could not
            start.
        duration_seconds: Monotonic elapsed time spent executing or attempting the command.
        timeout_seconds: Execution budget applied to this Verification attempt.
        output_excerpt: Combined stdout/stderr prefix stored in bounded checkpoint state.
        output_truncated: Whether complete output exceeded the excerpt's 32 KiB byte budget.
        artifact_path: Run-relative path to the complete stdout/stderr log.
        error_kind: Stable machine-readable error category, or ``None`` for normal pass/fail.

    Example:
        >>> summary = BaselineVerificationSummary(
        ...     outcome="failed",
        ...     exit_code=1,
        ...     duration_seconds=0.42,
        ...     timeout_seconds=60.0,
        ...     output_excerpt="stdout:\\n...\\nstderr:\\n1 failed",
        ...     output_truncated=False,
        ...     artifact_path="baseline/output.log",
        ... )
        >>> summary.outcome
        'failed'
    """

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
    """Execute baseline Verification once per Run Identifier.

    The verifier is source-neutral: it only receives an already validated argv and workspace,
    so bundled fixtures and explicitly trusted repositories use the same execution path.
    """

    def __init__(
        self,
        data_root: Path,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Configure artifact storage and the per-command execution budget."""
        self._data_root = data_root.resolve()
        self._timeout_seconds = timeout_seconds

    def verify(
        self,
        *,
        run_id: str,
        workspace: Path,
        argv: list[str],
    ) -> BaselineVerificationSummary:
        """Execute a contract argv once, persist output, and return bounded state.

        Exit code 0 means the Issue was not reproduced, exit code 1 means a normal failing
        baseline, and every other exit code is an execution error. Timeout and process-start
        failures are represented as results as well, allowing the graph to reach a durable
        terminal state instead of losing the Run to an exception.

        The per-run ``baseline`` directory is claimed before starting the subprocess. If the
        node is replayed after successful persistence, the existing result is returned. If a
        crash left the directory incomplete, execution is refused because rerunning an external
        side effect would be less safe than stopping for operator inspection.
        """
        baseline_root = self._data_root / run_id / "baseline"
        # Directory creation atomically claims this side effect. Replays consume the
        # completed ledger instead of executing repository code a second time.
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
        """Load and validate an earlier result without re-executing its command.

        Both files are required: ``result.json`` proves classification completed and
        ``output.log`` preserves the auditable output promised by the Run Artifact contract.
        """
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
        """Persist complete output separately from the bounded checkpoint summary.

        Stream labels make empty stdout or stderr unambiguous during later inspection. The JSON
        file mirrors the summary returned to the graph and never contains the unbounded output.
        """
        output = b"--- stdout ---\n" + stdout + b"\n--- stderr ---\n" + stderr
        (baseline_root / "output.log").write_bytes(output)
        (baseline_root / "result.json").write_text(
            json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _minimal_environment() -> dict[str, str]:
    """Build an allowlisted environment without host credentials or tokens.

    PATH is needed to resolve contract commands such as ``pytest`` and temporary-directory
    variables keep common tools functional. Everything else is dropped by default; Python flags
    make captured output deterministic and prevent runtime cache files in the workspace.
    """
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
    """Interpret pytest-style exit codes for baseline routing."""
    if exit_code == 0:
        return "passed"
    if exit_code == 1:
        return "failed"
    return "error"


def _output_excerpt(stdout: bytes, stderr: bytes) -> str:
    """Decode at most the checkpoint byte budget from combined process output.

    Slicing happens before UTF-8 decoding so the limit is measured in serialized bytes rather
    than Python characters. An incomplete trailing code point is discarded safely.
    """
    return _combined_output(stdout, stderr)[:_CHECKPOINT_EXCERPT_BYTES].decode(
        "utf-8",
        errors="ignore",
    )


def _combined_output(stdout: bytes, stderr: bytes) -> bytes:
    """Label streams before constructing their bounded checkpoint representation."""
    return b"stdout:\n" + stdout + b"\nstderr:\n" + stderr


def _as_bytes(value: bytes | str | None) -> bytes:
    """Normalize the partial output types exposed by ``TimeoutExpired``."""
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")
