"""Print the compiled Patch Run topology as a Markdown Mermaid block."""

from pathlib import Path
from tempfile import TemporaryDirectory

from patch_code_agent.candidate import CandidatePatchBuilder
from patch_code_agent.diagnosis import Diagnostician
from patch_code_agent.graph import build_graph
from patch_code_agent.model import ScriptedModel
from patch_code_agent.patching import PatchApplier
from patch_code_agent.planning import Planner
from patch_code_agent.reporting import RunAuditStore
from patch_code_agent.verification import BaselineVerifier, RepairVerifier


def main() -> None:
    """Build without executing the graph and print LangGraph's Mermaid representation."""
    with TemporaryDirectory(prefix="patch-code-agent-graph-") as temporary_directory:
        data_root = Path(temporary_directory)
        model = ScriptedModel()
        graph = build_graph(
            baseline_verifier=BaselineVerifier(data_root),
            planner=Planner(data_root, model),
            candidate_builder=CandidatePatchBuilder(data_root, model),
            diagnostician=Diagnostician(data_root, model),
            patch_applier=PatchApplier(data_root),
            repair_verifier=RepairVerifier(data_root),
            audit_store=RunAuditStore(data_root),
        )
        print("```mermaid")
        print(graph.get_graph().draw_mermaid(), end="")
        print("```")


if __name__ == "__main__":
    main()
