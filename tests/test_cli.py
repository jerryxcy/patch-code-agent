from pathlib import Path

from typer.testing import CliRunner

from patch_code_agent.cli import create_cli
from patch_code_agent.model import ScriptedModel


def test_user_runs_scaffold_with_injected_application_dependencies(tmp_path: Path) -> None:
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "cart.py").write_text(
        "def total(items):\n    return sum(items)\n",
        encoding="utf-8",
    )
    cli = create_cli(
        model_gateway=ScriptedModel(),
        data_root=tmp_path / "runs",
    )

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "Fix the cart total",
            "--repo",
            str(repository),
            "--thread-id",
            "acceptance-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PatchCodeAgent plan" in result.output
    assert "Inspect the issue against cart.py" in result.output
    assert "thread_id: acceptance-run" in result.output
    assert "python files inspected: 1" in result.output
    assert "status: planned" in result.output
