from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel

from patch_code_agent.graph import build_graph

app = typer.Typer(no_args_is_help=True, help="Run the PatchCodeAgent coding-agent harness.")
console = Console()


@app.callback()
def main() -> None:
    """PatchCodeAgent command-line interface."""


@app.command()
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
    graph = build_graph()
    result = graph.invoke(
        {
            "issue": issue,
            "repo_path": str(repo),
            "status": "created",
        },
        config={"configurable": {"thread_id": run_id}},
    )

    plan = "\n".join(f"{index}. {step}" for index, step in enumerate(result["plan"], 1))
    console.print(Panel.fit(plan, title="PatchCodeAgent plan", border_style="green"))
    console.print(f"[dim]thread_id:[/] {run_id}")
    console.print(f"[dim]python files inspected:[/] {len(result['inspected_files'])}")
    console.print(f"[dim]status:[/] {result['status']}")


if __name__ == "__main__":
    app()
