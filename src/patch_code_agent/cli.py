"""Expose Fixture Patch Run creation and read-only status through a Typer CLI."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4

import typer
from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel

from patch_code_agent.application import PatchCodeAgent, PatchRunStatus, PatchRunStatusReader
from patch_code_agent.candidate import CandidatePatchReference, load_candidate_patch
from patch_code_agent.diagnosis import DiagnosisArtifactReference, load_diagnosis_artifact
from patch_code_agent.gemini import SUPPORTED_GEMINI_MODEL_IDS, GeminiModelGateway
from patch_code_agent.inspection import InspectionTools
from patch_code_agent.model import (
    CandidateRequest,
    Diagnosis,
    DiagnosisRequest,
    ModelGateway,
    Plan,
    PlanningRequest,
    ScriptedModel,
)
from patch_code_agent.patching import CumulativeDiffReference
from patch_code_agent.planning import PlanArtifactReference, load_plan_artifact
from patch_code_agent.state import RunState


def _plan_lines(plan: Plan) -> tuple[tuple[str, str], ...]:
    """Return the one canonical CLI presentation of a typed Plan."""
    return (
        ("Issue", plan.issue_summary),
        ("Relevant Files", ", ".join(plan.relevant_files)),
        ("Repair", plan.repair_strategy),
        ("Verification", plan.verification_strategy),
    )


class _DeferredModelGateway:
    """Create an external Model Gateway only when the resumed graph requests model work."""

    def __init__(self, model_id: str, factory: Callable[[], ModelGateway]) -> None:
        self.model_id = model_id
        self._factory = factory
        self._gateway: ModelGateway | None = None

    def _get(self) -> ModelGateway:
        if self._gateway is None:
            self._gateway = self._factory()
        return self._gateway

    def create_plan(self, request: PlanningRequest, tools: InspectionTools) -> object:
        return self._get().create_plan(request, tools)

    def create_candidate(self, request: CandidateRequest, tools: InspectionTools) -> object:
        return self._get().create_candidate(request, tools)

    def create_diagnosis(self, request: DiagnosisRequest, tools: InspectionTools) -> object:
        return self._get().create_diagnosis(request, tools)


def create_cli(
    *,
    model_gateway: ModelGateway | None = None,
    data_root: Path | None = None,
    fixture_roots: tuple[Path, ...] | None = None,
    verification_timeout_seconds: float = 60.0,
    live_model_factory: Callable[[str, Path, str], ModelGateway] = (
        GeminiModelGateway.from_api_key
    ),
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

    def create_gemini_gateway(model_id: str) -> ModelGateway:
        """Validate explicit Gemini selection and load credentials at the provider seam."""
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key is None:
            api_key = dotenv_values(Path.cwd() / ".env").get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not configured")
        return live_model_factory(api_key, selected_data_root, model_id)

    def get_application(
        model_id: str | None = None,
        *,
        defer_model: bool = False,
    ) -> PatchCodeAgent:
        """Create the stateful application lazily for commands that need writes.

        A single CLI invocation reuses one application, but ``close_application`` resets the local
        reference so its SQLite connection cannot leak into another command execution.
        """
        nonlocal application
        if application is None:
            gateway = selected_model
            if model_id is not None:
                if model_id not in SUPPORTED_GEMINI_MODEL_IDS:
                    supported = ", ".join(SUPPORTED_GEMINI_MODEL_IDS)
                    raise ValueError(
                        f"Unsupported Gemini model: {model_id}; choose one of: {supported}"
                    )
                gateway = (
                    _DeferredModelGateway(
                        model_id,
                        lambda: create_gemini_gateway(model_id),
                    )
                    if defer_model
                    else create_gemini_gateway(model_id)
                )
            application = PatchCodeAgent(
                model_gateway=gateway,
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

    def get_resume_application(run_id: str) -> PatchCodeAgent:
        """Restore the Run's original model, deferring external setup until it is needed."""
        model_id = PatchRunStatusReader(selected_data_root).get(run_id).model_id
        if model_id == selected_model.model_id:
            return get_application()
        return get_application(model_id, defer_model=True)

    def print_candidate_patch(reference: CandidatePatchReference, diff: str) -> None:
        """Render the one canonical CLI view of an immutable Candidate Patch."""
        console.print("[bold yellow]Candidate Patch[/]")
        console.print(diff, markup=False, highlight=False, soft_wrap=True)
        console.print(f"[dim]Candidate Artifact:[/] {reference.path}")
        console.print(f"[dim]Candidate Checksum:[/] {reference.sha256}", soft_wrap=True)
        console.print(f"[dim]Candidate Diff:[/] {reference.diff_path}")
        console.print(f"[dim]Diff Checksum:[/] {reference.diff_sha256}", soft_wrap=True)

    def print_diagnosis(
        reference: DiagnosisArtifactReference,
        diagnosis: Diagnosis,
    ) -> None:
        """Render the one canonical CLI view of a failed-attempt Diagnosis."""
        console.print(f"[dim]Diagnosis Artifact:[/] {reference.path}")
        console.print(f"[dim]Failure:[/] {diagnosis.failure_summary}")
        console.print(f"[dim]Next Repair:[/] {diagnosis.next_strategy}")

    def outcome_label(status: str) -> str | None:
        """Translate terminal internal states into stable CLI outcome labels."""
        return {
            "issue_not_reproduced": "Issue Not Reproduced",
            "error": "Error",
            "rejected": "Rejected",
            "workspace_changed": "Workspace Changed",
            "attempts_exhausted": "Attempts Exhausted",
            "succeeded": "Succeeded",
        }.get(status)

    def print_repair_details(
        *,
        verification_outcome: object | None,
        error_kind: object | None,
    ) -> None:
        """Render the shared post-Approval details for command results and durable status."""
        if verification_outcome is not None:
            console.print(f"[dim]Verification:[/] {verification_outcome}")
        if error_kind is not None:
            console.print(f"[dim]Error Kind:[/] {error_kind}")

    def print_run_locations(
        *,
        status: str,
        run_id: str,
        files_changed: tuple[str, ...] | list[str],
        verification_artifact: str | None,
        cumulative_diff: CumulativeDiffReference | None,
        report_path: str | None,
    ) -> None:
        """Show newcomers where to inspect a completed Patch Run."""
        if outcome_label(status) is None:
            return

        run_root = (selected_data_root / run_id).resolve()
        if status == "succeeded":
            console.print(
                "[bold green]Patch Run succeeded: the patch was applied and "
                "Verification passed.[/]"
            )
        console.print("[bold]Files to review:[/]")
        console.print(f"[dim]Run Workspace:[/] {run_root / 'workspace'}", soft_wrap=True)
        for path in files_changed:
            console.print(
                f"[dim]Modified File:[/] {(run_root / 'workspace' / path).resolve()}",
                soft_wrap=True,
            )
        if verification_artifact is not None:
            console.print(
                f"[dim]Verification Log:[/] {(run_root / verification_artifact).resolve()}",
                soft_wrap=True,
            )
        if cumulative_diff is not None:
            console.print(
                f"[dim]Cumulative Diff:[/] {(run_root / cumulative_diff.path).resolve()}",
                soft_wrap=True,
            )
        if report_path is not None:
            console.print(
                f"[dim]Run Report:[/] {(run_root / report_path).resolve()}",
                soft_wrap=True,
            )

    def print_run_result(result: RunState, *, display_candidate: bool = True) -> None:
        """Render fields that exist on either planning or terminal graph branches.

        A failing baseline has a Plan Artifact; terminal baseline outcomes do not. Optional lookups
        keep one renderer valid for both state shapes while identity, baseline, counters, and raw
        status remain visible on every successful invocation.
        """
        if raw_reference := result.get("plan_artifact"):
            reference = PlanArtifactReference.model_validate(raw_reference)
            artifact = load_plan_artifact(selected_data_root, result["run_id"], reference)
            plan_text = "\n".join(
                f"{label}: {value}" for label, value in _plan_lines(artifact.plan)
            )
            console.print(Panel.fit(plan_text, title="PatchCodeAgent plan", border_style="green"))
            console.print(f"[dim]Plan Artifact:[/] {reference.path}")
            console.print(f"[dim]Plan Checksum:[/] {reference.sha256}", soft_wrap=True)
        if display_candidate and (raw_candidate_reference := result.get("candidate_artifact")):
            candidate_reference = CandidatePatchReference.model_validate(raw_candidate_reference)
            candidate = load_candidate_patch(
                selected_data_root,
                result["run_id"],
                candidate_reference,
            )
            print_candidate_patch(candidate_reference, candidate.diff)
        if raw_diagnosis_reference := result.get("diagnosis_artifact"):
            diagnosis_reference = DiagnosisArtifactReference.model_validate(raw_diagnosis_reference)
            diagnosis = load_diagnosis_artifact(
                selected_data_root,
                result["run_id"],
                diagnosis_reference,
            ).artifact.diagnosis
            print_diagnosis(diagnosis_reference, diagnosis)
        console.print(f"[dim]Run Identifier:[/] {result['run_id']}")
        console.print(f"[dim]Fixture Repository:[/] {result['source_id']}")
        console.print(f"[dim]Source Revision:[/] {result['source_revision']}", soft_wrap=True)
        console.print(f"[dim]Model:[/] {result['model_id']}")
        if baseline := result.get("baseline_verification"):
            console.print(f"[dim]Baseline Verification:[/] {baseline['outcome']}")
        else:
            console.print("[dim]Baseline Verification:[/] unavailable")
        if outcome := outcome_label(result["status"]):
            console.print(f"[dim]Outcome:[/] {outcome}")
        console.print(f"[dim]Model Requests:[/] {result['model_requests']}")
        console.print(f"[dim]Tool Executions:[/] {result.get('tool_executions', 0)}")
        console.print(f"[dim]Files Read:[/] {len(result.get('files_read', []))}")
        console.print(f"[dim]Repair Attempts:[/] {result.get('attempt', 0)}")
        console.print(f"[dim]Files Changed:[/] {len(result.get('files_changed', []))}")
        verification = result.get("verification")
        print_repair_details(
            verification_outcome=(verification.get("outcome") if verification else None),
            error_kind=result.get("error_kind"),
        )
        if reason := result.get("report", {}).get("note"):
            console.print(f"[dim]Reason:[/] {reason}")
        if report := result.get("report_artifact"):
            console.print(f"[dim]Run Report Checksum:[/] {report['sha256']}", soft_wrap=True)
        console.print(f"[dim]status:[/] {result['status']}")
        print_run_locations(
            status=result["status"],
            run_id=result["run_id"],
            files_changed=result.get("files_changed", []),
            verification_artifact=(
                str(verification["artifact_path"]) if verification is not None else None
            ),
            cumulative_diff=(
                CumulativeDiffReference.model_validate(raw_cumulative_diff)
                if (raw_cumulative_diff := result.get("cumulative_diff"))
                else None
            ),
            report_path=(str(report["path"]) if report else None),
        )

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
            console.print(f"[bold]{repository.source_id}[/]: {repository.issue_title}")

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
        console.print(f"[dim]Fixture Repository:[/] {patch_run.source_id}")
        console.print(
            f"[dim]Source Revision:[/] {patch_run.source_revision}",
            soft_wrap=True,
        )
        console.print(f"[dim]Model:[/] {patch_run.model_id}")
        console.print(f"[dim]Phase:[/] {patch_run.phase}")
        if outcome := outcome_label(patch_run.phase):
            console.print(f"[dim]Outcome:[/] {outcome}")
        console.print(f"[dim]Model Requests:[/] {patch_run.model_requests}")
        console.print(f"[dim]Tool Executions:[/] {patch_run.tool_executions}")
        console.print(f"[dim]Files Read:[/] {len(patch_run.files_read)}")
        console.print(f"[dim]Repair Attempts:[/] {patch_run.attempts}")
        console.print(f"[dim]Files Changed:[/] {len(patch_run.files_changed)}")
        print_repair_details(
            verification_outcome=(
                patch_run.verification.outcome if patch_run.verification is not None else None
            ),
            error_kind=patch_run.error_kind,
        )
        if patch_run.reason is not None:
            console.print(f"[dim]Reason:[/] {patch_run.reason}")
        if patch_run.report_artifact is not None:
            console.print(
                f"[dim]Run Report Checksum:[/] {patch_run.report_artifact.sha256}",
                soft_wrap=True,
            )
        if patch_run.plan is not None and patch_run.plan_artifact is not None:
            console.print(f"[dim]Plan Artifact:[/] {patch_run.plan_artifact.path}")
            console.print(
                f"[dim]Plan Checksum:[/] {patch_run.plan_artifact.sha256}",
                soft_wrap=True,
            )
            for label, value in _plan_lines(patch_run.plan):
                console.print(f"[dim]{label}:[/] {value}")
        if patch_run.diagnosis is not None and patch_run.diagnosis_artifact is not None:
            print_diagnosis(
                patch_run.diagnosis_artifact,
                patch_run.diagnosis.diagnosis,
            )
        if (
            patch_run.candidate is not None
            and patch_run.candidate_diff is not None
            and patch_run.candidate_artifact is not None
        ):
            print_candidate_patch(patch_run.candidate_artifact, patch_run.candidate_diff)
        print_run_locations(
            status=patch_run.phase,
            run_id=patch_run.run_id,
            files_changed=patch_run.files_changed,
            verification_artifact=(
                patch_run.verification.artifact_path
                if patch_run.verification is not None
                else None
            ),
            cumulative_diff=patch_run.cumulative_diff,
            report_path=(
                patch_run.report_artifact.path
                if patch_run.report_artifact is not None
                else None
            ),
        )

    @cli.command()
    def run(
        fixture_id: Annotated[
            str,
            typer.Argument(help="Registered Fixture Repository identifier."),
        ],
        model: Annotated[
            str | None,
            typer.Option(
                "--model",
                help="Send inspected Fixture contents to this Gemini model.",
            ),
        ] = None,
    ) -> None:
        """Start a Patch Run for a registered Fixture Repository."""
        run_id = str(uuid4())
        try:
            result = get_application(model).start_patch_run(fixture_id=fixture_id, run_id=run_id)
        except (RuntimeError, ValueError) as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from error
        finally:
            close_application()

        print_run_result(result)

    @cli.command()
    def reject(
        run_id: Annotated[
            str,
            typer.Argument(help="Run Identifier whose pending Candidate Patch should be rejected."),
        ],
    ) -> None:
        """Reject a pending Candidate Patch without modifying its Run Workspace."""
        try:
            result = get_application().reject_patch_run(run_id=run_id)
        except ValueError as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from error
        finally:
            close_application()
        print_run_result(result)

    @cli.command()
    def approve(
        run_id: Annotated[
            str,
            typer.Argument(help="Run Identifier whose pending Candidate Patch should be approved."),
        ],
        yes: Annotated[
            bool,
            typer.Option("--yes", help="Approve without the interactive confirmation prompt."),
        ] = False,
    ) -> None:
        """Approve one exact Candidate Patch, apply it, and execute Verification."""

        def confirm_candidate(patch_run: PatchRunStatus) -> bool:
            if patch_run.candidate_artifact is None or patch_run.candidate_diff is None:
                raise ValueError("Pending Patch Run has no Candidate Patch Artifact")
            print_candidate_patch(patch_run.candidate_artifact, patch_run.candidate_diff)
            if yes:
                return True
            return typer.confirm("Approve this exact Candidate Patch?", default=False)

        try:
            result = get_resume_application(run_id).approve_patch_run(
                run_id=run_id,
                confirm=confirm_candidate,
            )
        except (ValueError, RuntimeError) as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from error
        finally:
            close_application()
        if result is None:
            console.print("[yellow]Approval cancelled; Patch Run remains pending.[/]")
            return
        print_run_result(
            result,
            display_candidate=result["status"] == "pending_approval",
        )

    return cli


app = create_cli()


if __name__ == "__main__":
    app()
