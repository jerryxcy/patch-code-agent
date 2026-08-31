import sys
from pathlib import Path

import pytest

from patch_code_agent.graph import build_graph
from patch_code_agent.model import ScriptedModel
from patch_code_agent.planning import Planner
from patch_code_agent.verification import BaselineVerifier


class CountingModel:
    model_id = "counting"

    def __init__(self) -> None:
        self.requests = 0

    def create_plan(self, request, tools):
        self.requests += 1
        return ScriptedModel().create_plan(request, tools)


def test_graph_builds_a_plan(tmp_path):
    source = tmp_path / "cart.py"
    source.write_text("def total(items):\n    return sum(items)\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)

    result = build_graph(
        baseline_verifier=BaselineVerifier(data_root),
        planner=Planner(data_root, ScriptedModel()),
    ).invoke(
        {
            "run_id": "test-run",
            "issue": "Fix the cart total",
            "verification_argv": [sys.executable, "-c", "raise SystemExit(1)"],
            "model_requests": 0,
            "workspace_path": str(tmp_path),
            "status": "created",
        },
        config={"configurable": {"thread_id": "test-run"}},
    )

    assert result["status"] == "planned"
    assert result["files_read"] == ["cart.py"]
    assert result["plan_artifact"]["path"] == "plan.json"


def test_baseline_verification_is_replay_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)
    verifier = BaselineVerifier(data_root)
    program = """
from pathlib import Path

counter = Path("verification-count")
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
raise SystemExit(1)
"""

    first = verifier.verify(
        run_id="test-run",
        workspace=workspace,
        argv=[sys.executable, "-c", program],
    )
    replayed = verifier.verify(
        run_id="test-run",
        workspace=workspace,
        argv=[sys.executable, "-c", program],
    )

    assert first == replayed
    assert (workspace / "verification-count").read_text() == "1"


def test_graph_replay_does_not_create_a_second_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "cart.py").write_text("discount = 0.1\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)
    model = CountingModel()
    graph = build_graph(
        baseline_verifier=BaselineVerifier(data_root),
        planner=Planner(data_root, model),
    )
    initial_state = {
        "run_id": "test-run",
        "issue": "Fix the discount",
        "verification_argv": [sys.executable, "-c", "raise SystemExit(1)"],
        "model_requests": 0,
        "tool_executions": 0,
        "files_read": [],
        "workspace_path": str(workspace),
        "status": "created",
    }
    config = {"configurable": {"thread_id": "test-run"}}

    first = graph.invoke(initial_state, config=config)
    replayed = graph.invoke(initial_state, config=config)

    assert model.requests == 1
    assert first["plan_artifact"] == replayed["plan_artifact"]
    assert first["model_requests"] == replayed["model_requests"] == 1


def test_plan_replay_rejects_a_replaced_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "cart.py").write_text("discount = 0.1\n")
    data_root = tmp_path / "runs"
    run_root = data_root / "test-run"
    run_root.mkdir(parents=True)
    planner = Planner(data_root, ScriptedModel())
    first = planner.create_once(
        run_id="test-run",
        workspace=workspace,
        issue="Fix the discount",
        verification=["pytest"],
    )
    plan_path = run_root / "plan.json"
    plan_path.write_text(plan_path.read_text().replace("scripted", "replaced"))

    with pytest.raises(RuntimeError, match="completion checksum"):
        planner.create_once(
            run_id="test-run",
            workspace=workspace,
            issue="Fix the discount",
            verification=["pytest"],
            expected_reference=first.reference,
        )
