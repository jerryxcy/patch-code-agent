from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel

from patch_code_agent.application import PatchCodeAgent
from patch_code_agent.model import ModelGateway, ScriptedModel


def create_cli(
    *,
    model_gateway: ModelGateway | None = None,
    data_root: Path | None = None,
) -> typer.Typer:
    """Create the CLI with all application dependencies at one seam."""
    cli = typer.Typer(no_args_is_help=True, help="Run the PatchCodeAgent coding-agent harness.")
    console = Console()
    selected_model = model_gateway if model_gateway is not None else ScriptedModel()
    selected_data_root = data_root if data_root is not None else Path(".patch-code-agent")
    application = PatchCodeAgent(
        model_gateway=selected_model,
        data_root=selected_data_root,
    )


    @cli.callback()
    def main() -> None:
        """PatchCodeAgent command-line interface."""


    @cli.command()
    def run(
        issue: Annotated[str, typer.Argument(help="Bug report or coding task.")],
        repo: Annotated[
            Path,
            typer.Option(
                "--repo",
                exists=True,
                file_okay=False,
                resolve_path=True,
                help="Repository that the harness may inspect.",
            ),
        ] = Path("."),
        thread_id: Annotated[
            str | None,
            typer.Option(help="Checkpoint thread identifier."),
        ] = None,
    ) -> None:
        """Run the current harness graph against a repository."""
        run_id = thread_id or str(uuid4())
        result = application.start_patch_run(
            issue=issue,
            repo_path=repo,
            run_id=run_id,
        )

        plan = "\n".join(f"{index}. {step}" for index, step in enumerate(result["plan"], 1))
        console.print(Panel.fit(plan, title="PatchCodeAgent plan", border_style="green"))
        console.print(f"[dim]thread_id:[/] {run_id}")
        console.print(f"[dim]python files inspected:[/] {len(result['inspected_files'])}")
        console.print(f"[dim]status:[/] {result['status']}")

    return cli


app = create_cli()


if __name__ == "__main__":
    app()
