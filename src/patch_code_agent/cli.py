from pathlib import Path
from typing import Annotated, assert_never, cast
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel

from patch_code_agent.application import PatchCodeAgent, PatchRunStatusReader
from patch_code_agent.model import ModelGateway, ScriptedModel
from patch_code_agent.sources import RepositorySourceKind
from patch_code_agent.state import RunState


def create_cli(
    *,
    model_gateway: ModelGateway | None = None,
    data_root: Path | None = None,
    fixture_roots: tuple[Path, ...] | None = None,
    verification_timeout_seconds: float = 60.0,
) -> typer.Typer:
    """Create the CLI with all application dependencies at one seam."""
    cli = typer.Typer(no_args_is_help=True, help="Run the PatchCodeAgent coding-agent harness.")
    console = Console()
    selected_model = model_gateway if model_gateway is not None else ScriptedModel()
    selected_data_root = (
        data_root if data_root is not None else Path.home() / ".patch-code-agent" / "runs"
    )
    application: PatchCodeAgent | None = None

    def get_application() -> PatchCodeAgent:
        nonlocal application
        if application is None:
            application = PatchCodeAgent(
                model_gateway=selected_model,
                data_root=selected_data_root,
                fixture_roots=fixture_roots,
                verification_timeout_seconds=verification_timeout_seconds,
            )
        return cast(PatchCodeAgent, application)

    def close_application() -> None:
        nonlocal application
        if application is not None:
            application.close()
            application = None

    def source_label(source_kind: RepositorySourceKind) -> str:
        match source_kind:
            case "fixture":
                return "Fixture Repository"
            case "trusted":
                return "Trusted Repository"
        assert_never(source_kind)

    def outcome_label(status: str) -> str | None:
        return {
            "issue_not_reproduced": "Issue Not Reproduced",
            "budget_exceeded": "Budget Exceeded",
            "error": "Error",
        }.get(status)

    def print_run_result(result: RunState) -> None:
        if plan_steps := result.get("plan"):
            plan = "\n".join(f"{index}. {step}" for index, step in enumerate(plan_steps, 1))
            console.print(Panel.fit(plan, title="PatchCodeAgent plan", border_style="green"))
        console.print(f"[dim]Run Identifier:[/] {result['run_id']}")
        console.print(
            f"[dim]{source_label(result['source_kind'])}:[/] {result['source_id']}"
        )
        console.print(f"[dim]Source Revision:[/] {result['source_revision']}", soft_wrap=True)
        baseline = result["baseline_verification"]
        console.print(f"[dim]Baseline Verification:[/] {baseline['outcome']}")
        if inspected_files := result.get("inspected_files"):
            console.print(f"[dim]python files inspected:[/] {len(inspected_files)}")
        if outcome := outcome_label(result["status"]):
            console.print(f"[dim]Outcome:[/] {outcome}")
        console.print(f"[dim]Model Requests:[/] {result['model_requests']}")
        console.print(f"[dim]status:[/] {result['status']}")

    @cli.callback()
    def main() -> None:
        """PatchCodeAgent command-line interface."""


    @cli.command()
    def fixtures() -> None:
        """List registered Fixture Repositories."""
        try:
            repositories = get_application().list_fixture_repositories()
        except ValueError as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from error
        finally:
            close_application()
        for repository in repositories:
            console.print(f"[bold]{repository.manifest.fixture_id}[/]: {repository.issue_title}")


    @cli.command()
    def status(
        run_id: Annotated[str, typer.Argument(help="Run Identifier to inspect.")],
    ) -> None:
        """Show persisted Patch Run status without advancing it."""
        try:
            patch_run = PatchRunStatusReader(selected_data_root).get(run_id)
        except ValueError as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from error
        console.print(f"[dim]Run Identifier:[/] {patch_run.run_id}")
        console.print(
            f"[dim]{source_label(patch_run.source_kind)}:[/] {patch_run.source_id}"
        )
        console.print(
            f"[dim]Source Revision:[/] {patch_run.source_revision}",
            soft_wrap=True,
        )
        console.print(f"[dim]Phase:[/] {patch_run.phase}")
        console.print(f"[dim]Model Requests:[/] {patch_run.model_requests}")


    @cli.command()
    def run(
        fixture_id: Annotated[
            str,
            typer.Argument(help="Registered Fixture Repository identifier."),
        ],
    ) -> None:
        """Start a Patch Run for a registered Fixture Repository."""
        run_id = str(uuid4())
        try:
            result = get_application().start_patch_run(fixture_id=fixture_id, run_id=run_id)
        except ValueError as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from error
        finally:
            close_application()

        print_run_result(result)


    @cli.command(name="run-local")
    def run_local(
        repository: Annotated[
            Path,
            typer.Argument(
                exists=True,
                file_okay=False,
                help="Explicitly selected local Trusted Repository.",
            ),
        ],
        contract: Annotated[
            Path,
            typer.Option(
                "--contract",
                exists=True,
                dir_okay=False,
                help="TOML Patch Run Contract outside the repository.",
            ),
        ],
        trust_repository: Annotated[
            bool,
            typer.Option(
                "--trust-repository",
                help="Acknowledge that repository Verification executes with host authority.",
            ),
        ] = False,
    ) -> None:
        """Start a Patch Run from an explicitly trusted local repository."""
        if not trust_repository:
            console.print(
                "[red]Trusted Repository requires explicit --trust-repository acknowledgement[/]"
            )
            raise typer.Exit(code=2)

        run_id = str(uuid4())
        try:
            result = get_application().start_trusted_patch_run(
                repository=repository,
                contract_path=contract,
                run_id=run_id,
            )
        except ValueError as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from error
        finally:
            close_application()

        print_run_result(result)

    return cli


app = create_cli()


if __name__ == "__main__":
    app()
