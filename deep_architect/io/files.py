from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult
from deep_architect.models.progress import HarnessProgress, SprintStatus
from deep_architect.sprints import SprintDefinition

_HARNESS_ARTIFACT_FILES = frozenset({
    "generator-history.md",
    "generator-learnings.md",
    "critic-history.md",
    "INDEX.md",
})

_ARTIFACT_SUBDIRS = frozenset({"contracts", "feedback", "logs"})


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
    output_dir: Path,
    sprint_number: int,
    round_num: int,
    result: CriticResult,
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
    sprint_status.best_round = None
    sprint_status.best_scores = None

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


def write_index(
    output_dir: Path,
    sprints: list[SprintDefinition],
    progress: HarnessProgress,
) -> Path:
    """Write INDEX.md to output_dir linking all produced architecture files.

    Files are grouped by sprint with the critic score and pass/fail status.
    Harness artifacts (generator-history.md, feedback JSON, etc.) are excluded.
    Any .md files not attributable to a specific sprint appear under
    'Additional Files'.
    """
    # Collect all architecture .md files actually on disk
    all_files: set[Path] = set()
    for p in output_dir.rglob("*.md"):
        rel = p.relative_to(output_dir)
        if rel.parts[0] in _ARTIFACT_SUBDIRS:
            continue
        if p.name in _HARNESS_ARTIFACT_FILES:
            continue
        all_files.add(p)

    assigned: set[Path] = set()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = [
        "# Architecture Index",
        "",
        f"Generated by deep-architect · {timestamp}",
        "",
        "## Sprints",
        "",
    ]

    for sprint in sprints:
        ss = progress.sprint_statuses[sprint.number - 1]
        score_str = f"{ss.final_score:.1f}/10" if ss.final_score is not None else "—"
        status_icon = "✓" if ss.status in ("passed", "accepted") else "✗"
        lines.append(
            f"### Sprint {sprint.number} · {sprint.name} — {score_str} {status_icon}"
        )
        lines.append("")

        sprint_files: list[Path] = []
        domain_dirs: set[Path] = set()

        for entry in sprint.primary_files:
            if entry.endswith("/"):
                # Directory primary — list everything inside it
                domain = output_dir / entry.rstrip("/")
                domain_dirs.add(Path(entry.rstrip("/")))
                if domain.is_dir():
                    for f in sorted(domain.rglob("*.md")):
                        if f not in assigned:
                            sprint_files.append(f)
                            assigned.add(f)
            else:
                f = output_dir / entry
                domain_dirs.add(Path(entry).parent)
                if f in all_files and f not in assigned:
                    sprint_files.append(f)
                    assigned.add(f)

        # Extra files in the sprint's domain directories (allow_extra_files sprints)
        if sprint.allow_extra_files:
            for f in sorted(all_files - assigned):
                rel = f.relative_to(output_dir)
                for d in domain_dirs:
                    if d != Path(".") and rel.is_relative_to(d):
                        sprint_files.append(f)
                        assigned.add(f)
                        break

        for f in sorted(sprint_files, key=lambda p: p.relative_to(output_dir)):
            rel = f.relative_to(output_dir)
            lines.append(f"- [{rel}]({rel})")

        lines.append("")

    remaining = sorted(all_files - assigned, key=lambda p: p.relative_to(output_dir))
    if remaining:
        lines.extend(["## Additional Files", ""])
        for f in remaining:
            rel = f.relative_to(output_dir)
            lines.append(f"- [{rel}]({rel})")
        lines.append("")

    path = output_dir / "INDEX.md"
    path.write_text("\n".join(lines))
    return path


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


def generate_sprint_documentation(
    output_dir: Path,
    sprint_number: int,
    sprint_name: str,
    progress: HarnessProgress,
    sprint_status: SprintStatus,
    generator_history: str = "",
    critic_history: str = ""
) -> Path:
    """Generate sprint documentation by filling template with actual sprint data.

    Args:
        output_dir: Directory to save the completed sprint document
        sprint_number: Sprint number (1-7)
        sprint_name: Name of the sprint
        progress: Overall harness progress
        sprint_status: Status of this specific sprint
        generator_history: Content from generator-history.md (optional)
        critic_history: Content from critic-history.md (optional)

    Returns:
        Path to the generated sprint documentation file
    """
    # Read the appropriate sprint template
    sprint_name_formatted = sprint_name.lower().replace(' ', '-').replace('/', '-')
    template_path = Path(
        f"/home/gerald/repos/deep-architect/knowledge/architecture/sprints/"
        f"sprint-{sprint_number:02d}-{sprint_name_formatted}.md"
    )

    if not template_path.exists():
        # Fallback to a basic template if specific one not found
        template_content = f"""# Sprint {sprint_number}: {sprint_name}

## Overview
[To be filled during sprint execution]

## Agreements
[To be filled during sprint execution]

## Strengths
[To be filled during sprint execution]

## Concerns
[To be filled during sprint execution]

## Unresolved Critic Concerns
[To be filled during sprint execution - specific criteria where Critic remained concerned]

### Exit Status
- [ ] Completed via quality criteria 
  (avg score ≥ 9.0/10, zero Critical/High for 2 consecutive rounds)
- [ ] Completed via max rounds fallback (with warnings)
- [ ] Failed to produce usable output

### Notes on Exit Mechanism
[Documentation of what output was produced and why the sprint exited the way it did]
"""
    else:
        template_content = template_path.read_text()

    # Extract sprint-specific data from history and progress
    agreements = _extract_agreements(generator_history, critic_history)
    strengths = _extract_strengths(generator_history, critic_history)
    concerns = _extract_concerns(generator_history, critic_history)
    unresolved_concerns = _extract_unresolved_concerns(critic_history)

    # Determine exit status based on sprint_status
    exit_quality = (
        sprint_status.status == "passed" 
        and sprint_status.final_score is not None 
        and sprint_status.final_score >= 9.0
    )
    exit_max_rounds = sprint_status.status == "accepted"
    exit_failed = sprint_status.status == "failed"

    # Format exit status checkboxes
    exit_status_lines = []
    
    if exit_quality:
        quality_criteria = "- [x] Completed via quality criteria "
        quality_criteria += "(avg score ≥ 9.0/10, zero Critical/High for 2 consecutive rounds)"
    else:
        quality_criteria = "- [ ] Completed via quality criteria "
        quality_criteria += "(avg score ≥ 9.0/10, zero Critical/High for 2 consecutive rounds)"
         
    if exit_max_rounds:
        max_rounds = "- [x] Completed via max rounds fallback (with warnings)"
    else:
        max_rounds = "- [ ] Completed via max rounds fallback (with warnings)"
        
    if exit_failed:
        failed_output = "- [x] Failed to produce usable output"
    else:
        failed_output = "- [ ] Failed to produce usable output"
        
    exit_status_lines.append(quality_criteria)
    exit_status_lines.append(max_rounds)
    exit_status_lines.append(failed_output)

    # Generate notes on exit mechanism
    exit_notes = _generate_exit_notes(sprint_status, progress)

    # Replace placeholders in template
    placeholder1 = "[To be filled during sprint execution]"
    placeholder2 = (
        "[To be filled during sprint execution - specific criteria where Critic remained concerned]"
    )
    
    filled_content = template_content.replace(placeholder1, agreements, 1)
    filled_content = filled_content.replace(placeholder1, strengths, 1)
    filled_content = filled_content.replace(placeholder1, concerns, 1)
    filled_content = filled_content.replace(placeholder2, unresolved_concerns, 1)
    exit_status_text = (
        "- [ ] Completed via quality criteria \n"
        "  (avg score ≥ 9.0/10, zero Critical/High for 2 consecutive rounds)\n"
        "- [ ] Completed via max rounds fallback (with warnings)\n"
        "- [ ] Failed to produce usable output"
    )
    filled_content = filled_content.replace(
        exit_status_text,
        "\n".join(exit_status_lines),
        1
    )
    filled_content = filled_content.replace(
        "[Documentation of what output was produced and why the sprint exited the way it did]",
        exit_notes,
        1
    )

    # Save the completed documentation
    doc_path = output_dir / f"sprint-{sprint_number:02d}-documentation.md"
    doc_path.write_text(filled_content)

    return doc_path


def _extract_agreements(generator_history: str, critic_history: str) -> str:
    """Extract agreements from history files."""
    # Simple implementation - in practice would parse history for consensus points
    if not generator_history and not critic_history:
        return "Agreements extracted from Generator and Critic interaction history"

    # Look for agreement indicators in history
    agreements = []
    # Check for explicit agreement (not disagreement)
    gen_lower = generator_history.lower()
    crit_lower = critic_history.lower()
    if (" agree " in gen_lower or gen_lower.startswith("agree ") or 
        gen_lower.endswith(" agree") or " agreed " in gen_lower or
        " agree" in gen_lower.replace("disagree", "")) or \
       (" agree " in crit_lower or crit_lower.startswith("agree ") or 
        crit_lower.endswith(" agree") or " agreed " in crit_lower or
        " agree" in crit_lower.replace("disagree", "")):
        agreements.append("- Consensus reached on core architectural decisions")
    if "approved" in critic_history.lower():
        agreements.append("- Generator proposals approved by Critic")

    if agreements:
        return "\n".join(agreements)
    else:
        return "- Agreements to be extracted from sprint history"


def _extract_strengths(generator_history: str, critic_history: str) -> str:
    """Extract strengths from history files."""
    if not generator_history and not critic_history:
        return "Strengths identified during sprint execution"
        
    strengths = []
    if "improved" in generator_history.lower() or "strength" in generator_history.lower():
        strengths.append("- Design improvements identified during generation")
    if "strength" in critic_history.lower() or "well" in critic_history.lower():
        strengths.append("- Positive aspects noted by Critic")
        
    if strengths:
        return "\n".join(strengths)
    else:
        return "- Strengths to be extracted from sprint history"


def _extract_concerns(generator_history: str, critic_history: str) -> str:
    """Extract concerns from history files."""
    if not critic_history:
        return "Concerns identified during sprint execution"

    concerns = []
    # Extract concerns from critic history (look for concern patterns)
    lines = critic_history.split('\n')
    for line in lines:
        if '[Critical]' in line or '[High]' in line or '[Medium]' in line:
            concerns.append(line.strip())

    if concerns:
        return "\n".join(concerns)
    else:
        return "- Concerns to be extracted from critic history"


def _extract_unresolved_concerns(critic_history: str) -> str:
    """Extract unresolved Critic concerns from history."""
    if not critic_history:
        return "Unresolved Critic concerns for later human evaluation"

    unresolved = []
    # Look for concerns that weren't addressed in subsequent rounds
    # This is a simplified version - full implementation would track concern resolution
    lines = critic_history.split('\n')
    for line in lines:
        if ('[Critical]' in line or '[High]' in line) and 'resolved' not in line.lower():
            unresolved.append(line.strip())

    if unresolved:
        return "\n".join(unresolved)
    else:
        return "- No unresolved concerns identified"


def _generate_exit_notes(sprint_status: SprintStatus, progress: HarnessProgress) -> str:
    """Generate notes on exit mechanism based on sprint status."""
    notes = []

    if sprint_status.status == "passed":
        notes.append(
            f"Sprint completed via quality criteria with final score of "
            f"{sprint_status.final_score:.1f}/10"
        )
        notes.append(
            f"Achieved {sprint_status.consecutive_passes} consecutive passing rounds"
        )
    elif sprint_status.status == "accepted":
        notes.append("Sprint completed via max rounds fallback (best-effort acceptance)")
        notes.append(
            f"Best score achieved: {sprint_status.final_score:.1f}/10"
        )
        notes.append(
            f"Completed after {sprint_status.rounds_completed} rounds"
        )
    elif sprint_status.status == "failed":
        notes.append(
            f"Sprint failed to meet exit criteria after "
            f"{sprint_status.rounds_completed} rounds"
        )
        notes.append(
            f"Best score achieved: {sprint_status.final_score or 0:.1f}/10"
        )
    else:
        notes.append(f"Sprint status: {sprint_status.status}")

    notes.append(f"Sprint {sprint_status.sprint_number} of {progress.total_sprints} total sprints")

    return "\n".join(notes)