"""Compose source, workspace, graph, Verification, and checkpoint dependencies.

The CLI delegates use cases to this module instead of constructing infrastructure itself. A
``PatchCodeAgent`` loads a Fixture Repository, creates an isolated workspace, invokes LangGraph
under the Run Identifier, and owns the writable SQLite connection. ``PatchRunStatusReader`` is a
separate read-only path so a status command cannot accidentally advance the graph.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from patch_code_agent.candidate import (
    CandidatePatchArtifact,
    CandidatePatchBuilder,
    CandidatePatchReference,
    load_candidate_patch,
)
from patch_code_agent.diagnosis import (
    DiagnosisArtifact,
    DiagnosisArtifactReference,
    Diagnostician,
    load_diagnosis_artifact,
)
from patch_code_agent.fixtures import (
    FixtureRegistry,
    FixtureRepository,
    bundled_fixture_roots,
    load_fixture_registry,
)
from patch_code_agent.graph import build_graph
from patch_code_agent.model import ModelGateway, Plan
from patch_code_agent.patching import CumulativeDiffReference, PatchApplier
from patch_code_agent.planning import (
    PlanArtifactReference,
    Planner,
    load_plan_artifact,
)
from patch_code_agent.reporting import RunAuditStore, RunReportReference
from patch_code_agent.sources import RepositorySource, RepositorySourceKind
from patch_code_agent.state import RunState
from patch_code_agent.verification import (
    BaselineVerifier,
    RepairVerificationSummary,
    RepairVerifier,
)
from patch_code_agent.workspace import RunWorkspaceStore


@dataclass(frozen=True, slots=True)
class PatchRunStatus:
    """Read-only public status for one persisted Patch Run.

    Attributes:
        run_id: Public Run Identifier used to query and eventually resume this workflow.
        source_kind: The Fixture Repository source kind.
        source_id: Stable Repository Source identifier shown to the user.
        source_revision: Digest of the immutable initial source snapshot.
        model_id: Stable identifier of the Model Gateway used by this Run.
        phase: Latest persisted graph status without advancing graph execution.
        model_requests: Durable number of model calls consumed by this Run.
        tool_executions: Durable number of bounded inspection operations.
        files_read: Stable paths successfully read by the model.
        plan: Validated Plan loaded from its checksummed artifact, when present.
        plan_artifact: Durable path/checksum reference, when planning completed.
        candidate: Validated pending Candidate Patch artifact, when present.
        candidate_diff: Exact host-computed unified diff awaiting Approval.
        candidate_artifact: Durable paths/checksums for Candidate JSON and diff.
        diagnosis: Latest validated failed-attempt Diagnosis artifact, when present.
        diagnosis_artifact: Durable path/checksum reference for that Diagnosis.
        attempts: Approved and verified Repair Attempts consumed so far.
        files_changed: Stable paths changed by approved Candidate Patches.
        verification: Latest post-apply Verification summary, when present.
        cumulative_diff: Durable checksum reference for the aggregate repair.
        error_kind: Stable terminal error category, when present.
        reason: Human-readable terminal explanation, when present.
        report_artifact: Immutable terminal Run Report reference, when finalized.

    Example:
        >>> status = PatchRunStatus(
        ...     run_id="123e4567-e89b-12d3-a456-426614174000",
        ...     source_kind="fixture",
        ...     source_id="cart-discount",
        ...     source_revision="9f86d081884c7d659a2feaa0c55ad015",
        ...     model_id="scripted",
        ...     phase="planned",
        ...     model_requests=0,
        ...     tool_executions=0,
        ...     files_read=(),
        ...     plan=None,
        ...     plan_artifact=None,
        ...     candidate=None,
        ...     candidate_diff=None,
        ...     candidate_artifact=None,
        ...     diagnosis=None,
        ...     diagnosis_artifact=None,
        ...     attempts=0,
        ...     files_changed=(),
        ...     verification=None,
        ...     cumulative_diff=None,
        ...     error_kind=None,
        ...     reason=None,
        ...     report_artifact=None,
        ... )
        >>> status.phase
        'planned'
    """

    run_id: str
    source_kind: RepositorySourceKind
    source_id: str
    source_revision: str
    model_id: str
    phase: str
    model_requests: int
    tool_executions: int
    files_read: tuple[str, ...]
    plan: Plan | None
    plan_artifact: PlanArtifactReference | None
    candidate: CandidatePatchArtifact | None
    candidate_diff: str | None
    candidate_artifact: CandidatePatchReference | None
    diagnosis: DiagnosisArtifact | None
    diagnosis_artifact: DiagnosisArtifactReference | None
    attempts: int
    files_changed: tuple[str, ...]
    verification: RepairVerificationSummary | None
    cumulative_diff: CumulativeDiffReference | None
    error_kind: str | None
    reason: str | None
    report_artifact: RunReportReference | None


class PatchRunStatusReader:
    """Read the latest durable Patch Run state without opening a writer.

    LangGraph stores multiple checkpoints per thread. The reader selects the newest checkpoint for
    the requested Run Identifier and exposes only the small status view needed by the CLI.
    """

    def __init__(self, data_root: Path) -> None:
        """Point the read model at the shared checkpoint database."""
        self._database_path = data_root.resolve() / "checkpoints.sqlite"
        self._serializer = JsonPlusSerializer(allowed_msgpack_modules=None)

    def get(self, run_id: str) -> PatchRunStatus:
        """Read the latest checkpoint for a Run Identifier without advancing it.

        SQLite is opened with ``mode=ro`` so this query cannot create a missing database or mutate
        an existing Run. Storage and decoding errors are translated into user-facing domain errors.
        """
        if not self._database_path.is_file():
            raise ValueError(f"Unknown Run Identifier: {run_id}")

        database_uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(database_uri, uri=True) as connection:
                row = connection.execute(
                    """
                    SELECT type, checkpoint
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ''
                    ORDER BY checkpoint_id DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise ValueError(f"Unable to read Patch Run status: {error}") from error

        if row is None:
            raise ValueError(f"Unknown Run Identifier: {run_id}")
        checkpoint = cast(dict[str, object], self._serializer.loads_typed(row))
        state = cast(RunState, checkpoint["channel_values"])
        plan_reference = (
            PlanArtifactReference.model_validate(state["plan_artifact"])
            if "plan_artifact" in state
            else None
        )
        plan = (
            load_plan_artifact(self._database_path.parent, run_id, plan_reference).plan
            if plan_reference is not None
            else None
        )
        candidate_reference = (
            CandidatePatchReference.model_validate(state["candidate_artifact"])
            if "candidate_artifact" in state
            else None
        )
        candidate_result = (
            load_candidate_patch(self._database_path.parent, run_id, candidate_reference)
            if candidate_reference is not None
            else None
        )
        diagnosis_reference = (
            DiagnosisArtifactReference.model_validate(state["diagnosis_artifact"])
            if "diagnosis_artifact" in state
            else None
        )
        diagnosis_result = (
            load_diagnosis_artifact(self._database_path.parent, run_id, diagnosis_reference)
            if diagnosis_reference is not None
            else None
        )
        return PatchRunStatus(
            run_id=state["run_id"],
            source_kind=state["source_kind"],
            source_id=state["source_id"],
            source_revision=state["source_revision"],
            model_id=state["model_id"],
            phase=state["status"],
            model_requests=state["model_requests"],
            tool_executions=state.get("tool_executions", 0),
            files_read=tuple(state.get("files_read", [])),
            plan=plan,
            plan_artifact=plan_reference,
            candidate=(candidate_result.artifact if candidate_result is not None else None),
            candidate_diff=(candidate_result.diff if candidate_result is not None else None),
            candidate_artifact=candidate_reference,
            diagnosis=(diagnosis_result.artifact if diagnosis_result is not None else None),
            diagnosis_artifact=diagnosis_reference,
            attempts=state.get("attempt", 0),
            files_changed=tuple(state.get("files_changed", [])),
            verification=(
                RepairVerificationSummary.model_validate(state["verification"])
                if "verification" in state
                else None
            ),
            cumulative_diff=(
                CumulativeDiffReference.model_validate(state["cumulative_diff"])
                if "cumulative_diff" in state
                else None
            ),
            error_kind=state.get("error_kind"),
            reason=(
                str(state["report"]["note"])
                if state.get("report", {}).get("note") is not None
                else None
            ),
            report_artifact=(
                RunReportReference.model_validate(state["report_artifact"])
                if "report_artifact" in state
                else None
            ),
        )


class PatchCodeAgent:
    """Application service coordinating one process's Patch Run operations.

    Expensive stateful dependencies are lazy: listing fixtures does not open SQLite, and the graph
    plus connection are created only when a Run starts. Fixture loading ends at the
    ``RepositorySource`` seam before the workflow begins.
    """

    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        data_root: Path,
        fixture_roots: tuple[Path, ...] | None = None,
        verification_timeout_seconds: float = 60.0,
    ) -> None:
        """Configure lazy application dependencies around one durable data root."""
        self._model_gateway = model_gateway
        self._model_id = model_gateway.model_id
        self._data_root = data_root.resolve()
        self._fixture_roots = (
            fixture_roots if fixture_roots is not None else bundled_fixture_roots()
        )
        self._fixtures: FixtureRegistry | None = None
        self._workspaces = RunWorkspaceStore(self._data_root)
        self._baseline_verifier = BaselineVerifier(
            self._data_root,
            timeout_seconds=verification_timeout_seconds,
        )
        self._planner = Planner(self._data_root, model_gateway)
        self._candidate_builder = CandidatePatchBuilder(self._data_root, model_gateway)
        self._diagnostician = Diagnostician(self._data_root, model_gateway)
        self._patch_applier = PatchApplier(self._data_root)
        self._repair_verifier = RepairVerifier(
            self._data_root,
            timeout_seconds=verification_timeout_seconds,
        )
        self._audit_store = RunAuditStore(self._data_root)
        self._checkpoint_connection: sqlite3.Connection | None = None
        self._graph: CompiledStateGraph | None = None

    def list_fixture_repositories(self) -> tuple[FixtureRepository, ...]:
        """Return the registered Fixture Repositories in stable identifier order."""
        return self._fixture_registry().list()

    def start_patch_run(
        self,
        *,
        fixture_id: str,
        run_id: str,
    ) -> RunState:
        """Run the scaffold workflow through its public application interface."""
        source = self._fixture_registry().get(fixture_id).as_repository_source()
        return self._start_patch_run(source=source, run_id=run_id)

    def reject_patch_run(self, *, run_id: str) -> RunState:
        """Resume one pending Approval Gate with a durable rejection decision."""
        status = PatchRunStatusReader(self._data_root).get(run_id)
        if status.phase != "pending_approval":
            raise ValueError(
                f"Patch Run is not awaiting Approval (current phase: {status.phase})"
            )
        result = self._run_graph().invoke(
            Command(resume="reject"),
            config={"configurable": {"thread_id": run_id}},
        )
        return cast(RunState, result)

    def approve_patch_run(
        self,
        *,
        run_id: str,
        confirm: Callable[[PatchRunStatus], bool],
    ) -> RunState | None:
        """Display and confirm one exact Candidate before resuming its graph."""
        status = PatchRunStatusReader(self._data_root).get(run_id)
        if status.phase != "pending_approval":
            raise ValueError(
                f"Patch Run is not awaiting Approval (current phase: {status.phase})"
            )
        if status.model_id != self._model_id:
            raise ValueError(
                f"Patch Run requires model {status.model_id}, got {self._model_id}"
            )
        if not confirm(status):
            return None
        result = self._run_graph().invoke(
            Command(resume="approve"),
            config={"configurable": {"thread_id": run_id}},
        )
        return cast(RunState, result)

    def _start_patch_run(self, *, source: RepositorySource, run_id: str) -> RunState:
        """Snapshot a normalized source and invoke its graph under one thread ID.

        The initial state contains only validated contract data and stable source identity. The
        model-request counter begins at zero so baseline ordering remains observable and durable.
        LangGraph's thread ID equals the public Run Identifier used by later status/resume commands.
        """
        if bool(getattr(self._model_gateway, "synthetic_only", False)):
            allowed_roots = tuple(
                Path(root).resolve()
                for root in getattr(self._model_gateway, "allowed_fixture_roots", ())
            )
            if source.root.resolve() not in allowed_roots:
                raise ValueError(
                    "This Model Gateway only accepts bundled synthetic Fixture Repositories"
                )
        workspace = self._workspaces.create(run_id, source.root)
        graph = self._run_graph()
        result = graph.invoke(
            {
                "run_id": run_id,
                "source_kind": source.kind,
                "source_id": source.source_id,
                "source_revision": workspace.source_revision,
                "model_id": self._model_id,
                "issue": source.contract.issue,
                "verification_argv": list(source.contract.verification),
                "editable_paths": list(source.contract.editable_paths),
                "protected_paths": list(source.contract.protected_paths),
                "model_requests": 0,
                "tool_executions": 0,
                "files_read": [],
                "files_changed": [],
                "workspace_path": str(workspace.path),
                "status": "created",
            },
            config={"configurable": {"thread_id": run_id}},
        )
        return cast(RunState, result)

    def close(self) -> None:
        """Flush and close the writable checkpoint connection, if one was opened."""
        if self._checkpoint_connection is None:
            return
        self._checkpoint_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._checkpoint_connection.close()
        self._checkpoint_connection = None
        self._graph = None

    def _fixture_registry(self) -> FixtureRegistry:
        """Load and cache the fixture registry only when a fixture command needs it."""
        if self._fixtures is None:
            self._fixtures = load_fixture_registry(self._fixture_roots)
        return self._fixtures

    def _run_graph(self) -> CompiledStateGraph:
        """Build and cache the graph with one writable SQLite connection.

        ``check_same_thread=False`` is required by LangGraph's execution model; ownership still
        remains inside this application instance, and ``close`` checkpoints the WAL before release.
        """
        if self._graph is None:
            self._data_root.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self._data_root / "checkpoints.sqlite",
                check_same_thread=False,
            )
            self._checkpoint_connection = connection
            checkpointer = SqliteSaver(
                connection,
                serde=JsonPlusSerializer(allowed_msgpack_modules=None),
            )
            self._graph = build_graph(
                baseline_verifier=self._baseline_verifier,
                planner=self._planner,
                candidate_builder=self._candidate_builder,
                diagnostician=self._diagnostician,
                patch_applier=self._patch_applier,
                repair_verifier=self._repair_verifier,
                audit_store=self._audit_store,
                checkpointer=checkpointer,
            )
        return self._graph
