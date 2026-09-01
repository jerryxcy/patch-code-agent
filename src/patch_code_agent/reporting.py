"""Persist replay-safe Run Events and one immutable terminal Run Report."""

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from patch_code_agent.budgets import ResourceBudgets
from patch_code_agent.state import RunState

TerminalOutcome = Literal[
    "succeeded",
    "rejected",
    "issue_not_reproduced",
    "attempts_exhausted",
    "budget_exceeded",
    "workspace_changed",
    "error",
]
TERMINAL_OUTCOMES: frozenset[str] = frozenset(get_args(TerminalOutcome))
_TERMINAL_OUTCOME_ADAPTER = TypeAdapter(TerminalOutcome)


class ArtifactReference(BaseModel):
    """Run-relative path and checksum for one immutable Run Artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunEvent(BaseModel):
    """One stable, append-only record of a meaningful graph transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str
    transition: str
    status: str
    occurred_at: str
    attempt: int = Field(ge=0)
    model_requests: int = Field(ge=0)
    tool_executions: int = Field(ge=0)


class VerificationRecord(BaseModel):
    """Bounded Verification summary plus checksummed result and complete log references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int | None
    outcome: str
    exit_code: int | None
    duration_seconds: float = Field(ge=0)
    summary: ArtifactReference
    log: ArtifactReference


class VerificationHistory(BaseModel):
    """Baseline and ordered Repair Verification records for one Patch Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: VerificationRecord | None
    attempts: tuple[VerificationRecord, ...]


class ArtifactInventory(BaseModel):
    """All human-inspectable artifacts needed to audit the Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: ArtifactReference
    plan: ArtifactReference | None
    diagnoses: tuple[ArtifactReference, ...]
    candidates: tuple[ArtifactReference, ...]
    diffs: tuple[ArtifactReference, ...]
    logs: tuple[ArtifactReference, ...]
    cumulative_diff: ArtifactReference | None


class RunReport(BaseModel):
    """Versioned, complete terminal audit contract for one Patch Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    run_id: str
    source_kind: str
    source_id: str
    source_revision: str
    issue: str
    model_id: str
    outcome: TerminalOutcome
    terminal_reason: str | None
    error_kind: str | None
    started_at: str
    finished_at: str
    active_duration_seconds: float = Field(ge=0)
    attempts: int = Field(ge=0, le=3)
    model_requests: int = Field(ge=0)
    tool_executions: int = Field(ge=0)
    files_read: tuple[str, ...]
    files_changed: tuple[str, ...]
    verification: VerificationHistory
    artifacts: ArtifactInventory
    budgets: ResourceBudgets


class RunReportReference(BaseModel):
    """Bounded Checkpoint reference to the immutable terminal report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Literal["report.json"] = "report.json"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunAuditStore:
    """Own append-only events and immutable terminal report persistence."""

    def __init__(
        self,
        data_root: Path,
        *,
        timestamp_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._data_root = data_root.resolve()
        self._timestamp_factory = timestamp_factory or (lambda: datetime.now(UTC))

    def append_event(self, state: Mapping[str, object], transition: str) -> RunEvent:
        """Append one stable event unless replay already persisted its exact identifier."""
        run_id = str(state["run_id"])
        event_id = hashlib.sha256(f"{run_id}:{transition}".encode()).hexdigest()
        path = self._data_root / run_id / "events.jsonl"
        existing = self._read_events(path)
        for event in existing:
            if event.event_id == event_id:
                expected = (
                    run_id,
                    transition,
                    str(state["status"]),
                    int(state.get("attempt", 0)),
                    int(state.get("model_requests", 0)),
                    int(state.get("tool_executions", 0)),
                )
                observed = (
                    event.run_id,
                    event.transition,
                    event.status,
                    event.attempt,
                    event.model_requests,
                    event.tool_executions,
                )
                if observed != expected:
                    raise RuntimeError("Run Event replay does not match its persisted record")
                return event
        event = RunEvent(
            event_id=event_id,
            run_id=run_id,
            transition=transition,
            status=str(state["status"]),
            occurred_at=self._timestamp_factory().astimezone(UTC).isoformat().replace("+00:00", "Z"),
            attempt=int(state.get("attempt", 0)),
            model_requests=int(state.get("model_requests", 0)),
            tool_executions=int(state.get("tool_executions", 0)),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def finalize(self, state: RunState) -> RunReportReference:
        """Write or replay-validate the one immutable report for a terminal state."""
        if state["status"] not in TERMINAL_OUTCOMES:
            raise ValueError(f"Cannot finalize non-terminal Patch Run: {state['status']}")
        self.append_event(state, f"finalized:{state['status']}")
        report = self._build_report(state)
        report_bytes = (report.model_dump_json(indent=2) + "\n").encode()
        checksum = hashlib.sha256(report_bytes).hexdigest()
        run_root = self._data_root / state["run_id"]
        report_path = run_root / "report.json"
        completion_path = run_root / ".report-complete"
        if report_path.is_file():
            existing = report_path.read_bytes()
            if existing != report_bytes:
                raise RuntimeError("Run Report does not match its replay completion checksum")
            if completion_path.is_file():
                recorded = completion_path.read_text(encoding="utf-8").strip()
                if hashlib.sha256(existing).hexdigest() != recorded:
                    raise RuntimeError("Run Report completion checksum does not match")
                return RunReportReference(sha256=recorded)
            completion_path.write_text(checksum + "\n", encoding="utf-8")
            return RunReportReference(sha256=checksum)
        if completion_path.exists():
            raise RuntimeError("Run Report completion marker exists without its report")
        report_path.write_bytes(report_bytes)
        completion_path.write_text(checksum + "\n", encoding="utf-8")
        return RunReportReference(sha256=checksum)

    def _build_report(self, state: RunState) -> RunReport:
        run_root = self._data_root / state["run_id"]
        events = self._read_events(run_root / "events.jsonl")
        if not events:
            raise RuntimeError("Run Report requires at least one Run Event")
        verification = self._verification_history(run_root, state)
        logs = tuple(
            record.log
            for record in ((verification.baseline,) + verification.attempts)
            if record is not None
        )
        report_note = state.get("report", {}).get("note")
        terminal_reason = (
            str(report_note)
            if report_note is not None
            else cast(str | None, state.get("budget_name") or state.get("error_kind"))
        )
        return RunReport(
            run_id=state["run_id"],
            source_kind=state["source_kind"],
            source_id=state["source_id"],
            source_revision=state["source_revision"],
            issue=state["issue"],
            model_id=state["model_id"],
            outcome=_TERMINAL_OUTCOME_ADAPTER.validate_python(state["status"]),
            terminal_reason=terminal_reason,
            error_kind=state.get("error_kind"),
            started_at=events[0].occurred_at,
            finished_at=events[-1].occurred_at,
            active_duration_seconds=state.get("active_duration_seconds", 0.0),
            attempts=state.get("attempt", 0),
            model_requests=state.get("model_requests", 0),
            tool_executions=state.get("tool_executions", 0),
            files_read=tuple(state.get("files_read", [])),
            files_changed=tuple(state.get("files_changed", [])),
            verification=verification,
            artifacts=ArtifactInventory(
                events=self._reference(run_root / "events.jsonl", run_root),
                plan=self._optional_reference(run_root / "plan.json", run_root),
                diagnoses=self._attempt_references(run_root, "diagnosis.json"),
                candidates=self._attempt_references(run_root, "candidate.json"),
                diffs=self._attempt_references(run_root, "candidate.diff"),
                logs=logs,
                cumulative_diff=self._optional_reference(
                    run_root / "cumulative.diff", run_root
                ),
            ),
            budgets=ResourceBudgets.from_state(state),
        )

    def _verification_history(
        self,
        run_root: Path,
        state: RunState,
    ) -> VerificationHistory:
        baseline_summary = state.get("baseline_verification")
        baseline = (
            self._verification_record(
                run_root,
                cast(Mapping[str, object], baseline_summary),
                attempt=None,
                summary_path="baseline/result.json",
            )
            if baseline_summary is not None
            and (run_root / "baseline" / "result.json").is_file()
            else None
        )
        attempts: list[VerificationRecord] = []
        for attempt in range(1, 4):
            path = run_root / "attempts" / str(attempt) / "verification.json"
            if not path.is_file():
                continue
            summary = cast(Mapping[str, object], json.loads(path.read_text(encoding="utf-8")))
            attempts.append(
                self._verification_record(
                    run_root,
                    summary,
                    attempt=attempt,
                    summary_path=f"attempts/{attempt}/verification.json",
                )
            )
        return VerificationHistory(baseline=baseline, attempts=tuple(attempts))

    def _verification_record(
        self,
        run_root: Path,
        summary: Mapping[str, object],
        *,
        attempt: int | None,
        summary_path: str,
    ) -> VerificationRecord:
        log_path = str(summary["artifact_path"])
        return VerificationRecord(
            attempt=attempt,
            outcome=str(summary["outcome"]),
            exit_code=cast(int | None, summary.get("exit_code")),
            duration_seconds=float(summary["duration_seconds"]),
            summary=self._reference(run_root / summary_path, run_root),
            log=self._reference(run_root / log_path, run_root),
        )

    def _attempt_references(
        self,
        run_root: Path,
        filename: str,
    ) -> tuple[ArtifactReference, ...]:
        references = []
        for attempt in range(1, 4):
            reference = self._optional_reference(
                run_root / "attempts" / str(attempt) / filename,
                run_root,
            )
            if reference is not None:
                references.append(reference)
        return tuple(references)

    @staticmethod
    def _reference(path: Path, run_root: Path) -> ArtifactReference:
        content = path.read_bytes()
        return ArtifactReference(
            path=path.relative_to(run_root).as_posix(),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def _optional_reference(self, path: Path, run_root: Path) -> ArtifactReference | None:
        return self._reference(path, run_root) if path.is_file() else None

    @staticmethod
    def _read_events(path: Path) -> tuple[RunEvent, ...]:
        if not path.is_file():
            return ()
        return tuple(
            RunEvent.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        )
