import sys
from pathlib import Path

import pytest
from langgraph.types import Command

from patch_code_agent.candidate import CandidatePatchBuilder, CandidatePatchReference
from patch_code_agent.diagnosis import Diagnostician
from patch_code_agent.graph import build_graph
from patch_code_agent.model import ScriptedModel
from patch_code_agent.patching import PatchApplier
from patch_code_agent.planning import Planner
from patch_code_agent.verification import (
    BaselineVerifier,
    RepairVerificationSummary,
    RepairVerifier,
)


class CountingModel:
    model_id = "counting"

    def __init__(self) -> None:
        self.plan_requests = 0
        self.candidate_requests = 0
        self.diagnosis_requests = 0

    def create_plan(self, request, tools):
        self.plan_requests += 1
        return ScriptedModel().create_plan(request, tools)

    def create_candidate(self, request, tools):
        self.candidate_requests += 1
        return ScriptedModel().create_candidate(request, tools)

    def create_diagnosis(self, request, tools):
        self.diagnosis_requests += 1
        return ScriptedModel().create_diagnosis(request, tools)


class CrashingDiagnosisModel(CountingModel):
    def create_diagnosis(self, request, tools):
        self.diagnosis_requests += 1
        raise RuntimeError("simulated provider interruption")


def test_graph_pauses_after_persisting_a_candidate_patch(tmp_path):
    source = tmp_path / "cart.py"
    source.write_text("def total(items):\n    return sum(items)\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)

    result = build_graph(
        baseline_verifier=BaselineVerifier(data_root),
        planner=Planner(data_root, ScriptedModel()),
        candidate_builder=CandidatePatchBuilder(data_root, ScriptedModel()),
        diagnostician=Diagnostician(data_root, ScriptedModel()),
        patch_applier=PatchApplier(data_root),
        repair_verifier=RepairVerifier(data_root),
    ).invoke(
        {
            "run_id": "test-run",
            "issue": "Fix the cart total",
            "verification_argv": [sys.executable, "-c", "raise SystemExit(1)"],
            "editable_paths": ["cart.py"],
            "model_requests": 0,
            "workspace_path": str(tmp_path),
            "status": "created",
        },
        config={"configurable": {"thread_id": "test-run"}},
    )

    assert result["status"] == "pending_approval"
    assert result["files_read"] == ["cart.py"]
    assert result["plan_artifact"]["path"] == "plan.json"
    assert result["candidate_artifact"]["path"] == "attempts/1/candidate.json"
    assert result["__interrupt__"]


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
        candidate_builder=CandidatePatchBuilder(data_root, model),
        diagnostician=Diagnostician(data_root, model),
        patch_applier=PatchApplier(data_root),
        repair_verifier=RepairVerifier(data_root),
    )
    initial_state = {
        "run_id": "test-run",
        "issue": "Fix the discount",
        "verification_argv": [sys.executable, "-c", "raise SystemExit(1)"],
        "editable_paths": ["cart.py"],
        "model_requests": 0,
        "tool_executions": 0,
        "files_read": [],
        "workspace_path": str(workspace),
        "status": "created",
    }
    config = {"configurable": {"thread_id": "test-run"}}

    first = graph.invoke(initial_state, config=config)
    replayed = graph.invoke(initial_state, config=config)

    assert model.plan_requests == 1
    assert model.candidate_requests == 1
    assert first["plan_artifact"] == replayed["plan_artifact"]
    assert first["candidate_artifact"] == replayed["candidate_artifact"]
    assert first["model_requests"] == replayed["model_requests"] == 2


def test_diagnosis_replay_does_not_create_a_second_model_request(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "cart.py").write_text("VALUE = 1\n")
    data_root = tmp_path / "runs"
    attempt_root = data_root / "test-run" / "attempts" / "1"
    attempt_root.mkdir(parents=True)
    model = CountingModel()
    plan = Planner(data_root, model).create_once(
        run_id="test-run",
        workspace=workspace,
        issue="Fix the value",
        verification=["pytest"],
    )
    verification = RepairVerificationSummary(
        attempt=1,
        outcome="failed",
        exit_code=1,
        duration_seconds=0.1,
        timeout_seconds=60,
        output_excerpt="1 failed",
        output_truncated=False,
        artifact_path="attempts/1/verification.log",
    )
    diagnostician = Diagnostician(data_root, model)

    first = diagnostician.create_once(
        run_id="test-run",
        workspace=workspace,
        issue="Fix the value",
        plan_reference=plan.reference,
        verification=verification,
    )
    replayed = diagnostician.create_once(
        run_id="test-run",
        workspace=workspace,
        issue="Fix the value",
        plan_reference=plan.reference,
        verification=verification,
        expected_reference=first.reference,
    )

    assert replayed == first
    assert model.diagnosis_requests == 1


def test_incomplete_diagnosis_claim_refuses_a_second_model_request(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "cart.py").write_text("VALUE = 1\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run" / "attempts" / "1").mkdir(parents=True)
    model = CrashingDiagnosisModel()
    plan = Planner(data_root, model).create_once(
        run_id="test-run",
        workspace=workspace,
        issue="Fix the value",
        verification=["pytest"],
    )
    verification = RepairVerificationSummary(
        attempt=1,
        outcome="failed",
        exit_code=1,
        duration_seconds=0.1,
        timeout_seconds=60,
        output_excerpt="1 failed",
        output_truncated=False,
        artifact_path="attempts/1/verification.log",
    )
    diagnostician = Diagnostician(data_root, model)

    with pytest.raises(RuntimeError, match="simulated provider interruption"):
        diagnostician.create_once(
            run_id="test-run",
            workspace=workspace,
            issue="Fix the value",
            plan_reference=plan.reference,
            verification=verification,
        )
    with pytest.raises(RuntimeError, match="incomplete replay ledger"):
        diagnostician.create_once(
            run_id="test-run",
            workspace=workspace,
            issue="Fix the value",
            plan_reference=plan.reference,
            verification=verification,
        )

    assert model.diagnosis_requests == 1


def test_graph_applies_and_verifies_an_approved_candidate_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "cart.py").write_text("discount = 0.1\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)
    graph = build_graph(
        baseline_verifier=BaselineVerifier(data_root),
        planner=Planner(data_root, ScriptedModel()),
        candidate_builder=CandidatePatchBuilder(data_root, ScriptedModel()),
        diagnostician=Diagnostician(data_root, ScriptedModel()),
        patch_applier=PatchApplier(data_root),
        repair_verifier=RepairVerifier(data_root),
    )
    config = {"configurable": {"thread_id": "test-run"}}
    verification_program = """
from pathlib import Path

counter = Path("verification-count")
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
raise SystemExit(
    0 if "# Scripted Candidate Patch" in Path("cart.py").read_text() else 1
)
"""
    pending = graph.invoke(
        {
            "run_id": "test-run",
            "issue": "Fix the discount",
            "verification_argv": [sys.executable, "-c", verification_program],
            "editable_paths": ["cart.py"],
            "model_requests": 0,
            "tool_executions": 0,
            "files_read": [],
            "workspace_path": str(workspace),
            "status": "created",
        },
        config=config,
    )

    approved = graph.invoke(Command(resume="approve"), config=config)
    replayed = graph.invoke(Command(resume="approve"), config=config)

    assert pending["status"] == "pending_approval"
    assert approved["status"] == "succeeded"
    assert approved["attempt"] == 1
    assert replayed["status"] == "succeeded"
    assert replayed["attempt"] == 1
    assert (workspace / "cart.py").read_text().count("# Scripted Candidate Patch") == 1
    assert (workspace / "verification-count").read_text() == "2"


def test_graph_replay_revalidates_workspace_after_a_completed_apply_ledger(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "cart.py"
    source.write_text("discount = 0.1\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)
    applier = PatchApplier(data_root)
    verification_program = """
from pathlib import Path

counter = Path("verification-count")
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
raise SystemExit(1)
"""
    graph = build_graph(
        baseline_verifier=BaselineVerifier(data_root),
        planner=Planner(data_root, ScriptedModel()),
        candidate_builder=CandidatePatchBuilder(data_root, ScriptedModel()),
        diagnostician=Diagnostician(data_root, ScriptedModel()),
        patch_applier=applier,
        repair_verifier=RepairVerifier(data_root),
    )
    config = {"configurable": {"thread_id": "test-run"}}
    pending = graph.invoke(
        {
            "run_id": "test-run",
            "issue": "Fix the discount",
            "verification_argv": [sys.executable, "-c", verification_program],
            "editable_paths": ["cart.py"],
            "model_requests": 0,
            "tool_executions": 0,
            "files_read": [],
            "workspace_path": str(workspace),
            "status": "created",
        },
        config=config,
    )
    reference = CandidatePatchReference.model_validate(pending["candidate_artifact"])
    applied = applier.apply_once(
        run_id="test-run",
        workspace=workspace,
        reference=reference,
    )
    source.write_text(source.read_text() + "# external edit\n")

    replayed = graph.invoke(Command(resume="approve"), config=config)

    assert applied.outcome == "applied"
    assert replayed["status"] == "workspace_changed"
    assert replayed["attempt"] == 0
    assert (workspace / "verification-count").read_text() == "1"
    assert not (data_root / "test-run" / "attempts" / "1" / "verification.json").exists()


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


def test_candidate_replay_rejects_a_replaced_exact_diff(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "cart.py").write_text("discount = 0.1\n")
    data_root = tmp_path / "runs"
    (data_root / "test-run").mkdir(parents=True)
    model = ScriptedModel()
    plan = Planner(data_root, model).create_once(
        run_id="test-run",
        workspace=workspace,
        issue="Fix the discount",
        verification=["pytest"],
    )
    builder = CandidatePatchBuilder(data_root, model)
    candidate = builder.create_once(
        run_id="test-run",
        workspace=workspace,
        issue="Fix the discount",
        editable_paths=["cart.py"],
        plan_reference=plan.reference,
        attempt=1,
    )
    diff_path = data_root / "test-run" / candidate.reference.diff_path
    diff_path.write_text(diff_path.read_text().replace("discount", "tampered", 1))

    with pytest.raises(RuntimeError, match="completion checksums"):
        builder.create_once(
            run_id="test-run",
            workspace=workspace,
            issue="Fix the discount",
            editable_paths=["cart.py"],
            plan_reference=plan.reference,
            attempt=1,
            expected_reference=candidate.reference,
        )
