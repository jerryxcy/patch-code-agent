import sys
from pathlib import Path

from patch_code_agent.graph import build_graph
from patch_code_agent.verification import BaselineVerifier


def test_graph_builds_a_plan(tmp_path):
    source = tmp_path / "cart.py"
    source.write_text("def total(items):\n    return sum(items)\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)

    result = build_graph(baseline_verifier=BaselineVerifier(data_root)).invoke(
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
    assert result["inspected_files"] == ["cart.py"]
    assert len(result["plan"]) == 2


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
