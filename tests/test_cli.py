import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from patch_code_agent.cli import create_cli
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


def _run_identifier(output: str) -> str:
    match = re.search(r"Run Identifier: ([0-9a-f-]{36})", output)
    assert match is not None
    return match.group(1)


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
        f'''source_id = "inspection-repository"
issue = "Inspect the reported problem"
verification = {json.dumps([sys.executable, "-c", baseline_program])}
editable_paths = ["cart.py"]
''',
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
    assert f"Candidate Patch path is not editable: {path}" in result.output
    run_root = next(path for path in data_root.iterdir() if path.is_dir())
    assert (run_root / "workspace" / "cart.py").read_bytes() == (
        Path(__file__).parents[1] / "examples" / "tiny_repo" / "cart.py"
    ).read_bytes()
    assert not (run_root / "attempts" / "1" / "candidate.json").exists()


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

    assert result.exit_code == 2
    assert message in result.output


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

    assert result.exit_code == 2
    assert "validation error" in result.output
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
