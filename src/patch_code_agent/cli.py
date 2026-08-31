"""Expose Patch Run creation and read-only status through a Typer CLI.

Commands translate terminal/domain failures into stable exit code 2 messages and always close the
application writer after use. ``status`` intentionally takes the separate read-only path, while
``run-local`` requires an explicit trust acknowledgement before repository code can execute.
"""

from pathlib import Path
from typing import Annotated, assert_never, cast
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel

from patch_code_agent.application import PatchCodeAgent, PatchRunStatusReader
from patch_code_agent.candidate import CandidatePatchReference, load_candidate_patch
from patch_code_agent.model import ModelGateway, Plan, ScriptedModel
from patch_code_agent.planning import PlanArtifactReference, load_plan_artifact
from patch_code_agent.sources import RepositorySourceKind
from patch_code_agent.state import RunState


def _plan_lines(plan: Plan) -> tuple[tuple[str, str], ...]:
    """Return the one canonical CLI presentation of a typed Plan."""
    return (
        ("Issue", plan.issue_summary),
        ("Relevant Files", ", ".join(plan.relevant_files)),
        ("Repair", plan.repair_strategy),
        ("Verification", plan.verification_strategy),
    )


def create_cli(
    *,
    model_gateway: ModelGateway | None = None,
    data_root: Path | None = None,
    fixture_roots: tuple[Path, ...] | None = None,
    verification_timeout_seconds: float = 60.0,
) -> typer.Typer:
    """Create the CLI with all application dependencies at one seam.

    Optional arguments are dependency-injection hooks used by acceptance tests: they replace the
    model, data root, fixture roots, or timeout while still exercising the public Typer interface,
    real filesystem, SQLite, and subprocess implementations.
    """
    cli = typer.Typer(no_args_is_help=True, help="Run the PatchCodeAgent coding-agent harness.")
    console = Console()
    selected_model = model_gateway if model_gateway is not None else ScriptedModel()
    selected_data_root = (
        data_root if data_root is not None else Path.home() / ".patch-code-agent" / "runs"
    )
    application: PatchCodeAgent | None = None

    def get_application() -> PatchCodeAgent:
        """Create the stateful application lazily for commands that need writes.

        A single CLI invocation reuses one application, but ``close_application`` resets the local
        reference so its SQLite connection cannot leak into another command execution.
        """
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
        """Flush SQLite resources after a command finishes or raises."""
        nonlocal application
        if application is not None:
            application.close()
            application = None

    def source_label(source_kind: RepositorySourceKind) -> str:
        """Render the closed Repository Source kind set for humans."""
        match source_kind:
            case "fixture":
                return "Fixture Repository"
            case "trusted":
                return "Trusted Repository"
        assert_never(source_kind)

    def print_candidate_patch(reference: CandidatePatchReference, diff: str) -> None:
        """Render the one canonical CLI view of an immutable Candidate Patch."""
        console.print("[bold yellow]Candidate Patch[/]")
        console.print(diff, markup=False, highlight=False, soft_wrap=True)
        console.print(f"[dim]Candidate Artifact:[/] {reference.path}")
        console.print(f"[dim]Candidate Checksum:[/] {reference.sha256}", soft_wrap=True)
        console.print(f"[dim]Candidate Diff:[/] {reference.diff_path}")
        console.print(f"[dim]Diff Checksum:[/] {reference.diff_sha256}", soft_wrap=True)

    def outcome_label(status: str) -> str | None:
        """Translate terminal internal states into stable CLI outcome labels."""
        return {
            "issue_not_reproduced": "Issue Not Reproduced",
            "budget_exceeded": "Budget Exceeded",
            "error": "Error",
        }.get(status)

    def print_run_result(result: RunState) -> None:
        """Render fields that exist on either planning or terminal graph branches.

        A failing baseline has a Plan Artifact; terminal baseline outcomes do not. Optional lookups
        keep one renderer valid for both state shapes while identity, baseline, counters, and raw
        status remain visible on every successful invocation.
        """
        if raw_reference := result.get("plan_artifact"):
            reference = PlanArtifactReference.model_validate(raw_reference)
            artifact = load_plan_artifact(selected_data_root, result["run_id"], reference)
            plan_text = "\n".join(f"{label}: {value}" for label, value in _plan_lines(artifact.plan))
            console.print(Panel.fit(plan_text, title="PatchCodeAgent plan", border_style="green"))
            console.print(f"[dim]Plan Artifact:[/] {reference.path}")
            console.print(f"[dim]Plan Checksum:[/] {reference.sha256}", soft_wrap=True)
        if raw_candidate_reference := result.get("candidate_artifact"):
            candidate_reference = CandidatePatchReference.model_validate(raw_candidate_reference)
            candidate = load_candidate_patch(
                selected_data_root,
                result["run_id"],
                candidate_reference,
            )
            print_candidate_patch(candidate_reference, candidate.diff)
        console.print(f"[dim]Run Identifier:[/] {result['run_id']}")
        console.print(
            f"[dim]{source_label(result['source_kind'])}:[/] {result['source_id']}"
        )
        console.print(f"[dim]Source Revision:[/] {result['source_revision']}", soft_wrap=True)
        baseline = result["baseline_verification"]
        console.print(f"[dim]Baseline Verification:[/] {baseline['outcome']}")
        if outcome := outcome_label(result["status"]):
            console.print(f"[dim]Outcome:[/] {outcome}")
        console.print(f"[dim]Model Requests:[/] {result['model_requests']}")
        console.print(f"[dim]Tool Executions:[/] {result.get('tool_executions', 0)}")
        console.print(f"[dim]Files Read:[/] {len(result.get('files_read', []))}")
        console.print(f"[dim]Repair Attempts:[/] {result.get('attempt', 0)}")
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
        console.print(f"[dim]Tool Executions:[/] {patch_run.tool_executions}")
        console.print(f"[dim]Files Read:[/] {len(patch_run.files_read)}")
        console.print(f"[dim]Repair Attempts:[/] {patch_run.attempts}")
        if patch_run.plan is not None and patch_run.plan_artifact is not None:
            console.print(f"[dim]Plan Artifact:[/] {patch_run.plan_artifact.path}")
            console.print(
                f"[dim]Plan Checksum:[/] {patch_run.plan_artifact.sha256}",
                soft_wrap=True,
            )
            for label, value in _plan_lines(patch_run.plan):
                console.print(f"[dim]{label}:[/] {value}")
        if (
            patch_run.candidate is not None
            and patch_run.candidate_diff is not None
            and patch_run.candidate_artifact is not None
        ):
            print_candidate_patch(patch_run.candidate_artifact, patch_run.candidate_diff)


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
