from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult
from deep_architect.models.progress import HarnessProgress


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


def append_generator_history(
    output_dir: Path,
    sprint_num: int,
    round_num: int,
    previous_feedback: CriticResult | None,
    modified_files: list[Path],
    input_tokens: int,
) -> None:
    """Append a structured generator round entry to generator-history.md.

    Entries are grep-searchable by sprint number, round number, or filename.
    The file is never auto-loaded into context — agents search it via Read/Grep.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if previous_feedback is None:
        feedback_summary = "First round — no prior feedback"
    else:
        feedback_summary = (
            f"{len(previous_feedback.feedback)} concern(s) from prior critic round"
            f" (avg {previous_feedback.average_score:.1f}/10)"
        )
    files_str = ", ".join(f.name for f in modified_files) if modified_files else "None"
    entry = (
        f"\n## Sprint {sprint_num} · Round {round_num} — {timestamp}\n"
        f"**Feedback addressed**: {feedback_summary}\n"
        f"**Files modified**: {files_str}\n"
        f"**Token usage**: {input_tokens:,}\n"
        f"---\n"
    )
    with (output_dir / "generator-history.md").open("a") as fh:
        fh.write(entry)


def append_critic_history(
    output_dir: Path,
    sprint_num: int,
    round_num: int,
    result: CriticResult,
) -> None:
    """Append a structured critic round entry to critic-history.md.

    Entries are grep-searchable by sprint number, round number, severity, or criterion keyword.
    The file is never auto-loaded into context — agents search it via Read/Grep.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    passed_str = "Yes" if result.passed else "No"
    concerns = "\n".join(
        f"- [{f.severity}] {f.criterion} ({f.score:.1f}/10): {f.details[:200]}"
        for f in result.feedback
    )
    entry = (
        f"\n## Sprint {sprint_num} · Round {round_num} — {timestamp}\n"
        f"**Score**: {result.average_score:.1f}/10  **Passed**: {passed_str}\n"
        f"**Concerns**:\n{concerns}\n"
        f"**Summary**: {result.overall_summary[:300]}\n"
        f"---\n"
    )
    with (output_dir / "critic-history.md").open("a") as fh:
        fh.write(entry)


def append_rollback_event(
    output_dir: Path,
    sprint_num: int,
    round_num: int,
    regressed_score: float,
    best_score: float,
    best_commit_sha: str,
) -> None:
    """Append a [ROLLBACK]-prefixed entry to generator-history.md."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"\n## [ROLLBACK] Sprint {sprint_num} · Round {round_num} — {timestamp}\n"
        f"**Action**: Architecture files reverted to best-scoring commit\n"
        f"**Reason**: Score regressed {regressed_score:.1f}/10 vs best {best_score:.1f}/10\n"
        f"**Reverted to commit**: `{best_commit_sha[:12]}`\n"
        f"**What this means**: The files you see now reflect the best architecture so far.\n"
        f"Your next round should build on this baseline — do NOT reintroduce the changes\n"
        f"that caused the regression. Check critic-history.md for what the critic flagged.\n"
        f"---\n"
    )
    with (output_dir / "generator-history.md").open("a") as fh:
        fh.write(entry)


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


def reset_sprint_artifacts(
    output_dir: Path,
    checkpoint_dir: Path,
    sprint_number: int,
) -> tuple[HarnessProgress, list[Path]]:
    """Reset a single sprint to pending, wiping its contract, feedback, and history entries.

    Leaves sprints above and below sprint_number untouched.
    Returns (updated_progress, list_of_affected_paths).
    """
    progress = load_progress(checkpoint_dir)

    total = progress.total_sprints
    if not 1 <= sprint_number <= total:
        raise ValueError(f"Sprint {sprint_number} is out of range [1, {total}]")

    sprint_status = progress.sprint_statuses[sprint_number - 1]
    was_passed = sprint_status.status == "passed"

    # Reset sprint status fields
    sprint_status.status = "pending"
    sprint_status.rounds_completed = 0
    sprint_status.consecutive_passes = 0
    sprint_status.final_score = None

    # Walk current_sprint back if needed
    if progress.current_sprint >= sprint_number:
        progress.current_sprint = sprint_number

    # Undo completed_sprints credit if the sprint was previously passed
    if was_passed and progress.completed_sprints > 0:
        progress.completed_sprints -= 1

    # Un-terminate the harness so --resume can continue
    if progress.status in ("complete", "failed"):
        progress.status = "running"

    save_progress(checkpoint_dir, progress)

    affected: list[Path] = []

    # Delete contract
    contract_path = output_dir / "contracts" / f"sprint-{sprint_number}.json"
    if contract_path.exists():
        contract_path.unlink()
        affected.append(contract_path)

    # Delete all feedback files for this sprint
    feedback_dir = output_dir / "feedback"
    if feedback_dir.is_dir():
        for f in feedback_dir.glob(f"sprint-{sprint_number}-round-*"):
            f.unlink()
            affected.append(f)

    # Strip sprint N entries from history files (each entry ends with ---\n)
    pattern = re.compile(rf"\n## Sprint {sprint_number} ·[^#]*?---\n", re.DOTALL)
    for hist_name in ("generator-history.md", "critic-history.md"):
        hist_path = output_dir / hist_name
        if hist_path.exists():
            original = hist_path.read_text()
            stripped = pattern.sub("", original)
            if stripped != original:
                hist_path.write_text(stripped)
                affected.append(hist_path)

    return progress, affected


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
    for filename in ("generator-learnings.md", "generator-history.md", "critic-history.md"):
        artifact = output_dir / filename
        if artifact.exists():
            artifact.unlink()
            deleted.append(artifact)
    return deleted
