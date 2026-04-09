from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from deep_researcher.config import load_config
from deep_researcher.harness import run_harness

app = typer.Typer(help="Adversarial C4 architecture harness for BMAD projects.")
console = Console()


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
) -> None:
    """Run the adversarial C4 architecture harness."""
    if not prd.exists():
        console.print(f"[red]Error:[/red] PRD file not found: {prd}")
        raise typer.Exit(1)

    cfg = load_config(config_file)
    if model_generator:
        cfg.generator.model = model_generator
    if model_critic:
        cfg.critic.model = model_critic

    asyncio.run(run_harness(prd=prd, output_dir=output, resume=resume, config=cfg))
