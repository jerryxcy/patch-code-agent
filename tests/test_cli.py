import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from patch_code_agent.cli import create_cli
from patch_code_agent.inspection import WorkspaceInspector
from patch_code_agent.model import ScriptedInspectionCall, ScriptedModel


class RecordingModel:
    def __init__(self) -> None:
        self.model_id_accesses = 0
        self.plan_requests = 0
        self.candidate_requests = 0

    @property
    def model_id(self) -> str:
        self.model_id_accesses += 1
        return "recording"

    def create_plan(self, request, tools):
        self.plan_requests += 1
        return ScriptedModel().create_plan(request, tools)

    def create_candidate(self, request, tools):
        self.candidate_requests += 1
        return ScriptedModel().create_candidate(request, tools)


class SearchRecordingModel:
    model_id = "search-recording"

    def __init__(self) -> None:
        self.search_result = None

    def create_plan(self, request, tools):
        self.search_result = tools.search_code("needle")
        return {
            "issue_summary": "Inspect bounded search output",
            "relevant_files": ["large.txt"],
            "repair_strategy": "Use the matching lines to locate the defect.",
            "verification_strategy": "Run the declared Verification command.",
        }

    def create_candidate(self, request, tools):
        return ScriptedModel().create_candidate(request, tools)


class InvalidPlanModel:
    model_id = "invalid-plan"

    def create_plan(self, request, tools):
        tools.list_files()
        return {"issue_summary": "Missing required Plan fields"}


class FailingPlanModel:
    model_id = "failing-plan"

    def create_plan(self, request, tools):
        tools.list_files()
        raise RuntimeError("simulated provider outage")

    def create_candidate(self, request, tools):
        raise AssertionError("Candidate generation must not run after model failure")

    def create_diagnosis(self, request, tools):
        raise AssertionError("Diagnosis must not run after model failure")


class ToolBudgetModel:
    model_id = "tool-budget"

    def create_plan(self, request, tools):
        for _ in range(21):
            tools.list_files()
        return {
            "issue_summary": "Exhaust tool operations",
            "relevant_files": ["cart.py"],
            "repair_strategy": "No repair should be generated.",
            "verification_strategy": "Run the declared Verification.",
        }

    def create_candidate(self, request, tools):
        return ScriptedModel().create_candidate(request, tools)

    def create_diagnosis(self, request, tools):
        return ScriptedModel().create_diagnosis(request, tools)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowPlanModel:
    model_id = "slow-plan"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def create_plan(self, request, tools):
        result = ScriptedModel().create_plan(request, tools)
        self.clock.advance(301.0)
        return result

    def create_candidate(self, request, tools):
        return ScriptedModel().create_candidate(request, tools)

    def create_diagnosis(self, request, tools):
        return ScriptedModel().create_diagnosis(request, tools)


class FilesReadBudgetModel:
    model_id = "files-read-budget"

    def create_plan(self, request, tools):
        paths = tools.list_files().paths
        for path in paths[:13]:
            tools.read_file(path)
        return {
            "issue_summary": "Read too many distinct files",
            "relevant_files": ["cart.py"],
            "repair_strategy": "No repair should be generated.",
            "verification_strategy": "Run the declared Verification.",
        }

    def create_candidate(self, request, tools):
        return ScriptedModel().create_candidate(request, tools)

    def create_diagnosis(self, request, tools):
        return ScriptedModel().create_diagnosis(request, tools)


class CumulativeFilesChangedBudgetModel:
    model_id = "cumulative-files-changed-budget"

    def create_plan(self, request, tools):
        return {
            "issue_summary": "Change too many files across Repair Attempts",
            "relevant_files": ["file0.py"],
            "repair_strategy": "Apply bounded groups of complete replacements.",
            "verification_strategy": "Run the declared Verification.",
        }

    def create_candidate(self, request, tools):
        paths = request.editable_paths[:3] if request.attempt == 1 else request.editable_paths[3:4]
        replacements = []
        for path in paths:
            observed = tools.read_file(path)
            replacements.append(
                {
                    "path": path,
                    "expected_sha256": hashlib.sha256(observed.content.encode()).hexdigest(),
                    "new_content": observed.content + f"# attempt {request.attempt}\n",
                }
            )
        return {"replacements": replacements}

    def create_diagnosis(self, request, tools):
        return {
            "failure_summary": "The prior candidate did not satisfy Verification.",
            "evidence": request.verification_output_excerpt or "exit code 1",
            "next_strategy": "Try the remaining editable file.",
        }


class CorrectedEveryOutputModel:
    model_id = "corrected-every-output"

    def __init__(self) -> None:
        self.delegate = ScriptedModel(repair_failures=3)
        self.calls = {"plan": 0, "candidate": 0, "diagnosis": 0}

    def _needs_correction(self, phase: str) -> bool:
        self.calls[phase] += 1
        return self.calls[phase] % 2 == 1

    def create_plan(self, request, tools):
        if self._needs_correction("plan"):
            assert not request.validation_errors
            return {}
        assert request.validation_errors
        return self.delegate.create_plan(request, tools)

    def create_candidate(self, request, tools):
        if self._needs_correction("candidate"):
            assert not request.validation_errors
            return {}
        assert request.validation_errors
        return self.delegate.create_candidate(request, tools)

    def create_diagnosis(self, request, tools):
        if self._needs_correction("diagnosis"):
            assert not request.validation_errors
            return {}
        assert request.validation_errors
        return self.delegate.create_diagnosis(request, tools)


class CandidateScenarioModel:
    model_id = "candidate-scenario"

    def __init__(self, scenario: str, path: str = "cart.py") -> None:
        self.scenario = scenario
        self.path = path

    def create_plan(self, request, tools):
        return ScriptedModel().create_plan(request, tools)

    def create_candidate(self, request, tools):
        if self.scenario in {"create", "unread"}:
            expected_sha256 = "0" * 64
            old_content = ""
        else:
            observed = tools.read_file(self.path)
            old_content = observed.content
            expected_sha256 = hashlib.sha256(old_content.encode()).hexdigest()

        replacement = {
            "path": self.path,
            "expected_sha256": expected_sha256,
            "new_content": old_content + "# proposed change\n",
        }
        if self.scenario == "delete":
            replacement["new_content"] = ""
        elif self.scenario == "binary":
            replacement["new_content"] = old_content + "\x00binary"
        elif self.scenario == "stale_hash":
            replacement["expected_sha256"] = "f" * 64
        elif self.scenario == "oversized":
            replacement["new_content"] = "x" * (100 * 1024 + 1)
        elif self.scenario == "rename":
            replacement["new_path"] = "renamed.py"
        elif self.scenario == "too_many":
            return {"replacements": [replacement] * 4}
        return {"replacements": [replacement]}


class TwoFileCandidateModel:
    model_id = "two-file-candidate"

    def create_plan(self, request, tools):
        return ScriptedModel().create_plan(request, tools)

    def create_candidate(self, request, tools):
        replacements = []
        for path in request.editable_paths:
            observed = tools.read_file(path)
            replacements.append(
                {
                    "path": path,
                    "expected_sha256": hashlib.sha256(observed.content.encode()).hexdigest(),
                    "new_content": observed.content + "# approved change\n",
                }
            )
        return {"replacements": replacements}


def _run_identifier(output: str) -> str:
    match = re.search(r"Run Identifier: ([0-9a-f-]{36})", output)
    assert match is not None
    return match.group(1)


def _invoke_cli_process(
    tmp_path: Path,
    *arguments: str,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "process-home")
    return subprocess.run(
        [sys.executable, "-m", "patch_code_agent", *arguments],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        input=input_text,
        check=False,
        timeout=30,
    )


def _run_trusted_inspection(
    tmp_path: Path,
    model,
    *,
    extra_files: dict[str, bytes] | None = None,
    baseline_program: str = "raise SystemExit(1)",
):
    repository = tmp_path / "inspection-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    for relative, content in (extra_files or {}).items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    contract = tmp_path / "inspection-contract.toml"
    contract.write_text(
        f"""source_id = "inspection-repository"
issue = "Inspect the reported problem"
verification = {json.dumps([sys.executable, "-c", baseline_program])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=model, data_root=data_root)
    result = CliRunner().invoke(
        cli,
        ["run-local", str(repository), "--contract", str(contract), "--trust-repository"],
    )
    return result, data_root


def test_user_lists_registered_fixture_repositories(tmp_path: Path) -> None:
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=tmp_path / "runs",
    )

    result = CliRunner().invoke(cli, ["fixtures"])

    assert result.exit_code == 0, result.output
    assert "cart-discount" in result.output
    assert "Incorrect discount calculation" in result.output


def test_user_gets_clear_error_for_invalid_fixture_manifest(tmp_path: Path) -> None:
    invalid_fixture = tmp_path / "invalid-fixture"
    invalid_fixture.mkdir()
    (invalid_fixture / "fixture.toml").write_text(
        """fixture_id = "invalid-fixture"
issue_path = "missing-issue.md"
verification = ["pytest"]
editable_paths = ["missing.py"]
""",
        encoding="utf-8",
    )
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=tmp_path / "runs",
        fixture_roots=(invalid_fixture,),
    )

    result = CliRunner().invoke(cli, ["fixtures"])

    assert result.exit_code != 0
    assert "Repository Source Issue does not exist: missing-issue.md" in result.output


def test_user_gets_clear_error_for_malformed_fixture_manifest(tmp_path: Path) -> None:
    invalid_fixture = tmp_path / "invalid-fixture"
    invalid_fixture.mkdir()
    (invalid_fixture / "fixture.toml").write_text(
        'fixture_id = ["not", "valid"',
        encoding="utf-8",
    )
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=tmp_path / "runs",
        fixture_roots=(invalid_fixture,),
    )

    result = CliRunner().invoke(cli, ["fixtures"])

    assert result.exit_code != 0
    assert "Invalid Fixture Manifest" in result.output


def test_user_gets_clear_error_for_empty_fixture_issue(tmp_path: Path) -> None:
    invalid_fixture = tmp_path / "invalid-fixture"
    invalid_fixture.mkdir()
    (invalid_fixture / "fixture.toml").write_text(
        """fixture_id = "empty-issue"
issue_path = "issue.md"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    (invalid_fixture / "issue.md").write_text("\n", encoding="utf-8")
    (invalid_fixture / "cart.py").write_text("", encoding="utf-8")
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=tmp_path / "runs",
        fixture_roots=(invalid_fixture,),
    )

    result = CliRunner().invoke(cli, ["fixtures"])

    assert result.exit_code != 0
    assert "Fixture Issue must not be empty" in result.output


def test_user_gets_clear_error_for_fixture_path_traversal(tmp_path: Path) -> None:
    invalid_fixture = tmp_path / "invalid-fixture"
    invalid_fixture.mkdir()
    (tmp_path / "outside.md").write_text("# Outside", encoding="utf-8")
    (invalid_fixture / "fixture.toml").write_text(
        """fixture_id = "path-traversal"
issue_path = "../outside.md"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    (invalid_fixture / "cart.py").write_text("", encoding="utf-8")
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=tmp_path / "runs",
        fixture_roots=(invalid_fixture,),
    )

    result = CliRunner().invoke(cli, ["fixtures"])

    assert result.exit_code != 0
    assert "without traversal" in result.output


def test_user_gets_clear_error_for_symlink_in_fixture_tree(tmp_path: Path) -> None:
    invalid_fixture = tmp_path / "invalid-fixture"
    invalid_fixture.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("SECRET = True\n", encoding="utf-8")
    (invalid_fixture / "fixture.toml").write_text(
        """fixture_id = "symlink-fixture"
issue_path = "issue.md"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    (invalid_fixture / "issue.md").write_text("# Symlink fixture\n", encoding="utf-8")
    (invalid_fixture / "cart.py").symlink_to(outside_file)
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=tmp_path / "runs",
        fixture_roots=(invalid_fixture,),
    )

    result = CliRunner().invoke(cli, ["fixtures"])

    assert result.exit_code != 0
    assert "symbolic link" in result.output


def test_user_starts_registered_patch_run_in_isolated_workspace(tmp_path: Path) -> None:
    fixture_repository = Path(__file__).parents[1] / "examples" / "tiny_repo"
    original_cart = (fixture_repository / "cart.py").read_bytes()
    data_root = tmp_path / "runs"
    model = RecordingModel()
    cli = create_cli(
        model_gateway=model,
        data_root=data_root,
    )

    result = CliRunner().invoke(cli, ["run", "cart-discount"])

    assert result.exit_code == 0, result.output
    run_identifier = _run_identifier(result.output)
    workspace = data_root / run_identifier / "workspace"

    assert (data_root / "checkpoints.sqlite").is_file()
    assert (workspace / "cart.py").read_bytes() == original_cart
    assert (workspace / "fixture.toml").is_file()
    assert not (workspace / "__pycache__").exists()
    assert (fixture_repository / "cart.py").read_bytes() == original_cart
    assert "PatchCodeAgent plan" in result.output
    assert "Baseline Verification: failed" in result.output
    assert "Model Requests: 2" in result.output
    assert model.plan_requests == 1
    assert model.candidate_requests == 1
    assert model.model_id_accesses == 2
    assert "Run Identifier:" in result.output
    assert "status: pending_approval" in result.output
    baseline = json.loads((data_root / run_identifier / "baseline" / "result.json").read_text())
    assert baseline["outcome"] == "failed"
    assert baseline["exit_code"] == 1
    output_log = (data_root / run_identifier / "baseline" / "output.log").read_text()
    assert "test_discounted_total" in output_log


def test_failing_baseline_creates_a_typed_checksummed_plan_artifact(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(cli, ["run", "cart-discount"])

    assert result.exit_code == 0, result.output
    run_identifier = _run_identifier(result.output)
    plan_path = data_root / run_identifier / "plan.json"
    plan_bytes = plan_path.read_bytes()
    plan_artifact = json.loads(plan_bytes)
    checksum = hashlib.sha256(plan_bytes).hexdigest()
    assert plan_artifact == {
        "files_read": ["cart.py", "test_cart.py"],
        "model_id": "scripted",
        "model_requests": 1,
        "plan": {
            "issue_summary": "Incorrect discount calculation",
            "relevant_files": ["cart.py", "test_cart.py"],
            "repair_strategy": "Apply the smallest change that addresses the reported Issue.",
            "verification_strategy": "Run: pytest test_cart.py",
        },
        "schema_version": "1",
        "tool_executions": 4,
    }
    assert f"Plan Checksum: {checksum}" in result.output
    assert "Model Requests: 2" in result.output
    assert "Tool Executions: 5" in result.output

    status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    status = CliRunner().invoke(status_cli, ["status", run_identifier])
    assert status.exit_code == 0, status.output
    assert "Plan Artifact: plan.json" in status.output
    assert f"Plan Checksum: {checksum}" in status.output
    assert "Issue: Incorrect discount calculation" in status.output
    assert "Relevant Files: cart.py, test_cart.py" in status.output
    assert "Repair: Apply the smallest change that addresses the reported Issue." in status.output
    assert "Verification: Run: pytest test_cart.py" in status.output


def test_run_pauses_with_an_immutable_candidate_patch_visible_from_status(
    tmp_path: Path,
) -> None:
    fixture_repository = Path(__file__).parents[1] / "examples" / "tiny_repo"
    original_source = (fixture_repository / "cart.py").read_bytes()
    data_root = tmp_path / "runs"

    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )

    assert run_result.exit_code == 0, run_result.output
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    workspace_source = run_root / "workspace" / "cart.py"
    candidate_path = run_root / "attempts" / "1" / "candidate.json"
    diff_path = run_root / "attempts" / "1" / "candidate.diff"
    candidate_checksum = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    diff_checksum = hashlib.sha256(diff_path.read_bytes()).hexdigest()
    exact_diff = diff_path.read_text(encoding="utf-8")

    assert workspace_source.read_bytes() == original_source
    assert (fixture_repository / "cart.py").read_bytes() == original_source
    assert "status: pending_approval" in run_result.output
    assert "Candidate Patch" in run_result.output
    assert f"Candidate Checksum: {candidate_checksum}" in run_result.output
    assert f"Diff Checksum: {diff_checksum}" in run_result.output
    assert "-    return sum(prices) - discount" in exact_diff
    assert "+    subtotal = sum(prices)" in exact_diff
    assert "+    return subtotal * (1 - discount)" in exact_diff

    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["status", run_identifier],
    )

    assert status_result.exit_code == 0, status_result.output
    assert "Phase: pending_approval" in status_result.output
    assert "Candidate Artifact: attempts/1/candidate.json" in status_result.output
    assert f"Candidate Checksum: {candidate_checksum}" in status_result.output
    assert "Candidate Diff: attempts/1/candidate.diff" in status_result.output
    assert f"Diff Checksum: {diff_checksum}" in status_result.output
    assert exact_diff in status_result.output
    assert "Model Requests: 2" in status_result.output
    assert "Tool Executions: 5" in status_result.output
    assert "Files Read: 2" in status_result.output
    assert "Repair Attempts: 0" in status_result.output


def test_status_displays_independent_resource_budget_usage(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)

    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["status", run_identifier],
    )

    assert status_result.exit_code == 0, status_result.output
    assert "Repair Attempts Budget: 0/3" in status_result.output
    assert "Distinct Files Read Budget: 2/12" in status_result.output
    assert "Files Changed Budget: 0/3" in status_result.output
    assert "Tool Executions Budget: 5/20" in status_result.output
    assert "Model Requests Budget: 2/8" in status_result.output
    assert "Verification Seconds Budget:" in status_result.output
    assert "/60.0" in status_result.output
    assert "Active Seconds Budget:" in status_result.output
    assert "/300.0" in status_result.output


def test_tool_execution_budget_exceeded_names_limit_and_usage(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"

    result = CliRunner().invoke(
        create_cli(model_gateway=ToolBudgetModel(), data_root=data_root),
        ["run", "cart-discount"],
    )

    assert result.exit_code == 0, result.output
    assert "Outcome: Budget Exceeded" in result.output
    assert "Budget: tool_executions" in result.output
    assert "Budget Usage: 20/20" in result.output
    run_root = data_root / _run_identifier(result.output)
    assert not (run_root / "plan.json").exists()
    assert not (run_root / "attempts").exists()


def test_model_infrastructure_failure_is_not_a_failed_repair_attempt(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"

    result = CliRunner().invoke(
        create_cli(model_gateway=FailingPlanModel(), data_root=data_root),
        ["run", "cart-discount"],
    )

    assert result.exit_code == 0, result.output
    assert "Outcome: Error" in result.output
    assert "Error Kind: model_failure" in result.output
    assert "Model Requests: 1" in result.output
    assert "Tool Executions: 1" in result.output
    assert "Repair Attempts: 0" in result.output
    assert "Attempts Exhausted" not in result.output


def test_plan_storage_failure_is_a_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_bytes = Path.write_bytes

    def fail_plan_write(path: Path, data: bytes) -> int:
        if path.name == "plan.json":
            raise OSError("simulated artifact storage outage")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_plan_write)

    result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=tmp_path / "runs"),
        ["run", "cart-discount"],
    )

    assert result.exit_code == 0, result.output
    assert "Outcome: Error" in result.output
    assert "Error Kind: storage_failure" in result.output
    assert "Repair Attempts: 0" in result.output
    assert "Attempts Exhausted" not in result.output


def test_distinct_files_read_budget_exceeded_names_limit_and_usage(tmp_path: Path) -> None:
    result, data_root = _run_trusted_inspection(
        tmp_path,
        FilesReadBudgetModel(),
        extra_files={f"file{index:02}.py": b"VALUE = 1\n" for index in range(12)},
    )

    assert result.exit_code == 0, result.output
    assert "Outcome: Budget Exceeded" in result.output
    assert "Budget: files_read" in result.output
    assert "Budget Usage: 12/12" in result.output
    assert not (data_root / _run_identifier(result.output) / "attempts").exists()


def test_files_changed_budget_accumulates_across_attempts(tmp_path: Path) -> None:
    repository = tmp_path / "four-file-repository"
    repository.mkdir()
    paths = [f"file{index}.py" for index in range(4)]
    for path in paths:
        (repository / path).write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "four-file-contract.toml"
    contract.write_text(
        f'''source_id = "four-file-repository"
issue = "Change no more than three files"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(1)"])}
editable_paths = {json.dumps(paths)}
''',
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    model = CumulativeFilesChangedBudgetModel()
    run_result = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["run-local", str(repository), "--contract", str(contract), "--trust-repository"],
    )
    run_identifier = _run_identifier(run_result.output)

    first_approval = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )
    approve_result = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert first_approval.exit_code == 0, first_approval.output
    assert "status: pending_approval" in first_approval.output
    assert approve_result.exit_code == 0, approve_result.output
    assert "Outcome: Budget Exceeded" in approve_result.output
    assert "Budget: files_changed" in approve_result.output
    assert "Budget Usage: 3/3" in approve_result.output
    assert "Repair Attempts: 1" in approve_result.output
    workspace = data_root / run_identifier / "workspace"
    assert all("# attempt 1" in (workspace / path).read_text() for path in paths[:3])
    assert (workspace / paths[3]).read_text() == "VALUE = 1\n"


def test_model_request_budget_counts_schema_correction_requests(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    model = CorrectedEveryOutputModel()
    run_result = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    first_approval = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    second_approval = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert first_approval.exit_code == 0, first_approval.output
    assert "status: pending_approval" in first_approval.output
    assert second_approval.exit_code == 0, second_approval.output
    assert "Outcome: Budget Exceeded" in second_approval.output
    assert "Budget: model_requests" in second_approval.output
    assert "Budget Usage: 8/8" in second_approval.output
    assert "Repair Attempts: 2" in second_approval.output
    assert "Attempts Exhausted" not in second_approval.output


def test_active_time_budget_counts_model_work(tmp_path: Path) -> None:
    clock = FakeClock()

    result = CliRunner().invoke(
        create_cli(
            model_gateway=SlowPlanModel(clock),
            data_root=tmp_path / "runs",
            clock=clock,
        ),
        ["run", "cart-discount"],
    )

    assert result.exit_code == 0, result.output
    assert "Outcome: Budget Exceeded" in result.output
    assert "Budget: active_seconds" in result.output
    assert "Budget Usage: 301.0/300.0" in result.output
    assert "Candidate Patch" not in result.output


def test_approval_wait_is_excluded_from_active_time(tmp_path: Path) -> None:
    clock = FakeClock()
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root, clock=clock),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)

    clock.advance(1_000.0)
    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root, clock=clock),
        ["approve", run_identifier, "--yes"],
    )
    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root, clock=clock),
        ["status", run_identifier],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert "Outcome: Succeeded" in approve_result.output
    assert status_result.exit_code == 0, status_result.output
    assert "Active Seconds Budget: 0.000/300.0" in status_result.output


@pytest.mark.parametrize("path", ["test_cart.py", "issue.md", "fixture.toml"])
def test_candidate_patch_rejects_tests_issue_and_manifest_changes(
    tmp_path: Path,
    path: str,
) -> None:
    data_root = tmp_path / "runs"

    result = CliRunner().invoke(
        create_cli(
            model_gateway=CandidateScenarioModel("noneditable", path),
            data_root=data_root,
        ),
        ["run", "cart-discount"],
    )

    assert result.exit_code == 2
    assert f"Candidate Patch path is protected: {path}" in result.output
    run_root = next(path for path in data_root.iterdir() if path.is_dir())
    assert (run_root / "workspace" / "cart.py").read_bytes() == (
        Path(__file__).parents[1] / "examples" / "tiny_repo" / "cart.py"
    ).read_bytes()
    assert not (run_root / "attempts" / "1" / "candidate.json").exists()


@pytest.mark.parametrize("path", ["test_cart.py", "issue.md", "fixture.toml"])
def test_candidate_patch_rejects_protected_files_even_when_manifest_marks_them_editable(
    tmp_path: Path,
    path: str,
) -> None:
    fixture = tmp_path / "unsafe-fixture"
    fixture.mkdir()
    (fixture / "fixture.toml").write_text(
        """fixture_id = "unsafe-fixture"
issue_path = "issue.md"
verification = ["pytest", "test_cart.py"]
editable_paths = ["cart.py", "test_cart.py", "issue.md", "fixture.toml"]
""",
        encoding="utf-8",
    )
    (fixture / "issue.md").write_text("# Protected files stay immutable\n", encoding="utf-8")
    (fixture / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    (fixture / "test_cart.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        create_cli(
            model_gateway=CandidateScenarioModel("protected", path),
            data_root=tmp_path / "runs",
            fixture_roots=(fixture,),
        ),
        ["run", "unsafe-fixture"],
    )

    assert result.exit_code == 2
    assert f"Candidate Patch path is protected: {path}" in result.output


@pytest.mark.parametrize(
    ("scenario", "path", "message"),
    [
        ("create", "new.py", "not editable"),
        ("unread", "cart.py", "not explicitly read"),
        ("delete", "cart.py", "cannot delete"),
        ("binary", "cart.py", "must be text"),
        ("stale_hash", "cart.py", "does not match the model read"),
        ("oversized", "cart.py", "exceeds 100 KiB"),
        ("rename", "cart.py", "validation error"),
        ("too_many", "cart.py", "validation error"),
    ],
)
def test_candidate_patch_rejects_unsafe_structured_replacements(
    tmp_path: Path,
    scenario: str,
    path: str,
    message: str,
) -> None:
    result = CliRunner().invoke(
        create_cli(
            model_gateway=CandidateScenarioModel(scenario, path),
            data_root=tmp_path / "runs",
        ),
        ["run", "cart-discount"],
    )

    if message == "validation error":
        assert result.exit_code == 0, result.output
        assert "Outcome: Error" in result.output
        assert "Error Kind: invalid_model_output" in result.output
        assert "Model Requests: 3" in result.output
    else:
        assert result.exit_code == 2
        assert message in result.output


def test_candidate_diff_marks_a_missing_trailing_newline_exactly(tmp_path: Path) -> None:
    result, data_root = _run_trusted_inspection(
        tmp_path,
        CandidateScenarioModel("valid"),
        extra_files={"cart.py": b"VALUE = 1"},
    )
    run_identifier = _run_identifier(result.output)
    diff = (data_root / run_identifier / "attempts" / "1" / "candidate.diff").read_text()

    assert result.exit_code == 0, result.output
    assert "-VALUE = 1\n\\ No newline at end of file\n" in diff
    assert "+VALUE = 1# proposed change\n" in diff


def test_user_rejects_a_pending_candidate_from_a_separate_cli_process(
    tmp_path: Path,
) -> None:
    fixture_repository = Path(__file__).parents[1] / "examples" / "tiny_repo"
    original_source = (fixture_repository / "cart.py").read_bytes()
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    workspace_source = run_root / "workspace" / "cart.py"
    candidate_bytes = (run_root / "attempts" / "1" / "candidate.json").read_bytes()
    candidate_checksum = hashlib.sha256(candidate_bytes).hexdigest()

    reject_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["reject", run_identifier],
    )

    assert reject_result.exit_code == 0, reject_result.output
    assert "Outcome: Rejected" in reject_result.output
    assert "Repair Attempts: 0" in reject_result.output
    assert f"Candidate Checksum: {candidate_checksum}" in reject_result.output
    assert workspace_source.read_bytes() == original_source
    assert (fixture_repository / "cart.py").read_bytes() == original_source

    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["status", run_identifier],
    )

    assert status_result.exit_code == 0, status_result.output
    assert "Phase: rejected" in status_result.output
    assert "Outcome: Rejected" in status_result.output
    assert "Repair Attempts: 0" in status_result.output
    assert f"Candidate Checksum: {candidate_checksum}" in status_result.output
    assert (run_root / "attempts" / "1" / "candidate.json").read_bytes() == candidate_bytes


def test_run_reject_and_status_work_across_real_os_processes(tmp_path: Path) -> None:
    run_result = _invoke_cli_process(tmp_path, "run", "cart-discount")
    run_identifier = _run_identifier(run_result.stdout)

    reject_result = _invoke_cli_process(tmp_path, "reject", run_identifier)
    status_result = _invoke_cli_process(tmp_path, "status", run_identifier)

    assert run_result.returncode == 0, run_result.stderr
    assert "status: pending_approval" in run_result.stdout
    assert reject_result.returncode == 0, reject_result.stderr
    assert "Outcome: Rejected" in reject_result.stdout
    assert status_result.returncode == 0, status_result.stderr
    assert "Phase: rejected" in status_result.stdout
    assert "Outcome: Rejected" in status_result.stdout


def test_repeated_reject_does_not_advance_the_graph_again(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    first_reject = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["reject", run_identifier],
    )
    database_before = (data_root / "checkpoints.sqlite").read_bytes()

    repeated_reject = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["reject", run_identifier],
    )

    assert first_reject.exit_code == 0, first_reject.output
    assert repeated_reject.exit_code == 2
    assert "not awaiting Approval (current phase: rejected)" in repeated_reject.output
    assert (data_root / "checkpoints.sqlite").read_bytes() == database_before


def test_reject_refuses_a_non_pending_patch_run(tmp_path: Path) -> None:
    repository = tmp_path / "passing-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "passing-contract.toml"
    contract.write_text(
        f"""source_id = "passing-repository"
issue = "No reproducible failure"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(0)"])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )
    run_identifier = _run_identifier(run_result.output)

    reject_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["reject", run_identifier],
    )

    assert reject_result.exit_code == 2
    assert "not awaiting Approval (current phase: issue_not_reproduced)" in reject_result.output


def test_reject_reports_busy_without_advancing_the_pending_run(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    candidate_path = data_root / run_identifier / "attempts" / "1" / "candidate.json"
    candidate_before = candidate_path.read_bytes()

    lock_holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "from patch_code_agent.locking import RunMutationLock; "
                "lock = RunMutationLock(Path(sys.argv[1]), sys.argv[2]); "
                "lock.__enter__(); print('locked', flush=True); "
                "sys.stdin.readline(); lock.__exit__(None, None, None)"
            ),
            str(data_root),
            run_identifier,
        ],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert lock_holder.stdout is not None
        assert lock_holder.stdout.readline().strip() == "locked"
        reject_result = CliRunner().invoke(
            create_cli(model_gateway=ScriptedModel(), data_root=data_root),
            ["reject", run_identifier],
        )
    finally:
        if lock_holder.stdin is not None:
            lock_holder.stdin.write("release\n")
            lock_holder.stdin.flush()
        lock_holder.wait(timeout=10)

    assert reject_result.exit_code == 2
    assert f"Patch Run is busy: {run_identifier}" in reject_result.output
    assert candidate_path.read_bytes() == candidate_before
    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["status", run_identifier],
    )
    assert status_result.exit_code == 0, status_result.output
    assert "Phase: pending_approval" in status_result.output


def test_reject_never_creates_a_lock_outside_the_data_root(tmp_path: Path) -> None:
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    data_root = tmp_path / "runs"
    data_root.mkdir()

    result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["reject", str(outside_directory)],
    )

    assert result.exit_code == 2
    assert "Invalid Run Identifier" in result.output
    assert not (outside_directory / ".mutation.lock").exists()


def test_reject_never_follows_a_symlinked_lock_file(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    outside_lock = tmp_path / "outside-lock"
    outside_lock.write_text("unchanged\n", encoding="utf-8")
    (data_root / run_identifier / ".mutation.lock").symlink_to(outside_lock)

    reject_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["reject", run_identifier],
    )

    assert reject_result.exit_code == 2
    assert f"Unsafe Patch Run lock file: {run_identifier}" in reject_result.output
    assert outside_lock.read_text(encoding="utf-8") == "unchanged\n"


def test_user_approves_and_verifies_a_candidate_from_a_separate_cli_process(
    tmp_path: Path,
) -> None:
    fixture_repository = Path(__file__).parents[1] / "examples" / "tiny_repo"
    original_source = (fixture_repository / "cart.py").read_bytes()
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    candidate_diff = (run_root / "attempts" / "1" / "candidate.diff").read_text()
    candidate_checksum = hashlib.sha256(
        (run_root / "attempts" / "1" / "candidate.json").read_bytes()
    ).hexdigest()

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert candidate_diff in approve_result.output
    assert f"Candidate Checksum: {candidate_checksum}" in approve_result.output
    assert "Outcome: Succeeded" in approve_result.output
    assert "Repair Attempts: 1" in approve_result.output
    assert "Verification: passed" in approve_result.output
    assert "Cumulative Diff: cumulative.diff" in approve_result.output
    assert "subtotal = sum(prices)" in (run_root / "workspace" / "cart.py").read_text()
    assert (fixture_repository / "cart.py").read_bytes() == original_source
    verification = json.loads((run_root / "attempts" / "1" / "verification.json").read_text())
    assert verification["outcome"] == "passed"
    assert verification["exit_code"] == 0
    assert "1 passed" in (run_root / "attempts" / "1" / "verification.log").read_text()
    assert (run_root / "cumulative.diff").read_text() == candidate_diff

    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["status", run_identifier],
    )
    assert status_result.exit_code == 0, status_result.output
    assert "Phase: succeeded" in status_result.output
    assert "Outcome: Succeeded" in status_result.output
    assert "Repair Attempts: 1" in status_result.output


def test_approval_prompt_defaults_to_no_and_keeps_the_run_pending(tmp_path: Path) -> None:
    fixture_repository = Path(__file__).parents[1] / "examples" / "tiny_repo"
    original_source = (fixture_repository / "cart.py").read_bytes()
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    candidate_diff = (data_root / run_identifier / "attempts" / "1" / "candidate.diff").read_text()

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier],
        input="\n",
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert candidate_diff in approve_result.output
    assert "Approve this exact Candidate Patch? [y/N]" in approve_result.output
    assert "Approval cancelled; Patch Run remains pending." in approve_result.output
    assert not (data_root / run_identifier / "attempts" / "1" / "apply.json").exists()
    assert (data_root / run_identifier / "workspace" / "cart.py").read_bytes() == original_source

    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["status", run_identifier],
    )
    assert "Phase: pending_approval" in status_result.output
    assert "Repair Attempts: 0" in status_result.output


def test_interactive_yes_approves_the_exact_candidate(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    candidate_checksum = hashlib.sha256(
        (data_root / run_identifier / "attempts" / "1" / "candidate.json").read_bytes()
    ).hexdigest()

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier],
        input="y\n",
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert f"Candidate Checksum: {candidate_checksum}" in approve_result.output
    assert "Approve this exact Candidate Patch? [y/N]: y" in approve_result.output
    assert "Outcome: Succeeded" in approve_result.output


def test_run_approve_and_status_work_across_real_os_processes(tmp_path: Path) -> None:
    fixture_repository = Path(__file__).parents[1] / "examples" / "tiny_repo"
    original_source = (fixture_repository / "cart.py").read_bytes()
    run_result = _invoke_cli_process(tmp_path, "run", "cart-discount")
    run_identifier = _run_identifier(run_result.stdout)

    approve_result = _invoke_cli_process(tmp_path, "approve", run_identifier, "--yes")
    status_result = _invoke_cli_process(tmp_path, "status", run_identifier)

    assert run_result.returncode == 0, run_result.stderr
    assert approve_result.returncode == 0, approve_result.stderr
    assert "Candidate Checksum:" in approve_result.stdout
    assert "Outcome: Succeeded" in approve_result.stdout
    assert status_result.returncode == 0, status_result.stderr
    assert "Phase: succeeded" in status_result.stdout
    assert "Verification: passed" in status_result.stdout
    assert (fixture_repository / "cart.py").read_bytes() == original_source


def test_repeated_approve_does_not_repeat_apply_verification_or_counters(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    first_approve = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )
    run_root = data_root / run_identifier
    database_before = (data_root / "checkpoints.sqlite").read_bytes()
    workspace_before = (run_root / "workspace" / "cart.py").read_bytes()
    verification_before = (run_root / "attempts" / "1" / "verification.log").read_bytes()

    repeated_approve = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert first_approve.exit_code == 0, first_approve.output
    assert repeated_approve.exit_code == 2
    assert "not awaiting Approval (current phase: succeeded)" in repeated_approve.output
    assert (data_root / "checkpoints.sqlite").read_bytes() == database_before
    assert (run_root / "workspace" / "cart.py").read_bytes() == workspace_before
    assert (run_root / "attempts" / "1" / "verification.log").read_bytes() == verification_before

    status_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["status", run_identifier],
    )
    assert "Repair Attempts: 1" in status_result.output


def test_approve_reports_busy_without_advancing_the_pending_run(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    candidate_path = data_root / run_identifier / "attempts" / "1" / "candidate.json"
    candidate_before = candidate_path.read_bytes()

    lock_holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "from patch_code_agent.locking import RunMutationLock; "
                "lock = RunMutationLock(Path(sys.argv[1]), sys.argv[2]); "
                "lock.__enter__(); print('locked', flush=True); "
                "sys.stdin.readline(); lock.__exit__(None, None, None)"
            ),
            str(data_root),
            run_identifier,
        ],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert lock_holder.stdout is not None
        assert lock_holder.stdout.readline().strip() == "locked"
        approve_result = CliRunner().invoke(
            create_cli(model_gateway=ScriptedModel(), data_root=data_root),
            ["approve", run_identifier, "--yes"],
        )
    finally:
        if lock_holder.stdin is not None:
            lock_holder.stdin.write("release\n")
            lock_holder.stdin.flush()
        lock_holder.wait(timeout=10)

    assert approve_result.exit_code == 2
    assert f"Patch Run is busy: {run_identifier}" in approve_result.output
    assert candidate_path.read_bytes() == candidate_before
    assert not (data_root / run_identifier / "attempts" / "1" / "apply.json").exists()


def test_approve_continues_when_every_replacement_is_already_applied(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    candidate = json.loads((run_root / "attempts" / "1" / "candidate.json").read_text())
    replacement = candidate["candidate"]["replacements"][0]
    (run_root / "workspace" / replacement["path"]).write_text(
        replacement["new_content"], encoding="utf-8"
    )

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert "Outcome: Succeeded" in approve_result.output
    apply_summary = json.loads((run_root / "attempts" / "1" / "apply.json").read_text())
    assert apply_summary["outcome"] == "already_applied"
    assert (run_root / "attempts" / "1" / "verification.json").is_file()


def test_approve_reports_workspace_changed_without_running_verification(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    workspace_source = run_root / "workspace" / "cart.py"
    workspace_source.write_text(
        workspace_source.read_text() + "# external edit\n", encoding="utf-8"
    )

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert "Outcome: Workspace Changed" in approve_result.output
    assert "Error Kind: workspace_changed" in approve_result.output
    assert "Repair Attempts: 0" in approve_result.output
    assert not (run_root / "attempts" / "1" / "verification.json").exists()


def test_apply_does_not_follow_a_parent_symlink_swapped_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "nested-repository"
    (repository / "pkg").mkdir(parents=True)
    (repository / "pkg" / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "nested-contract.toml"
    contract.write_text(
        f"""source_id = "nested-repository"
issue = "Repair nested source"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(1)"])}
editable_paths = ["pkg/cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(
            model_gateway=CandidateScenarioModel("valid", "pkg/cart.py"),
            data_root=data_root,
        ),
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )
    run_identifier = _run_identifier(run_result.output)
    workspace = data_root / run_identifier / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_source = outside / "cart.py"
    outside_source.write_text("OUTSIDE = True\n", encoding="utf-8")
    original_read = WorkspaceInspector.read_file
    swapped = False

    def read_then_swap_parent(inspector: WorkspaceInspector, path: str):
        nonlocal swapped
        result = original_read(inspector, path)
        if not swapped:
            (workspace / "pkg").rename(workspace / "pkg-original")
            (workspace / "pkg").symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(WorkspaceInspector, "read_file", read_then_swap_parent)

    approve_result = CliRunner().invoke(
        create_cli(
            model_gateway=CandidateScenarioModel("valid", "pkg/cart.py"),
            data_root=data_root,
        ),
        ["approve", run_identifier, "--yes"],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert "Outcome: Workspace Changed" in approve_result.output
    assert outside_source.read_text(encoding="utf-8") == "OUTSIDE = True\n"
    assert (workspace / "pkg-original" / "cart.py").read_text() == "VALUE = 1\n"


def test_approve_reports_partial_apply_for_mixed_before_and_after_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "two-file-repository"
    repository.mkdir()
    (repository / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (repository / "two.py").write_text("TWO = 2\n", encoding="utf-8")
    verification_program = (
        "from pathlib import Path; "
        "raise SystemExit(0 if all('# approved change' in Path(path).read_text() "
        "for path in ('one.py', 'two.py')) else 1)"
    )
    contract = tmp_path / "two-file-contract.toml"
    contract.write_text(
        f"""source_id = "two-file-repository"
issue = "Repair both files"
verification = {json.dumps([sys.executable, "-c", verification_program])}
editable_paths = ["one.py", "two.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=TwoFileCandidateModel(), data_root=data_root),
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    candidate = json.loads((run_root / "attempts" / "1" / "candidate.json").read_text())
    first, second = candidate["candidate"]["replacements"]
    (run_root / "workspace" / first["path"]).write_text(first["new_content"], encoding="utf-8")

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=TwoFileCandidateModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert "Outcome: Error" in approve_result.output
    assert "Error Kind: partial_apply" in approve_result.output
    assert (run_root / "workspace" / second["path"]).read_text() != second["new_content"]
    assert not (run_root / "attempts" / "1" / "verification.json").exists()


def test_yes_cannot_skip_candidate_checksum_validation(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    candidate_path = run_root / "attempts" / "1" / "candidate.json"
    candidate_path.write_text(candidate_path.read_text() + " ", encoding="utf-8")
    database_before = (data_root / "checkpoints.sqlite").read_bytes()

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert approve_result.exit_code == 2
    assert "Candidate Patch does not match its replay completion checksums" in approve_result.output
    assert (data_root / "checkpoints.sqlite").read_bytes() == database_before
    assert not (run_root / "attempts" / "1" / "apply.json").exists()


def test_failed_first_attempt_is_diagnosed_and_second_attempt_succeeds(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runs"
    model = ScriptedModel(repair_failures=1)
    run_result = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier
    plan_before = (run_root / "plan.json").read_bytes()

    first_approve = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert first_approve.exit_code == 0, first_approve.output
    assert "status: pending_approval" in first_approve.output
    assert "Repair Attempts: 1" in first_approve.output
    assert "Model Requests: 4" in first_approve.output
    assert "Candidate Artifact: attempts/2/candidate.json" in first_approve.output
    assert "return subtotal - discount" in (run_root / "workspace" / "cart.py").read_text()
    diagnosis = json.loads((run_root / "attempts" / "1" / "diagnosis.json").read_text())
    first_preimages = (run_root / "attempts" / "1" / "preimages.json").read_bytes()
    assert diagnosis["attempt"] == 1
    assert diagnosis["diagnosis"]["failure_summary"]
    assert "1 failed" in diagnosis["verification_output_excerpt"]
    assert (run_root / "plan.json").read_bytes() == plan_before

    second_approve = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert second_approve.exit_code == 0, second_approve.output
    assert "Outcome: Succeeded" in second_approve.output
    assert "Repair Attempts: 2" in second_approve.output
    assert "Model Requests: 4" in second_approve.output
    assert "return subtotal * (1 - discount)" in (run_root / "workspace" / "cart.py").read_text()
    assert (run_root / "plan.json").read_bytes() == plan_before
    assert (run_root / "attempts" / "1" / "preimages.json").read_bytes() == first_preimages
    assert (run_root / "attempts" / "2" / "preimages.json").is_file()
    for attempt in (1, 2):
        assert (run_root / "attempts" / str(attempt) / "candidate.json").is_file()
        assert (run_root / "attempts" / str(attempt) / "candidate.diff").is_file()
        assert (run_root / "attempts" / str(attempt) / "verification.json").is_file()
    cumulative_diff = (run_root / "cumulative.diff").read_text()
    assert "return sum(prices) - discount" in cumulative_diff
    assert "return subtotal * (1 - discount)" in cumulative_diff


def test_three_failing_attempts_are_exhausted_without_a_fourth_candidate(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runs"
    model = ScriptedModel(repair_failures=3)
    run_result = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    run_root = data_root / run_identifier

    approvals = [
        CliRunner().invoke(
            create_cli(model_gateway=model, data_root=data_root),
            ["approve", run_identifier, "--yes"],
        )
        for _ in range(3)
    ]

    assert all(result.exit_code == 0 for result in approvals)
    assert "Candidate Artifact: attempts/2/candidate.json" in approvals[0].output
    assert "Candidate Artifact: attempts/3/candidate.json" in approvals[1].output
    assert "Outcome: Attempts Exhausted" in approvals[2].output
    assert "Repair Attempts: 3" in approvals[2].output
    assert not (run_root / "attempts" / "4").exists()
    assert (run_root / "attempts" / "1" / "diagnosis.json").is_file()
    assert (run_root / "attempts" / "2" / "diagnosis.json").is_file()
    assert (run_root / "attempts" / "3" / "diagnosis.json").is_file()


def test_rejecting_a_follow_up_candidate_does_not_add_a_repair_attempt(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runs"
    model = ScriptedModel(repair_failures=1)
    run_result = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["run", "cart-discount"],
    )
    run_identifier = _run_identifier(run_result.output)
    first_approve = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    reject_result = CliRunner().invoke(
        create_cli(model_gateway=model, data_root=data_root),
        ["reject", run_identifier],
    )

    assert first_approve.exit_code == 0, first_approve.output
    assert reject_result.exit_code == 0, reject_result.output
    assert "Outcome: Rejected" in reject_result.output
    assert "Repair Attempts: 1" in reject_result.output
    assert not (data_root / run_identifier / "attempts" / "2" / "verification.json").exists()


def test_model_cannot_read_an_absolute_workspace_path(tmp_path: Path) -> None:
    cli = create_cli(
        model_gateway=ScriptedModel(
            inspection_calls=(ScriptedInspectionCall(operation="read", argument="/etc/passwd"),)
        ),
        data_root=tmp_path / "runs",
    )

    result = CliRunner().invoke(cli, ["run", "cart-discount"])

    assert result.exit_code == 2
    assert "paths must be relative and without traversal" in result.output


@pytest.mark.parametrize("path", ["../outside.py", ".git/config", "venv/secret.py"])
def test_model_cannot_read_traversing_or_ignored_paths(tmp_path: Path, path: str) -> None:
    result, _ = _run_trusted_inspection(
        tmp_path,
        ScriptedModel(inspection_calls=(ScriptedInspectionCall("read", path),)),
    )

    assert result.exit_code == 2
    assert "traversal" in result.output or "hidden or ignored" in result.output


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("binary.dat", b"abc\x00def", "binary"),
        ("non-utf8.txt", b"\xff\xfe", "not UTF-8"),
        ("oversized.txt", b"x" * (100 * 1024 + 1), "exceeds 100 KiB"),
    ],
)
def test_model_can_only_read_bounded_utf8_text(
    tmp_path: Path,
    name: str,
    content: bytes,
    message: str,
) -> None:
    result, _ = _run_trusted_inspection(
        tmp_path,
        ScriptedModel(inspection_calls=(ScriptedInspectionCall("read", name),)),
        extra_files={name: content},
    )

    assert result.exit_code == 2
    assert message in result.output


def test_model_cannot_read_a_symlink_created_during_baseline(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = True\n", encoding="utf-8")
    program = f"import os; os.symlink({str(outside)!r}, 'link.py'); raise SystemExit(1)"
    result, _ = _run_trusted_inspection(
        tmp_path,
        ScriptedModel(inspection_calls=(ScriptedInspectionCall("read", "link.py"),)),
        baseline_program=program,
    )

    assert result.exit_code == 2
    assert "must not contain a symbolic link" in result.output


def test_search_response_is_predictably_limited_to_32_kib(tmp_path: Path) -> None:
    model = SearchRecordingModel()
    result, _ = _run_trusted_inspection(
        tmp_path,
        model,
        extra_files={"large.txt": ("needle: matching source line\n" * 2500).encode()},
    )

    assert result.exit_code == 0, result.output
    assert model.search_result is not None
    assert len(model.search_result.text.encode("utf-8")) == 32 * 1024
    assert model.search_result.truncated is True
    assert model.search_result.text.startswith("large.txt:1:needle")


def test_model_plan_is_runtime_validated_before_artifact_persistence(tmp_path: Path) -> None:
    result, data_root = _run_trusted_inspection(tmp_path, InvalidPlanModel())

    assert result.exit_code == 0, result.output
    assert "Outcome: Error" in result.output
    assert "Error Kind: invalid_model_output" in result.output
    assert "Model Requests: 2" in result.output
    assert "Repair Attempts: 0" in result.output
    run_roots = [path for path in data_root.iterdir() if path.is_dir()]
    assert len(run_roots) == 1
    assert not (run_roots[0] / "plan.json").exists()


def test_user_starts_trusted_repository_run_from_explicit_contract(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    original_source = b"def total(items):\n    return sum(items)\n"
    (repository / "cart.py").write_bytes(original_source)
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        f"""source_id = "trusted-cart"
issue = "Fix the incorrect cart total"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(1)"])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    run_identifier = _run_identifier(result.output)
    workspace = data_root / run_identifier / "workspace"
    assert (workspace / "cart.py").read_bytes() == original_source
    assert (repository / "cart.py").read_bytes() == original_source
    assert "Trusted Repository: trusted-cart" in result.output
    assert "status: pending_approval" in result.output
    revision_match = re.search(r"Source Revision: ([0-9a-f]{64})", result.output)
    assert revision_match is not None

    status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    status = CliRunner().invoke(status_cli, ["status", run_identifier])
    assert status.exit_code == 0, status.output
    assert "Trusted Repository: trusted-cart" in status.output
    assert f"Source Revision: {revision_match.group(1)}" in status.output
    assert "Phase: pending_approval" in status.output


def test_run_workspace_excludes_visible_virtual_environment(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    virtual_environment = repository / "venv" / "lib"
    virtual_environment.mkdir(parents=True)
    (virtual_environment / "dependency.py").write_text("SECRET = True\n", encoding="utf-8")
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        f"""source_id = "trusted-cart"
issue = "Fix the incorrect cart total"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(1)"])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    workspace = data_root / _run_identifier(result.output) / "workspace"
    assert not (workspace / "venv").exists()
    assert "Relevant Files: cart.py" in result.output


def test_passing_baseline_becomes_issue_not_reproduced(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        f"""source_id = "trusted-passing"
issue = "Reproduce the reported issue"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(0)"])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Baseline Verification: passed" in result.output
    assert "Outcome: Issue Not Reproduced" in result.output
    assert "Model Requests: 0" in result.output
    assert "PatchCodeAgent plan" not in result.output
    run_identifier = _run_identifier(result.output)

    status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    status = CliRunner().invoke(status_cli, ["status", run_identifier])
    assert status.exit_code == 0, status.output
    assert "Phase: issue_not_reproduced" in status.output
    assert "Model Requests: 0" in status.output


def test_baseline_exit_code_two_becomes_verification_error(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        f"""source_id = "trusted-error"
issue = "Reproduce the reported issue"
verification = {json.dumps([sys.executable, "-c", "raise SystemExit(2)"])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Baseline Verification: error" in result.output
    assert "Outcome: Error" in result.output
    run_identifier = _run_identifier(result.output)
    baseline = json.loads((data_root / run_identifier / "baseline" / "result.json").read_text())
    assert baseline["exit_code"] == 2
    assert baseline["error_kind"] == "verification_exit_code"

    status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    status = CliRunner().invoke(status_cli, ["status", run_identifier])
    assert status.exit_code == 0, status.output
    assert "Phase: error" in status.output


def test_repair_verification_infrastructure_error_is_not_attempts_exhausted(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repair-verification-error"
    repository.mkdir()
    source = Path(__file__).parents[1] / "examples" / "tiny_repo" / "cart.py"
    (repository / "cart.py").write_bytes(source.read_bytes())
    verification_program = (
        "from pathlib import Path; "
        "text = Path('cart.py').read_text(); "
        "raise SystemExit(1 if 'sum(prices) - discount' in text else 2)"
    )
    contract = tmp_path / "repair-verification-error.toml"
    contract.write_text(
        f'''source_id = "repair-verification-error"
issue = "Repair the discount calculation"
verification = {json.dumps([sys.executable, "-c", verification_program])}
editable_paths = ["cart.py"]
''',
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    run_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["run-local", str(repository), "--contract", str(contract), "--trust-repository"],
    )
    run_identifier = _run_identifier(run_result.output)

    approve_result = CliRunner().invoke(
        create_cli(model_gateway=ScriptedModel(), data_root=data_root),
        ["approve", run_identifier, "--yes"],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert "Outcome: Error" in approve_result.output
    assert "Error Kind: verification_exit_code" in approve_result.output
    assert "Repair Attempts: 1" in approve_result.output
    assert "Attempts Exhausted" not in approve_result.output


def test_baseline_timeout_becomes_budget_exceeded(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        f"""source_id = "trusted-timeout"
issue = "Reproduce the reported issue"
verification = {json.dumps([sys.executable, "-c", "import time; time.sleep(1)"])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=data_root,
        verification_timeout_seconds=0.01,
    )

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Baseline Verification: timeout" in result.output
    assert "Outcome: Budget Exceeded" in result.output
    assert "PatchCodeAgent plan" not in result.output
    run_identifier = _run_identifier(result.output)
    baseline = json.loads((data_root / run_identifier / "baseline" / "result.json").read_text())
    assert baseline["error_kind"] == "verification_timeout"
    assert baseline["timeout_seconds"] == 0.01

    status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    status = CliRunner().invoke(status_cli, ["status", run_identifier])
    assert status.exit_code == 0, status.output
    assert "Phase: budget_exceeded" in status.output


def test_baseline_uses_minimal_environment_and_bounded_checkpoint_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-reach-verification")
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("VALUE = 1\n", encoding="utf-8")
    script = (
        "import os; "
        "print(os.getenv('GEMINI_API_KEY', 'secret-not-present')); "
        "print('x' * 40000); "
        "raise SystemExit(1)"
    )
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        f"""source_id = "trusted-environment"
issue = "Reproduce the reported issue"
verification = {json.dumps([sys.executable, "-c", script])}
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    run_identifier = _run_identifier(result.output)
    baseline_root = data_root / run_identifier / "baseline"
    full_output = (baseline_root / "output.log").read_text()
    assert "secret-not-present" in full_output
    assert "must-not-reach-verification" not in full_output
    assert len(full_output) > 32 * 1024
    baseline = json.loads((baseline_root / "result.json").read_text())
    assert baseline["output_truncated"] is True
    assert len(baseline["output_excerpt"].encode()) <= 32 * 1024
    assert "must-not-reach-verification" not in baseline["output_excerpt"]


def test_user_must_explicitly_acknowledge_trusted_repository_execution(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        """source_id = "trusted-cart"
issue = "Fix the incorrect cart total"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        ["run-local", str(repository), "--contract", str(contract)],
    )

    assert result.exit_code != 0
    assert "requires explicit --trust-repository acknowledgement" in result.output
    assert not data_root.exists()


def test_trusted_repository_contract_must_stay_outside_source_tree(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    contract = repository / "patch-run.toml"
    contract.write_text(
        """source_id = "trusted-cart"
issue = "Fix the incorrect cart total"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code != 0
    assert "Patch Run Contract must be outside the Repository Source" in result.output
    assert not data_root.exists()


def test_trusted_repository_rejects_run_storage_inside_source_tree(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        """source_id = "trusted-cart"
issue = "Fix the incorrect cart total"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = repository / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code != 0
    assert "Run storage must not overlap the Repository Source" in result.output
    assert not data_root.exists()


def test_trusted_repository_rejects_symlinked_source_root(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    repository_link = tmp_path / "repository-link"
    repository_link.symlink_to(repository, target_is_directory=True)
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        """source_id = "trusted-cart"
issue = "Fix the incorrect cart total"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository_link),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code != 0
    assert "Repository Source must not be a symbolic link" in result.output
    assert not data_root.exists()


def test_user_gets_clear_error_for_invalid_trusted_repository_contract(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    contract = tmp_path / "patch-run.toml"
    contract.write_text('source_id = "trusted-cart"\n', encoding="utf-8")
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code != 0
    assert "Invalid Patch Run Contract" in result.output
    assert not data_root.exists()


def test_trusted_repository_rejects_oversized_patch_run_contract(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    oversized_issue = "x" * 32_769
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        f"""source_id = "trusted-cart"
issue = "{oversized_issue}"
verification = ["pytest"]
editable_paths = ["cart.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code != 0
    assert "Invalid Patch Run Contract" in result.output
    assert "at most 32768 characters" in result.output
    assert not data_root.exists()


def test_trusted_repository_rejects_ignored_editable_path(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    hidden_directory = repository / ".config"
    hidden_directory.mkdir(parents=True)
    (hidden_directory / "settings.py").write_text("VALUE = 1\n", encoding="utf-8")
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        """source_id = "trusted-settings"
issue = "Fix the hidden setting"
verification = ["pytest"]
editable_paths = [".config/settings.py"]
""",
        encoding="utf-8",
    )
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(
        cli,
        [
            "run-local",
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code != 0
    assert "Repository Source editable path is ignored: .config/settings.py" in result.output
    assert not data_root.exists()


def test_two_patch_runs_have_distinct_workspaces(tmp_path: Path) -> None:
    fixture_repository = Path(__file__).parents[1] / "examples" / "tiny_repo"
    original_cart = (fixture_repository / "cart.py").read_bytes()
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    first_result = CliRunner().invoke(cli, ["run", "cart-discount"])
    first_run_id = _run_identifier(first_result.output)
    first_cart = data_root / first_run_id / "workspace" / "cart.py"
    first_cart.write_text("# changed only in the first Run Workspace\n", encoding="utf-8")

    second_result = CliRunner().invoke(cli, ["run", "cart-discount"])
    second_run_id = _run_identifier(second_result.output)
    second_cart = data_root / second_run_id / "workspace" / "cart.py"

    assert first_result.exit_code == 0, first_result.output
    assert second_result.exit_code == 0, second_result.output
    assert first_run_id != second_run_id
    assert first_cart.read_text(encoding="utf-8").startswith("# changed only")
    assert second_cart.read_bytes() == original_cart
    assert (fixture_repository / "cart.py").read_bytes() == original_cart


def test_user_gets_clear_error_for_unknown_fixture_repository(tmp_path: Path) -> None:
    cli = create_cli(model_gateway=ScriptedModel(), data_root=tmp_path / "runs")

    result = CliRunner().invoke(cli, ["run", "missing-fixture"])

    assert result.exit_code != 0
    assert "Unknown Fixture Repository: missing-fixture" in result.output


def test_user_reads_patch_run_status_from_separate_cli_invocations(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    start_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    start_result = CliRunner().invoke(start_cli, ["run", "cart-discount"])
    run_identifier = _run_identifier(start_result.output)

    first_status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    database_before = (data_root / "checkpoints.sqlite").read_bytes()
    first_status = CliRunner().invoke(first_status_cli, ["status", run_identifier])
    database_after = (data_root / "checkpoints.sqlite").read_bytes()
    second_status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    second_status = CliRunner().invoke(second_status_cli, ["status", run_identifier])

    assert first_status.exit_code == 0, first_status.output
    assert first_status.output == second_status.output
    assert f"Run Identifier: {run_identifier}" in first_status.output
    assert "Fixture Repository: cart-discount" in first_status.output
    assert "Phase: pending_approval" in first_status.output
    assert database_after == database_before


def test_user_gets_clear_error_for_unknown_run_identifier(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(cli, ["status", "missing-run"])

    assert result.exit_code != 0
    assert "Unknown Run Identifier: missing-run" in result.output
    assert not data_root.exists()
