import re
from pathlib import Path

from typer.testing import CliRunner

from patch_code_agent.cli import create_cli
from patch_code_agent.model import ScriptedModel


def _run_identifier(output: str) -> str:
    match = re.search(r"Run Identifier: ([0-9a-f-]{36})", output)
    assert match is not None
    return match.group(1)


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
    cli = create_cli(
        model_gateway=ScriptedModel(),
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
    assert "Run Identifier:" in result.output
    assert "status: planned" in result.output


def test_user_starts_trusted_repository_run_from_explicit_contract(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    original_source = b"def total(items):\n    return sum(items)\n"
    (repository / "cart.py").write_bytes(original_source)
    contract = tmp_path / "patch-run.toml"
    contract.write_text(
        """source_id = "trusted-cart"
issue = "Fix the incorrect cart total"
verification = ["pytest", "test_cart.py"]
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
    assert "status: planned" in result.output
    revision_match = re.search(r"Source Revision: ([0-9a-f]{64})", result.output)
    assert revision_match is not None

    status_cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)
    status = CliRunner().invoke(status_cli, ["status", run_identifier])
    assert status.exit_code == 0, status.output
    assert "Trusted Repository: trusted-cart" in status.output
    assert f"Source Revision: {revision_match.group(1)}" in status.output
    assert "Phase: planned" in status.output


def test_run_workspace_excludes_visible_virtual_environment(tmp_path: Path) -> None:
    repository = tmp_path / "trusted-repository"
    repository.mkdir()
    (repository / "cart.py").write_text("def total():\n    return 0\n", encoding="utf-8")
    virtual_environment = repository / "venv" / "lib"
    virtual_environment.mkdir(parents=True)
    (virtual_environment / "dependency.py").write_text("SECRET = True\n", encoding="utf-8")
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
            str(repository),
            "--contract",
            str(contract),
            "--trust-repository",
        ],
    )

    assert result.exit_code == 0, result.output
    workspace = data_root / _run_identifier(result.output) / "workspace"
    assert not (workspace / "venv").exists()
    assert "python files inspected: 1" in result.output


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
    assert "Phase: planned" in first_status.output
    assert database_after == database_before


def test_user_gets_clear_error_for_unknown_run_identifier(tmp_path: Path) -> None:
    data_root = tmp_path / "runs"
    cli = create_cli(model_gateway=ScriptedModel(), data_root=data_root)

    result = CliRunner().invoke(cli, ["status", "missing-run"])

    assert result.exit_code != 0
    assert "Unknown Run Identifier: missing-run" in result.output
    assert not data_root.exists()
