"""Compose source, workspace, graph, Verification, and checkpoint dependencies.

The CLI delegates use cases to this module instead of constructing infrastructure itself. A
``PatchCodeAgent`` normalizes either source kind, creates an isolated workspace, invokes LangGraph
under the Run Identifier, and owns the writable SQLite connection. ``PatchRunStatusReader`` is a
separate read-only path so a status command cannot accidentally advance the graph.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph

from patch_code_agent.fixtures import (
    FixtureRegistry,
    FixtureRepository,
    bundled_fixture_roots,
    load_fixture_registry,
)
from patch_code_agent.graph import build_graph
from patch_code_agent.model import ModelGateway
from patch_code_agent.sources import (
    RepositorySource,
    RepositorySourceKind,
    load_trusted_repository,
)
from patch_code_agent.state import RunState
from patch_code_agent.verification import BaselineVerifier
from patch_code_agent.workspace import RunWorkspaceStore


@dataclass(frozen=True, slots=True)
class PatchRunStatus:
    """Read-only public status for one persisted Patch Run.

    Attributes:
        run_id: Public Run Identifier used to query and eventually resume this workflow.
        source_kind: Whether the Run began from a fixture or trusted local repository.
        source_id: Stable Repository Source identifier shown to the user.
        source_revision: Digest of the immutable initial source snapshot.
        phase: Latest persisted graph status without advancing graph execution.
        model_requests: Durable number of model calls consumed by this Run.

    Example:
        >>> status = PatchRunStatus(
        ...     run_id="123e4567-e89b-12d3-a456-426614174000",
        ...     source_kind="fixture",
        ...     source_id="cart-discount",
        ...     source_revision="9f86d081884c7d659a2feaa0c55ad015",
        ...     phase="planned",
        ...     model_requests=0,
        ... )
        >>> status.phase
        'planned'
    """

    run_id: str
    source_kind: RepositorySourceKind
    source_id: str
    source_revision: str
    phase: str
    model_requests: int


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
        return PatchRunStatus(
            run_id=state["run_id"],
            source_kind=state["source_kind"],
            source_id=state["source_id"],
            source_revision=state["source_revision"],
            phase=state["status"],
            model_requests=state["model_requests"],
        )


class PatchCodeAgent:
    """Application service coordinating one process's Patch Run operations.

    Expensive stateful dependencies are lazy: listing fixtures does not open SQLite, and the graph
    plus connection are created only when a Run starts. Source-specific work ends at the
    ``RepositorySource`` seam; both fixture and trusted-local paths then share the same workflow.
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
        self._data_root = data_root.resolve()
        self._fixture_roots = fixture_roots if fixture_roots is not None else bundled_fixture_roots()
        self._fixtures: FixtureRegistry | None = None
        self._workspaces = RunWorkspaceStore(self._data_root)
        self._baseline_verifier = BaselineVerifier(
            self._data_root,
            timeout_seconds=verification_timeout_seconds,
        )
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

    def start_trusted_patch_run(
        self,
        *,
        repository: Path,
        contract_path: Path,
        run_id: str,
    ) -> RunState:
        """Start a Patch Run from an explicitly selected Trusted Repository."""
        source = load_trusted_repository(repository, contract_path)
        return self._start_patch_run(source=source, run_id=run_id)

    def _start_patch_run(self, *, source: RepositorySource, run_id: str) -> RunState:
        """Snapshot a normalized source and invoke its graph under one thread ID.

        The initial state contains only validated contract data and stable source identity. The
        model-request counter begins at zero so baseline ordering remains observable and durable.
        LangGraph's thread ID equals the public Run Identifier used by later status/resume commands.
        """
        workspace = self._workspaces.create(run_id, source.root)
        graph = self._run_graph()
        result = graph.invoke(
            {
                "run_id": run_id,
                "source_kind": source.kind,
                "source_id": source.source_id,
                "source_revision": workspace.source_revision,
                "issue": source.contract.issue,
                "verification_argv": list(source.contract.verification),
                "editable_paths": list(source.contract.editable_paths),
                "model_requests": 0,
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
                checkpointer=checkpointer,
            )
        return self._graph
