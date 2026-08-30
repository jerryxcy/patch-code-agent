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
from patch_code_agent.workspace import RunWorkspaceStore


@dataclass(frozen=True, slots=True)
class PatchRunStatus:
    """Read-only public status for one persisted Patch Run."""

    run_id: str
    source_kind: RepositorySourceKind
    source_id: str
    source_revision: str
    phase: str


class PatchRunStatusReader:
    """Reads bounded Patch Run status without mutating checkpoint storage."""

    def __init__(self, data_root: Path) -> None:
        self._database_path = data_root.resolve() / "checkpoints.sqlite"
        self._serializer = JsonPlusSerializer(allowed_msgpack_modules=None)

    def get(self, run_id: str) -> PatchRunStatus:
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
        )


class PatchCodeAgent:
    """Application module that owns Patch Run orchestration dependencies."""

    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        data_root: Path,
        fixture_roots: tuple[Path, ...] | None = None,
    ) -> None:
        self._model_gateway = model_gateway
        self._data_root = data_root.resolve()
        self._fixture_roots = fixture_roots if fixture_roots is not None else bundled_fixture_roots()
        self._fixtures: FixtureRegistry | None = None
        self._workspaces = RunWorkspaceStore(self._data_root)
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
        if self._fixtures is None:
            self._fixtures = load_fixture_registry(self._fixture_roots)
        return self._fixtures

    def _run_graph(self) -> CompiledStateGraph:
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
            self._graph = build_graph(checkpointer=checkpointer)
        return self._graph
