from __future__ import annotations

import json
import os
from pathlib import Path

from deep_researcher.models.contract import SprintContract
from deep_researcher.models.feedback import CriticResult
from deep_researcher.models.progress import HarnessProgress


def init_workspace(output_dir: Path) -> None:
    """Create the architecture output directory and harness artifact subdirs."""
    for subdir in ["contracts", "feedback", "decisions", "logs"]:
        (output_dir / subdir).mkdir(parents=True, exist_ok=True)


def save_contract(output_dir: Path, contract: SprintContract) -> Path:
    path = output_dir / "contracts" / f"sprint-{contract.sprint_number}.json"
    path.write_text(contract.model_dump_json(indent=2))
    return path


def load_contract(output_dir: Path, sprint_number: int) -> SprintContract:
    path = output_dir / "contracts" / f"sprint-{sprint_number}.json"
    return SprintContract.model_validate_json(path.read_text())


def save_feedback(
    output_dir: Path, sprint_number: int, round_num: int, result: CriticResult
) -> Path:
    path = output_dir / "feedback" / f"sprint-{sprint_number}-round-{round_num}.json"
    path.write_text(result.model_dump_json(indent=2))
    return path


def load_feedback(output_dir: Path, sprint_number: int, round_num: int) -> CriticResult:
    path = output_dir / "feedback" / f"sprint-{sprint_number}-round-{round_num}.json"
    return CriticResult.model_validate_json(path.read_text())


def save_progress(checkpoint_dir: Path, progress: HarnessProgress) -> Path:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = checkpoint_dir / "progress.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(progress.model_dump_json(indent=2))
    os.replace(tmp, path)
    return path


def load_progress(checkpoint_dir: Path) -> HarnessProgress:
    path = checkpoint_dir / "progress.json"
    return HarnessProgress.model_validate_json(path.read_text())


def save_round_log(
    output_dir: Path, sprint_number: int, round_num: int, data: dict[str, object]
) -> None:
    """Write structured round log for reproducibility."""
    path = output_dir / "feedback" / f"sprint-{sprint_number}-round-{round_num}-log.json"
    path.write_text(json.dumps(data, indent=2, default=str))


def clean_run_artifacts(output_dir: Path, checkpoint_dir: Path) -> list[Path]:
    """Delete prior run artifacts to allow a clean restart.

    Removes all files in the checkpoint directory and the contracts and feedback
    subdirectories of output_dir.  Leaves logs/ and decisions/ intact so run
    history and any manual ADR content are preserved.

    Returns the list of deleted paths.
    """
    deleted: list[Path] = []
    for directory in (checkpoint_dir, output_dir / "contracts", output_dir / "feedback"):
        if directory.is_dir():
            for f in directory.iterdir():
                if f.is_file():
                    f.unlink()
                    deleted.append(f)
    learnings = output_dir / "generator-learnings.md"
    if learnings.exists():
        learnings.unlink()
        deleted.append(learnings)
    return deleted
