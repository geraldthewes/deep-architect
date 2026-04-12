from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import typer
from rich.console import Console

from deep_architect.config import load_config
from deep_architect.git_ops import validate_git_repo
from deep_architect.harness import run_harness
from deep_architect.io.files import clean_run_artifacts, load_progress, reset_sprint_artifacts

app = typer.Typer(help="Adversarial C4 architecture harness for BMAD projects.")
console = Console()


def _find_checkpoint(output_dir: Path) -> Path | None:
    """Return the checkpoint path if it exists for the repo containing output_dir."""
    try:
        repo = validate_git_repo(output_dir)
        if repo.working_tree_dir is None:
            return None
        checkpoint = Path(repo.working_tree_dir) / ".checkpoints" / "progress.json"
        return checkpoint if checkpoint.exists() else None
    except SystemExit:
        return None


@app.command()
def main(
    prd: Path = typer.Option(..., help="Path to the PRD Markdown file"),
    output: Path = typer.Option(..., help="Output directory for architecture files"),
    resume: bool = typer.Option(False, "--resume", help="Resume an interrupted run"),
    config_file: Path | None = typer.Option(
        None, "--config", help="Config file path (default: ~/.deep-researcher.toml)"
    ),
    model_generator: str | None = typer.Option(
        None, "--model-generator", help="Override generator model name"
    ),
    model_critic: str | None = typer.Option(
        None, "--model-critic", help="Override critic model name"
    ),
    context: list[Path] = typer.Option(
        [], "--context", help="Supplementary context files (repeatable)"
    ),
    reset_sprint: int | None = typer.Option(
        None,
        "--reset-sprint",
        help=(
            "Reset sprint N to initial state and resume from it. "
            "Deletes sprint N's contract, feedback files, and history entries. "
            "Sprints above N keep their current status."
        ),
        min=1,
    ),
) -> None:
    """Run the adversarial C4 architecture harness."""
    if not prd.exists():
        console.print(f"[red]Error:[/red] PRD file not found: {prd}")
        raise typer.Exit(1)

    for ctx_path in context:
        if not ctx_path.exists():
            console.print(f"[red]Error:[/red] Context file not found: {ctx_path}")
            raise typer.Exit(1)

    cfg = load_config(config_file)
    if model_generator:
        cfg.generator.model = model_generator
    if model_critic:
        cfg.critic.model = model_critic

    if reset_sprint is not None:
        checkpoint = _find_checkpoint(output)
        if checkpoint is None:
            console.print("[red]Error:[/red] No checkpoint found — start a fresh run first.")
            raise typer.Exit(1)
        repo = validate_git_repo(output)
        checkpoint_dir = Path(repo.working_tree_dir) / ".checkpoints"  # type: ignore[arg-type]
        try:
            _, affected = reset_sprint_artifacts(output, checkpoint_dir, reset_sprint)
        except ValueError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1)
        console.print(
            f"[green]Sprint {reset_sprint} reset.[/green] "
            f"{len(affected)} artifact(s) removed/updated."
        )
        resume = True  # implicitly resume from the reset sprint

    # If no --resume flag and a prior checkpoint exists, ask the user what to do.
    if not resume:
        checkpoint = _find_checkpoint(output)
        if checkpoint is not None:
            try:
                prior = load_progress(checkpoint.parent)
                console.print(
                    f"\n[yellow]Prior run detected:[/yellow] "
                    f"sprint {prior.current_sprint}/{prior.total_sprints}, "
                    f"{prior.completed_sprints} sprint(s) completed, "
                    f"{prior.total_rounds} round(s) run, "
                    f"status=[bold]{prior.status}[/bold]\n"
                )
            except Exception:
                console.print(
                    "\n[yellow]Prior run detected[/yellow] (checkpoint unreadable)\n"
                )

            if typer.confirm("Continue from where it left off?", default=True):
                resume = True
            else:
                if not typer.confirm(
                    "This will delete prior checkpoints, contracts, and feedback. Start fresh?",
                    default=False,
                ):
                    raise typer.Exit(0)
                repo = validate_git_repo(output)
                checkpoint_dir = Path(repo.working_tree_dir) / ".checkpoints"  # type: ignore[arg-type]
                deleted = clean_run_artifacts(output, checkpoint_dir)
                console.print(f"[green]Cleaned up {len(deleted)} file(s).[/green]\n")

    def _sigterm_to_sigint(signum: int, frame: object) -> None:
        """Redirect SIGTERM to SIGINT so asyncio.run() handles it cleanly."""
        os.kill(os.getpid(), signal.SIGINT)

    signal.signal(signal.SIGTERM, _sigterm_to_sigint)

    try:
        asyncio.run(
            run_harness(
                prd=prd, output_dir=output, resume=resume, config=cfg, context_files=context
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — run with --resume to continue.[/yellow]")
        raise typer.Exit(0)
