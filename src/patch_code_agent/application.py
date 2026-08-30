from pathlib import Path
from typing import cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from patch_code_agent.graph import build_graph
from patch_code_agent.model import ModelGateway
from patch_code_agent.state import RunState


class PatchCodeAgent:
    """Application module that owns Patch Run orchestration dependencies."""

    def __init__(self, *, model_gateway: ModelGateway, data_root: Path) -> None:
        self._model_gateway = model_gateway
        self._data_root = data_root.resolve()
        self._graph: CompiledStateGraph = build_graph(checkpointer=InMemorySaver())

    def start_patch_run(
        self,
        *,
        issue: str,
        repo_path: Path,
        run_id: str,
    ) -> RunState:
        """Run the scaffold workflow through its public application interface."""
        result = self._graph.invoke(
            {
                "issue": issue,
                "repo_path": str(repo_path),
                "status": "created",
            },
            config={"configurable": {"thread_id": run_id}},
        )
        return cast(RunState, result)
