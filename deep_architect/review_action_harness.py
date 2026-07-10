from __future__ import annotations

import argparse
import asyncio
import logging
import re
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from deep_architect.coding_agents import (
    CodingAgent,
    CodingAgentConfig,
    create_agent,
    finding_already_satisfied,
)
from deep_architect.config import HarnessConfig, _resolve_default_config_path, load_config
from deep_architect.git_ops import (
    get_modified_files,
    git_commit,
    git_restore_files,
    validate_git_repo,
)
from deep_architect.llm_judge import git_diff_for_file, judge_file, load_llm_rules, rules_for_file
from deep_architect.logger import get_logger
from deep_architect.models.checks import StyleViolation
from deep_architect.quality_checks import (
    CheckFailure,
    capture_baseline,
    load_quality_checks,
    match_profiles,
    new_failures,
    run_checks,
)

if TYPE_CHECKING:
    import git

    from deep_architect.agents.client import RunStats

logger = get_logger(__name__)

# Global flag for graceful shutdown on SIGINT
_shutdown_requested = False


def _sigint_handler(signum: int, frame: object) -> None:
    """Signal handler for SIGINT (CTRL-C). Sets shutdown flag and logs."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("CTRL-C received, finishing current finding before shutdown...")


# ---------------------------------------------------------------------------
# Constants & Data Types
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR = Path("feedback")

# Files in the output dir that are not findings and must never be processed
# or written back to as if they were one (includes review-action's own
# summary file, which lives alongside the findings it reports on).
_NON_FINDING_FILES = frozenset({"SUMMARY.md", "INDEX.md", "review-action_summary.md"})


@dataclass
class ReviewFinding:
    """Represents a single review finding from review-analyzer output."""

    file_path: Path
    line_start: int | None
    line_end: int | None
    existing_code: str
    suggested_code: str
    review_comment: str
    analysis: str
    finding_id: str


@dataclass
class FindingStatus:
    """Persistent status for a finding — written to the .md file after each run."""

    status: str  # "completed" | "error" | "skipped" | "rejected" | "dry-run" | "interrupted"
    timestamp: str
    summary: str = ""
    commit_sha: str | None = None
    error_message: str | None = None


def parse_markdown_finding(file_path: Path) -> ReviewFinding | None:
    """Parse a review-analyzer markdown file into a ReviewFinding."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read %s: %s", file_path, e)
        return None

    finding_id = file_path.stem

    file_match = re.search(r"-?\s*\*\*File\*\*:?\s*(.+)", content)
    lines_match = re.search(r"-?\s*\*\*Lines\*\*:?\s*(.+)", content)
    existing_code_match = re.search(
        r"\*\*Existing Code\*\*:?\s*```[a-zA-Z]*\s*\n(.*?)\n```",
        content,
        re.DOTALL,
    )
    suggested_code_match = re.search(
        r"\*\*Suggested Code\*\*:?\s*```[a-zA-Z]*\s*\n(.*?)\n```",
        content,
        re.DOTALL,
    )
    review_comment_match = re.search(
        r"-?\s*\*\*Review Comment\*\*:?\s*(.+?)(?:\n|$)", content
    )
    analysis_match = re.search(
        r"\*\*Analysis\*\*:?\s*\n(.*?)(?:\n---|\n\*Generated|\Z)",
        content,
        re.DOTALL,
    )

    if file_match is None or existing_code_match is None or review_comment_match is None:
        missing = [
            name
            for name, match in (
                ("File", file_match),
                ("Existing Code", existing_code_match),
                ("Review Comment", review_comment_match),
            )
            if match is None
        ]
        logger.warning("Missing required sections in %s: %s", file_path, ", ".join(missing))
        return None

    file_str = file_match.group(1).strip()
    try:
        full_path = Path(file_str)
    except Exception as e:
        logger.error(
            "Invalid file path '%s' in %s: %s", file_str, file_path, e
        )
        return None

    line_start: int | None = None
    line_end: int | None = None
    if lines_match:
        lines_str = lines_match.group(1).strip()
        if lines_str and "-" in lines_str:
            try:
                parts = lines_str.split("-")
                line_start = int(parts[0].strip())
                line_end = int(parts[1].strip())
            except ValueError:
                pass

    return ReviewFinding(
        file_path=full_path,
        line_start=line_start,
        line_end=line_end,
        existing_code=existing_code_match.group(1).strip(),
        suggested_code=suggested_code_match.group(1).strip() if suggested_code_match else "",
        review_comment=review_comment_match.group(1).strip(),
        analysis=analysis_match.group(1).strip() if analysis_match else "",
        finding_id=finding_id,
    )


def get_verdict(file_path: Path) -> str | None:
    """Return the finding's verdict ("VALID"/"REJECTED"/"BACKLOG"), or None."""
    try:
        content = file_path.read_text(encoding="utf-8")
        verdict_match = re.search(
            r"\*\*Verdict\*\*:?\s*(VALID|REJECTED|BACKLOG)", content
        )
        return verdict_match.group(1) if verdict_match else None
    except Exception:
        return None


def is_valid_finding(file_path: Path) -> bool:
    """Check if a markdown file contains a VALID verdict."""
    return get_verdict(file_path) == "VALID"


# ---------------------------------------------------------------------------
# Finding Status Persistence
# ---------------------------------------------------------------------------

_ACTION_TAKEN_HEADER = "## Action Taken"


def _finding_status_header() -> str:
    """Return the section header used in the markdown file."""
    return _ACTION_TAKEN_HEADER


_ACTION_TAKEN_FIELD_MAX_LEN = 500


def _sanitize_action_taken_field(value: str) -> str:
    """Flatten a value for embedding as a single `Key: value` line.

    write_action_taken()'s block is parsed back by splitting on newlines and
    matching `key: value` per line (read_action_taken()); an unflattened
    multi-line value (e.g. a quality-check failure report containing its own
    "## " headers or "key: value"-shaped lines) would corrupt the parse —
    including possibly overwriting the Status field itself.
    """
    flattened = value.replace("\r\n", "\\n").replace("\n", "\\n")
    if len(flattened) > _ACTION_TAKEN_FIELD_MAX_LEN:
        flattened = flattened[:_ACTION_TAKEN_FIELD_MAX_LEN] + "...(truncated, see logs)"
    return flattened


def has_action_taken(file_path: Path) -> bool:
    """Check if a finding has already been acted upon.

    Uses a raw substring check, so if a field written by write_action_taken()
    ever contained a literal "## Action Taken" the block would be misdetected;
    _sanitize_action_taken_field() flattening newlines out of written fields
    makes this practically unreachable.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
        return _ACTION_TAKEN_HEADER in content
    except Exception:
        return False


def read_action_taken(file_path: Path) -> FindingStatus | None:
    """Read the action-taken status from a finding markdown file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    header_idx = content.find(_ACTION_TAKEN_HEADER)
    if header_idx == -1:
        return None

    # Extract the YAML-style block after the header
    block_start = content.find("\n", header_idx) + 1
    block_end = len(content)
    # Find next header or end of file
    for marker in ("\n## ", "\n---\n", "\n*Generated by"):
        idx = content.find(marker, block_start)
        if 0 <= idx < block_end:
            block_end = idx

    block = content[block_start:block_end].strip()

    status_data: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            status_data[key.strip()] = value.strip()

    st = status_data.get("Status", "").strip()
    if not st:
        return None

    raw_ts = status_data.get("Timestamp", "")
    raw_summary = status_data.get("Summary", "")
    raw_commit = status_data.get("CommitSha", "")
    raw_error = status_data.get("ErrorMessage", "")

    return FindingStatus(
        status=st,
        timestamp=raw_ts.strip(),
        summary=raw_summary.strip(),
        commit_sha=raw_commit.strip() or None,
        error_message=raw_error.strip() or None,
    )


def write_action_taken(file_path: Path, status: FindingStatus) -> None:
    """Append or update the action-taken section in a finding markdown file.

    If an existing section is present, it is replaced to avoid stale status.
    """
    separator = "\n" + _ACTION_TAKEN_HEADER + "\n"
    new_block = separator + "\n".join([
        f"Status: {status.status}",
        f"Timestamp: {status.timestamp}",
        f"Summary: {_sanitize_action_taken_field(status.summary)}",
    ])
    if status.commit_sha:
        new_block += f"\nCommitSha: {status.commit_sha}"
    if status.error_message:
        new_block += f"\nErrorMessage: {_sanitize_action_taken_field(status.error_message)}"
    new_block += "\n"

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        content = ""

    # Remove any existing section
    header_idx = content.find(_ACTION_TAKEN_HEADER)
    if header_idx >= 0:
        content = content[:header_idx].rstrip("\n")

    content += new_block
    try:
        file_path.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.error("Failed to write action status to %s: %s", file_path, e)


def _now_iso() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Quality-check failure reporting
# ---------------------------------------------------------------------------


def _render_failure_report(
    prog: list[CheckFailure], style: list[tuple[Path, StyleViolation]]
) -> str:
    """Render programmatic + LLM-judged failures into a report for the fix agent."""
    lines: list[str] = []
    if prog:
        lines.append("## Programmatic check failures\n")
        for f in prog:
            lines.append(f"### `{f.command}` (profile: {f.profile}, exit code {f.returncode})")
            lines.append(f"```\n{f.output}\n```\n")
    if style:
        lines.append("## Style rule violations\n")
        for file_path, violation in style:
            loc = f":{violation.line}" if violation.line is not None else ""
            lines.append(
                f"- **{file_path}{loc}** [{violation.severity}] {violation.rule_id}: "
                f"{violation.description}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def _commit_status_file(repo: git.Repo, md_file: Path, finding_id: str) -> None:
    """Commit a finding's own status write immediately.

    Without this, the status write sits as a dirty file until the *next*
    finding's get_modified_files() sweeps it into that finding's commit —
    misattributing it and breaking resume for the current finding.
    """
    git_commit(
        repo,
        f"chore: record review-action status for {finding_id}",
        [md_file],
    )


def _process_single_finding(
    md_file: Path,
    agent: CodingAgent,
    max_retries: int,
    retry_delay: float,
    dry_run: bool,
    harness_config: HarnessConfig,
    skip_llm_checks: bool = False,
    quality_checks_override: Path | None = None,
) -> tuple[str, bool, str | None]:
    """Process a single VALID finding. Returns (status, committed, error).

    Status is one of: 'skipped', 'committed', 'error'.
    After each action, a ## Action Taken section is appended to the finding
    markdown file so interrupted runs can be resumed safely.
    """
    finding = parse_markdown_finding(md_file)
    if finding is None:
        # Warning-type findings have no File/Existing Code/Review Comment
        # anchor to act on — skip, don't error.
        skip_msg = f"{md_file.name}: warning-type finding — no actionable code change"
        write_action_taken(
            md_file,
            FindingStatus(
                status="skipped",
                timestamp=_now_iso(),
                summary=skip_msg,
            ),
        )
        return (
            "skipped",
            False,
            skip_msg,
        )

    logger.info(
        "Processing finding %s for %s", finding.finding_id, finding.file_path
    )

    # Dry-run: skip agent call entirely. Status is "dry-run", not "completed" —
    # no fix was actually applied or committed, so a later real run must
    # replay this finding rather than treating it as already resolved.
    if dry_run:
        logger.info("[DRY RUN] Would apply fix for %s", finding.file_path)
        write_action_taken(
            md_file,
            FindingStatus(
                status="dry-run",
                timestamp=_now_iso(),
                summary="[DRY RUN] Would apply fix",
            ),
        )
        return ("committed", True, None)

    # Capture original file content before any changes for verification
    original_content: str | None = None
    try:
        original_content = finding.file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug(
            "Original file not found for %s, skipping original content capture",
            finding.file_path,
        )

    # Quality checks: discover the target repo's checks and capture a
    # pre-fix baseline now, before the fix is applied, so the baseline
    # reflects the file's state prior to any change (fail-closed diffing
    # below only blocks on failures *introduced* by the fix).
    try:
        repo = validate_git_repo(Path.cwd())
    except Exception as e:
        error_msg = f"Failed to access git repo for {finding.file_path}: {e}"
        write_action_taken(
            md_file,
            FindingStatus(
                status="error",
                timestamp=_now_iso(),
                summary="Git repo access failed",
                error_message=error_msg,
            ),
        )
        return ("error", False, error_msg)

    if original_content is not None:
        already_satisfied_reason = finding_already_satisfied(
            original_content, finding.existing_code, finding.suggested_code
        )
        if already_satisfied_reason is not None:
            logger.info(
                "Skipping %s: %s", finding.finding_id, already_satisfied_reason
            )
            write_action_taken(
                md_file,
                FindingStatus(
                    status="skipped",
                    timestamp=_now_iso(),
                    summary=already_satisfied_reason,
                ),
            )
            _commit_status_file(repo, md_file, finding.finding_id)
            return ("skipped", False, already_satisfied_reason)

    repo_root = Path(repo.working_dir)
    checks_cfg = load_quality_checks(
        repo_root,
        override=quality_checks_override,
        default_timeout=harness_config.thresholds.check_command_timeout,
    )
    pre_matched = match_profiles(checks_cfg, [finding.file_path], repo_root)
    baseline = capture_baseline(pre_matched, checks_cfg, repo_root)
    rules = [] if skip_llm_checks else load_llm_rules(repo_root, checks_cfg)
    max_iterations = harness_config.thresholds.check_max_fix_iterations

    if not checks_cfg.profiles and not rules:
        logger.info(
            "No quality checks discovered for %s — proceeding to commit",
            finding.finding_id,
        )

    # Apply fix with retries
    success = False
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        # Check for interrupt signal
        if _shutdown_requested:
            interrupt_msg = "Interrupted by SIGINT"
            write_action_taken(
                md_file,
                FindingStatus(
                    status="interrupted",
                    timestamp=_now_iso(),
                    summary=interrupt_msg,
                    error_message=interrupt_msg,
                ),
            )
            _commit_status_file(repo, md_file, finding.finding_id)
            return ("interrupted", False, interrupt_msg)

        try:
            success = asyncio.run(
                agent.apply_fix(
                    finding.file_path,
                    finding.existing_code,
                    finding.suggested_code,
                    finding.analysis,
                    original_content=original_content,
                    review_comment=finding.review_comment,
                )
            )

            if success:
                break
            last_error = "Agent.apply_fix returned False"

        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "Attempt %d failed for %s: %s",
                attempt + 1,
                finding.file_path,
                e,
            )

            if attempt < max_retries:
                asyncio.get_event_loop().run_until_complete(
                    asyncio.sleep(retry_delay * (2 ** attempt))
                )

    if not success:
        error_msg = (
            f"Failed to apply fix for {finding.file_path} "
            f"after {max_retries + 1} attempts: {last_error}"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="error",
                timestamp=_now_iso(),
                summary=f"Fix failed after {max_retries + 1} attempts",
                error_message=error_msg,
            ),
        )
        _commit_status_file(repo, md_file, finding.finding_id)
        return (
            "error",
            False,
            error_msg,
        )

    # Quality-check fix loop: check → feedback → fix until clean or the
    # iteration cap is hit. Fail-closed: a finding is never committed while
    # checks it introduced are failing.
    modified: list[Path] = []
    prog_failures: list[CheckFailure] = []
    style_failures: list[tuple[Path, StyleViolation]] = []
    checks_clean = False

    report_only = max_iterations == 0
    iterations_to_run = 1 if report_only else max_iterations

    for iteration in range(1, iterations_to_run + 1):
        if _shutdown_requested:
            interrupt_msg = "Interrupted by SIGINT"
            write_action_taken(
                md_file,
                FindingStatus(
                    status="interrupted",
                    timestamp=_now_iso(),
                    summary=interrupt_msg,
                    error_message=interrupt_msg,
                ),
            )
            _commit_status_file(repo, md_file, finding.finding_id)
            return ("interrupted", False, interrupt_msg)

        modified = get_modified_files(repo)
        matched = match_profiles(checks_cfg, modified, repo_root)
        prog_failures = new_failures(
            run_checks(matched, checks_cfg, repo_root), baseline, modified
        )

        style_failures = []
        if not prog_failures and rules:
            for py_file in (m for m in modified if m.suffix == ".py"):
                diff = git_diff_for_file(repo, py_file)
                verdict = asyncio.run(
                    judge_file(
                        py_file,
                        diff,
                        rules_for_file(rules, py_file, repo_root),
                        agent,
                        repo_root,
                        max_parse_retries=harness_config.thresholds.judge_parse_retries,
                    )
                )
                style_failures.extend((py_file, v) for v in verdict.blocking)

        if not prog_failures and not style_failures:
            checks_clean = True
            break

        if report_only:
            logger.warning(
                "Quality checks failing for %s but check_max_fix_iterations=0 "
                "(report-only) — not blocking: %d programmatic, %d style",
                finding.finding_id, len(prog_failures), len(style_failures),
            )
            checks_clean = True
            break

        logger.info(
            "Check iteration %d/%d for %s: %d programmatic, %d style failure(s)",
            iteration, max_iterations, finding.finding_id,
            len(prog_failures), len(style_failures),
        )

        if iteration == max_iterations:
            break

        report = _render_failure_report(prog_failures, style_failures)
        ok = asyncio.run(
            agent.fix_check_failures(modified, report, finding.analysis)
        )
        if not ok:
            logger.warning(
                "fix_check_failures returned False on iteration %d for %s",
                iteration, finding.finding_id,
            )

    if not checks_clean:
        git_restore_files(repo, modified)
        report = _render_failure_report(prog_failures, style_failures)
        error_msg = (
            f"Quality checks failed after {max_iterations} iteration(s) "
            f"for {finding.file_path}: {report[:2000]}"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="error",
                timestamp=_now_iso(),
                summary=f"Quality checks failed after {max_iterations} iteration(s)",
                error_message=error_msg,
            ),
        )
        _commit_status_file(repo, md_file, finding.finding_id)
        return ("error", False, error_msg)

    # Commit changes
    try:
        comment_snippet = finding.review_comment[:50]
        suffix = (
            "..." if len(finding.review_comment) > 50 else ""
        )
        commit_subject = f"fix: {comment_snippet}{suffix} [{finding.finding_id}]"
        commit_message = (
            f"{commit_subject}\n\n"
            f"{finding.review_comment}\n\n"
            f"Review-Finding: {md_file.name}\n"
            f"Generated-by: deep-architect review-action"
        )
        commit_paths = modified if modified else [finding.file_path]
        committed = git_commit(
            repo, commit_message, commit_paths
        )
        if committed:
            logger.info("Committed fix for %s", finding.file_path)
            commit_sha = repo.head.commit.hexsha[:8]
            write_action_taken(
                md_file,
                FindingStatus(
                    status="completed",
                    timestamp=_now_iso(),
                    summary=f"Fix applied and committed: {commit_subject}",
                    commit_sha=commit_sha,
                ),
            )
            _commit_status_file(repo, md_file, finding.finding_id)
            return ("committed", True, None)
        else:
            logger.info(
                "No changes to commit for %s (file unchanged)",
                finding.file_path,
            )
            write_action_taken(
                md_file,
                FindingStatus(
                    status="skipped",
                    timestamp=_now_iso(),
                    summary="File already contained expected changes",
                ),
            )
            _commit_status_file(repo, md_file, finding.finding_id)
            return ("skipped", False, None)
    except Exception as e:
        error_msg = (
            f"Failed to commit changes for {finding.file_path}: {e}"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="error",
                timestamp=_now_iso(),
                summary="Commit failed",
                error_message=error_msg,
            ),
        )
        return (
            "error",
            False,
            error_msg,
        )


def process_findings(
    output_dir: Path,
    agent: CodingAgent,
    max_retries: int,
    retry_delay: float,
    harness_config: HarnessConfig,
    dry_run: bool = False,
    force: bool = False,
    skip_errors: bool = False,
    skip_llm_checks: bool = False,
    quality_checks_override: Path | None = None,
) -> dict[str, int]:
    """Process all VALID findings in the output directory.

    Already-processed findings (those with a ## Action Taken section) are
    skipped unless force=True.  Findings previously marked "error" are
    retried unless skip_errors=True.
    """
    stats: dict[str, int] = {
        "processed": 0,
        "committed": 0,
        "skipped": 0,
        "errors": 0,
        "restored": 0,
        "total_findings": 0,
        "interrupted": False,
    }

    if not output_dir.exists():
        logger.error("Output directory %s does not exist", output_dir)
        return stats

    markdown_files = sorted(output_dir.glob("*.md"))
    if not markdown_files:
        logger.warning("No markdown files found in %s", output_dir)
        return stats

    logger.info("Found %d markdown files to process", len(markdown_files))

    # Count eligible findings first
    for md_file in markdown_files:
        if md_file.name not in _NON_FINDING_FILES:
            stats["total_findings"] += 1

    total = stats["total_findings"]
    finding_index = 0

    for md_file in markdown_files:
        # Check for interrupt signal
        if _shutdown_requested:
            stats["interrupted"] = True
            break

        # Skip summary/index files
        if md_file.name in _NON_FINDING_FILES:
            continue

        finding_index += 1
        pct = round(finding_index / total * 100) if total else 0
        logger.info(
            "Addressing Review %d/%d (%d%%): %s",
            finding_index,
            total,
            pct,
            md_file.stem,
        )

        # Skip already-processed findings unless forced
        if not force and has_action_taken(md_file):
            existing = read_action_taken(md_file)
            if existing and existing.status in ("completed", "rejected"):
                logger.info(
                    "Skipping %s finding: %s (commit: %s)",
                    existing.status,
                    md_file.name,
                    existing.commit_sha or "unknown",
                )
                logger.info(
                    "  -> Skipped (already %s, commit %s)",
                    existing.status,
                    existing.commit_sha or "unknown",
                )
                stats["restored"] += 1
                write_summary_file(stats, output_dir)
                continue
            elif existing and existing.status == "error" and skip_errors:
                logger.info(
                    "Skipping errored finding (--skip-errors): %s",
                    md_file.name,
                )
                logger.info("  -> Skipped (previous error)")
                stats["skipped"] += 1
                write_summary_file(stats, output_dir)
                continue
            elif existing:
                logger.info(
                    "Replaying %s finding: %s",
                    existing.status,
                    md_file.name,
                )

        stats["processed"] += 1

        verdict = get_verdict(md_file)
        if verdict != "VALID":
            verdict_label = verdict or "unknown"
            logger.info(
                "Skipping non-VALID finding: %s (verdict: %s)",
                md_file.name,
                verdict_label,
            )
            logger.info("  -> Rejected (verdict %s)", verdict_label)
            stats["skipped"] += 1
            write_action_taken(
                md_file,
                FindingStatus(
                    status="rejected",
                    timestamp=_now_iso(),
                    summary=f"Verdict {verdict_label} — not actioned",
                ),
            )
            write_summary_file(stats, output_dir)
            continue

        status, committed, error = _process_single_finding(
            md_file,
            agent,
            max_retries,
            retry_delay,
            dry_run,
            harness_config,
            skip_llm_checks=skip_llm_checks,
            quality_checks_override=quality_checks_override,
        )

        if status == "error":
            logger.error("%s", error or f"Unknown error processing {md_file.name}")
            logger.info("  -> Error: %s", error or "unknown error")
            stats["errors"] += 1
        elif status == "skipped":
            logger.warning("%s", error or f"Skipped {md_file.name}")
            logger.info("  -> Skipped (no change committed)")
            stats["skipped"] += 1
        elif committed:
            logger.info("  -> Change applied and committed")
            stats["committed"] += 1

        write_summary_file(stats, output_dir)

    return stats


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Apply review-analyzer fixes automatically"
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=DEFAULT_OUTPUT_DIR,
        type=Path,
        help="Directory containing review-analyzer markdown output",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model to use (overrides config)",
    )
    parser.add_argument(
        "--provider",
        choices=["opencode", "claude", "grok"],
        default=None,
        help="Agent provider to use (overrides config, default: opencode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to configuration file "
            "(defaults to ~/.config/deep-architect/config.toml, "
            "falls back to legacy ~/.deep-architect.toml)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process findings that were already completed",
    )
    parser.add_argument(
        "--skip-errors",
        action="store_true",
        help="Skip findings that previously failed instead of retrying them",
    )
    parser.add_argument(
        "--max-check-iterations",
        type=int,
        default=None,
        help=(
            "Post-fix quality-check retry cap (overrides config); "
            "0 = run checks but never block or retry"
        ),
    )
    parser.add_argument(
        "--skip-llm-checks",
        action="store_true",
        help="Run programmatic quality checks only, skip the LLM style-rule judge",
    )
    parser.add_argument(
        "--quality-checks",
        type=Path,
        default=None,
        help="Explicit path to a .quality-checks.toml file (overrides auto-discovery)",
    )
    return parser.parse_args(argv)


_OUTCOME_LABELS = {
    "completed": "Fixed",
    "rejected": "Rejected",
    "error": "Error",
    "skipped": "Skipped",
    "interrupted": "Interrupted",
    "dry-run": "Dry run",
}


def _escape_table_cell(value: str) -> str:
    """Escape characters that would break a markdown table cell."""
    return value.replace("|", "\\|").replace("\n", " ")


def _outcome_label(status: FindingStatus) -> str:
    """Human-readable outcome label, including the verdict for rejections."""
    label = _OUTCOME_LABELS.get(status.status, status.status)
    if status.status == "rejected" and status.summary.startswith("Verdict "):
        # "Verdict BACKLOG — not actioned" -> "Rejected (BACKLOG)"
        verdict = status.summary.split(" ")[1]
        return f"{label} ({verdict})"
    return label


def build_detailed_summary(output_dir: Path) -> str:
    """Render a per-finding markdown table from each finding's Action Taken block.

    Re-scans the output directory rather than accumulating results in memory
    during process_findings() — this way restored/prior-run findings and
    findings never reached (e.g. after a SIGINT) still get a row, using
    exactly the same persisted state the resume logic relies on.
    """
    lines = [
        "## Findings",
        "",
        "| Finding | File | Outcome | Commit | What was done |",
        "|---------|------|---------|--------|---------------|",
    ]
    for md_file in sorted(output_dir.glob("*.md")):
        if md_file.name in _NON_FINDING_FILES:
            continue

        finding = parse_markdown_finding(md_file)
        finding_id = finding.finding_id if finding else md_file.stem
        if finding is not None:
            file_ref = str(finding.file_path)
        else:
            bare_file_match = re.search(
                r"-?\s*\*\*File\*\*:?\s*(.+)", md_file.read_text(encoding="utf-8")
            )
            file_ref = bare_file_match.group(1).strip() if bare_file_match else "(unparseable)"

        action = read_action_taken(md_file)
        if action is None:
            outcome = "Not processed"
            commit_cell = "—"
            what = "—"
        else:
            outcome = _outcome_label(action)
            commit_cell = f"`{action.commit_sha}`" if action.commit_sha else "—"
            what = action.summary or "—"
            if action.status == "error" and action.error_message:
                first_line = action.error_message.split("\\n", 1)[0]
                what = f"{what}: {first_line}" if what != "—" else first_line

        link = f"[{finding_id}](./{md_file.name})"
        lines.append(
            "| "
            + " | ".join(
                _escape_table_cell(cell)
                for cell in (link, file_ref, outcome, commit_cell, what)
            )
            + " |"
        )
    return "\n".join(lines)


def write_summary_file(
    stats: dict[str, int], output_dir: Path, run_stats: RunStats | None = None
) -> None:
    """Write the aggregate counters + per-finding table to review-action_summary.md."""
    summary_file = output_dir / "review-action_summary.md"
    with summary_file.open("w", encoding="utf-8") as f:
        f.write("# Review Action Summary\n\n")
        f.write(f"Restored:   {stats['restored']}\n")
        f.write(f"Processed:  {stats['processed']}\n")
        f.write(f"Committed:  {stats['committed']}\n")
        f.write(f"Skipped:    {stats['skipped']}\n")
        f.write(f"Errors:     {stats['errors']}\n")
        if run_stats is not None:
            f.write(
                f"Total cost: ${run_stats.total_cost_usd:.4f} "
                f"across {run_stats.num_calls} agent call(s)\n"
            )
        f.write(f"Interrupted: {'yes' if stats['interrupted'] else 'no'}\n")
        processed = stats['processed']
        total = stats['total_findings']
        f.write(f"Progress: {processed} out of {total} findings processed\n")
        f.write("\n")
        f.write(build_detailed_summary(output_dir))
        f.write("\n")


def print_summary(
    stats: dict[str, int], output_dir: Path, run_stats: RunStats | None = None
) -> None:
    """Print the final processing summary and write it to file."""
    print("\n=== Review Action Harness Summary ===")
    print(f"Restored:   {stats['restored']}")
    print(f"Processed:  {stats['processed']}")
    print(f"Committed:  {stats['committed']}")
    print(f"Skipped:    {stats['skipped']}")
    print(f"Errors:     {stats['errors']}")
    if run_stats is not None:
        print(
            f"Total cost: ${run_stats.total_cost_usd:.4f} "
            f"across {run_stats.num_calls} agent call(s)"
        )

    write_summary_file(stats, output_dir, run_stats)


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    args = parse_args(argv)

    # Reset shutdown flag and set up signal handler for graceful interrupt
    global _shutdown_requested
    _shutdown_requested = False
    signal.signal(signal.SIGINT, _sigint_handler)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load configuration
    harness_config = HarnessConfig()
    try:
        config_path = args.config or _resolve_default_config_path()
        if config_path.exists():
            harness_config = load_config(config_path)
    except Exception as e:
        logger.warning(
            "Failed to load config: %s, using defaults", e
        )

    provider = args.provider or "opencode"
    if provider == "grok":
        # grok is not a valid Anthropic model alias — don't inherit the
        # generator's TOML default; None lets GrokAgent use its own default.
        model = args.model
    else:
        model = args.model or harness_config.generator.model

    # Validate git repo
    try:
        validate_git_repo(Path.cwd())
    except SystemExit:
        return 1
    except Exception as e:
        logger.error("Not in a valid git repository: %s", e)
        return 1

    # Initialize configs
    agent_config = CodingAgentConfig(
        provider=provider,
        model=model,
        max_retries=harness_config.thresholds.model_comm_failure_threshold,
        retry_delay=harness_config.thresholds.model_comm_base_backoff,
        permission_mode="bypassPermissions",
        timeout_seconds=harness_config.thresholds.coding_agent_timeout,
        max_turns=harness_config.thresholds.coding_agent_max_turns,
    )

    if args.max_check_iterations is not None:
        harness_config.thresholds.check_max_fix_iterations = args.max_check_iterations

    # Create agent
    try:
        agent = create_agent(agent_config)
    except Exception as e:
        logger.error("Failed to initialize agent: %s", e)
        return 1

    from deep_architect.agents.client import init_run_stats  # noqa: PLC0415

    run_stats = init_run_stats()

    # Process findings
    stats = process_findings(
        args.output_dir,
        agent,
        agent_config.max_retries,
        agent_config.retry_delay,
        harness_config,
        args.dry_run,
        force=args.force,
        skip_errors=args.skip_errors,
        skip_llm_checks=args.skip_llm_checks,
        quality_checks_override=args.quality_checks,
    )

    print_summary(stats, args.output_dir, run_stats)

    return 130 if stats["interrupted"] else (0 if stats["errors"] == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
