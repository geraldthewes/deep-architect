from __future__ import annotations

import argparse
import asyncio
import logging
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TextIO

from deep_architect import action_report as _action_report
from deep_architect import feedback_report as _feedback_report
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
    from deep_architect.agents.client import RunStats

# Re-export parse helpers for tests and public API compatibility.
ReviewFinding = _feedback_report.ReviewFinding
get_verdict = _feedback_report.get_verdict
get_severity = _feedback_report.get_severity
is_valid_finding = _feedback_report.is_valid_finding
parse_markdown_finding = _feedback_report.parse_markdown_finding
_NON_FINDING_FILES = _feedback_report.NON_FINDING_FILES

# Action Taken status — implementation lives in action_report (shared with browse TUI).
FindingStatus = _action_report.FindingStatus
has_action_taken = _action_report.has_action_taken
read_action_taken = _action_report.read_action_taken
write_action_taken = _action_report.write_action_taken
_outcome_label = _action_report.outcome_label

logger = get_logger(__name__)

# Global flag for graceful shutdown on SIGINT
_shutdown_requested = False


def request_shutdown() -> None:
    """Request a graceful stop after the current finding finishes.

    Used by the SIGINT handler and the full-screen TUI stop binding.
    """
    global _shutdown_requested
    _shutdown_requested = True


def _sigint_handler(signum: int, frame: object) -> None:
    """Signal handler for SIGINT (CTRL-C). Sets shutdown flag and logs."""
    request_shutdown()
    logger.info("CTRL-C received, finishing current finding before shutdown...")


def _install_sigint_handler() -> None:
    """Install the SIGINT handler when running on the main thread.

    ``signal.signal`` raises ``ValueError`` off the main thread. review-driver
    invokes this ``main()`` in-process from a Textual worker; the parent
    already owns SIGINT on the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGINT, _sigint_handler)


# ---------------------------------------------------------------------------
# Constants & Data Types
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR = Path("feedback")

# OCR severity rank for --min-severity. Missing / unknown ranks as 0 and
# never meets an explicit floor.
_SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}
MIN_SEVERITY_CHOICES: tuple[str, ...] = ("low", "medium", "high")


def severity_meets_floor(severity: str, min_severity: str) -> bool:
    """True when *severity* is at or above the --min-severity floor."""
    rank = _SEVERITY_RANK.get(severity.strip().lower(), 0)
    floor = _SEVERITY_RANK[min_severity]
    return rank >= floor


def _exclude_output_dir(paths: list[Path], output_dir: Path) -> list[Path]:
    """Drop paths under output_dir.

    Finding markdown and the summary file are review-action's own ephemeral
    working state (review-analyzer's scratch output plus the Action Taken
    audit trail appended on disk) — they must never be staged into a git
    commit, only the actual code files a fix touches.
    """
    output_dir_resolved = output_dir.resolve()
    kept = []
    for p in paths:
        resolved = p.resolve()
        if resolved == output_dir_resolved or output_dir_resolved in resolved.parents:
            continue
        kept.append(p)
    return kept


# ---------------------------------------------------------------------------
# Progress reporting (plain text or full-screen Textual TUI)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMeta:
    """Immutable run metadata shown in the progress header."""

    output_dir: Path
    provider: str
    model: str | None
    dry_run: bool
    force: bool
    skip_errors: bool
    total_findings: int
    coding_agent: str


@dataclass(frozen=True)
class ProgressEvent:
    """One finding completion (or mid-finding phase update) during a run."""

    completed: int
    total: int
    finding_id: str
    file_path: str
    outcome: str  # completed|error|skipped|rejected|dry-run|interrupted|restored
    summary: str
    commit_sha: str | None
    elapsed_s: float
    phase: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    severity: str = ""  # OCR label, lowercased; empty if absent
    duration_s: float = 0.0  # wall-clock for this finding only


class ProgressReporter(Protocol):
    """Progress sink used by the action pipeline (plain text or TUI)."""

    def start(self, meta: RunMeta) -> None:
        """Called once before processing begins."""

    def on_result(self, event: ProgressEvent) -> None:
        """Called after each finding is finished (including skips/restores)."""

    def on_phase(self, event: ProgressEvent) -> None:
        """Called for mid-finding phase updates (does not advance completed)."""

    def finish(self, stats: dict[str, int]) -> None:
        """Called after all findings are processed (or on early exit)."""


def should_use_tui(
    *,
    force_tui: bool | None = None,
    stream: TextIO | None = None,
) -> bool:
    """Return whether the live TUI should be used.

    *force_tui* is ``True`` for ``--tui``, ``False`` for ``--no-tui``, and
    ``None`` for auto-detect via ``stream.isatty()`` (default: stdout).
    """
    if force_tui is True:
        return True
    if force_tui is False:
        return False
    target = stream if stream is not None else sys.stdout
    return bool(target.isatty())


class PlainReporter:
    """Plain-text progress reporter for non-interactive terminals and CI."""

    def start(self, meta: RunMeta) -> None:
        print(
            f"Applying fixes for {meta.total_findings} findings "
            f"(agent={meta.coding_agent})…"
        )
        print(f"Feedback dir: {meta.output_dir}/")
        flags: list[str] = []
        if meta.dry_run:
            flags.append("dry-run")
        if meta.force:
            flags.append("force")
        if meta.skip_errors:
            flags.append("skip-errors")
        if flags:
            print(f"Flags: {', '.join(flags)}")

    def on_result(self, event: ProgressEvent) -> None:
        pct = round(event.completed / event.total * 100) if event.total else 0
        commit_bit = f" commit={event.commit_sha}" if event.commit_sha else ""
        print(
            f"  [{event.completed}/{event.total} {pct}%] "
            f"{event.finding_id}: {event.outcome}"
            f"{commit_bit} — {event.summary}"
        )

    def on_phase(self, event: ProgressEvent) -> None:
        # Keep plain mode quiet mid-finding; logger already covers detail.
        _ = event

    def finish(self, stats: dict[str, int]) -> None:
        # Final counters are printed by print_summary after the pipeline returns.
        _ = stats


# ---------------------------------------------------------------------------
# Finding Status Persistence
# ---------------------------------------------------------------------------

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


def _emit_phase(
    on_phase: Callable[[ProgressEvent], None] | None,
    *,
    finding_id: str,
    file_path: str,
    phase: str,
    completed: int,
    total: int,
    t0: float,
    stats: dict[str, int] | None = None,
) -> None:
    """Notify the progress reporter of a mid-finding phase (no completion advance)."""
    if on_phase is None:
        return
    on_phase(
        ProgressEvent(
            completed=completed,
            total=total,
            finding_id=finding_id,
            file_path=file_path,
            outcome="in-progress",
            summary=phase,
            commit_sha=None,
            elapsed_s=time.monotonic() - t0,
            phase=phase,
            stats=dict(stats) if stats else {},
        )
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
    *,
    on_phase: Callable[[ProgressEvent], None] | None = None,
    completed: int = 0,
    total: int = 0,
    t0: float | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[str, bool, str | None]:
    """Process a single VALID finding. Returns (status, committed, error).

    Status is one of: 'skipped', 'committed', 'error'.
    After each action, a ## Action Taken section is appended to the finding
    markdown file so interrupted runs can be resumed safely.
    """
    started = t0 if t0 is not None else time.monotonic()
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

    _emit_phase(
        on_phase,
        finding_id=finding.finding_id,
        file_path=str(finding.file_path),
        phase="applying",
        completed=completed,
        total=total,
        t0=started,
        stats=stats,
    )

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

            # File still identical after a failed apply: re-check whether the
            # finding is already resolved (agent no-op / prior fix). Avoids
            # burning remaining retries when nothing can change on disk.
            if original_content is not None:
                try:
                    current_after = finding.file_path.read_text(encoding="utf-8")
                except OSError:
                    current_after = None
                if (
                    current_after is not None
                    and current_after.replace("\r\n", "\n")
                    == original_content.replace("\r\n", "\n")
                ):
                    reason = finding_already_satisfied(
                        current_after,
                        finding.existing_code,
                        finding.suggested_code,
                    )
                    if reason is not None:
                        logger.info(
                            "Skipping %s after no-op apply: %s",
                            finding.finding_id,
                            reason,
                        )
                        write_action_taken(
                            md_file,
                            FindingStatus(
                                status="skipped",
                                timestamp=_now_iso(),
                                summary=reason,
                            ),
                        )
                        return ("skipped", False, reason)

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
        # If every attempt left the file untouched, report a clear no-op error
        # (still an error: agent completed without applying a needed fix).
        attempts_used = max_retries + 1
        error_msg = (
            f"Failed to apply fix for {finding.file_path} "
            f"after {attempts_used} attempts: {last_error}"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="error",
                timestamp=_now_iso(),
                summary=f"Fix failed after {attempts_used} attempts",
                error_message=error_msg,
            ),
        )
        return (
            "error",
            False,
            error_msg,
        )

    # Successful apply that left the target file byte-identical is an
    # intentional no-op (already satisfied / agent reported already done).
    # Skip quality checks and commit — there is nothing to verify or ship.
    if original_content is not None:
        try:
            post_content = finding.file_path.read_text(encoding="utf-8")
        except OSError:
            post_content = None
        if (
            post_content is not None
            and post_content.replace("\r\n", "\n")
            == original_content.replace("\r\n", "\n")
        ):
            reason = finding_already_satisfied(
                post_content, finding.existing_code, finding.suggested_code
            ) or "Already addressed — agent completed with no file changes"
            logger.info(
                "Skipping %s: successful no-op (%s)", finding.finding_id, reason
            )
            write_action_taken(
                md_file,
                FindingStatus(
                    status="skipped",
                    timestamp=_now_iso(),
                    summary=reason,
                ),
            )
            return ("skipped", False, reason)

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
            return ("interrupted", False, interrupt_msg)

        phase_label = (
            f"quality-checks {iteration}/{max_iterations}"
            if not report_only
            else "quality-checks (report-only)"
        )
        _emit_phase(
            on_phase,
            finding_id=finding.finding_id,
            file_path=str(finding.file_path),
            phase=phase_label,
            completed=completed,
            total=total,
            t0=started,
            stats=stats,
        )

        modified = _exclude_output_dir(get_modified_files(repo), md_file.parent)
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
        return ("error", False, error_msg)

    # Commit changes
    try:
        _emit_phase(
            on_phase,
            finding_id=finding.finding_id,
            file_path=str(finding.file_path),
            phase="committing",
            completed=completed,
            total=total,
            t0=started,
            stats=stats,
        )
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


def _file_ref_for_finding(md_file: Path) -> str:
    """Best-effort target file path from a finding markdown (for progress UI)."""
    try:
        content = md_file.read_text(encoding="utf-8")
    except OSError:
        return md_file.name
    match = re.search(r"-?\s*\*\*File\*\*:?\s*(.+)", content)
    if match:
        return match.group(1).strip()
    finding = parse_markdown_finding(md_file)
    if finding is not None:
        return str(finding.file_path)
    return md_file.name


def _emit_result(
    on_result: Callable[[ProgressEvent], None] | None,
    *,
    completed: int,
    total: int,
    finding_id: str,
    file_path: str,
    outcome: str,
    summary: str,
    commit_sha: str | None,
    t0: float,
    stats: dict[str, int],
    severity: str = "",
    duration_s: float = 0.0,
) -> None:
    if on_result is None:
        return
    on_result(
        ProgressEvent(
            completed=completed,
            total=total,
            finding_id=finding_id,
            file_path=file_path,
            outcome=outcome,
            summary=summary,
            commit_sha=commit_sha,
            elapsed_s=time.monotonic() - t0,
            stats={
                k: int(v) if not isinstance(v, bool) else int(v)
                for k, v in stats.items()
                if k != "interrupted"
            },
            severity=severity,
            duration_s=duration_s,
        )
    )


def count_actionable_findings(output_dir: Path) -> int:
    """Count VALID finding markdown files (excludes summary/index and non-VALID).

    Used for live progress totals — REJECTED/BACKLOG/TIMEOUT findings are never
    auto-fixed and must not inflate the UI denominator.
    """
    if not output_dir.exists():
        return 0
    return sum(
        1
        for p in output_dir.glob("*.md")
        if p.name not in _NON_FINDING_FILES and is_valid_finding(p)
    )


def _stamp_non_valid_findings(
    non_valid_files: list[Path],
    *,
    force: bool,
) -> int:
    """Write Action Taken for non-VALID findings without live progress events.

    Returns the number of non-VALID findings considered (for summary counters).
    Already-stamped rejected findings are left alone unless *force* is set.
    """
    stamped_or_seen = 0
    for md_file in non_valid_files:
        stamped_or_seen += 1
        existing = read_action_taken(md_file) if has_action_taken(md_file) else None
        if (
            not force
            and existing is not None
            and existing.status == "rejected"
        ):
            logger.debug(
                "Non-VALID finding already marked rejected: %s",
                md_file.name,
            )
            continue

        verdict_label = get_verdict(md_file) or "unknown"
        summary = f"Verdict {verdict_label} — not actioned"
        logger.info(
            "Not actioning non-VALID finding: %s (verdict: %s)",
            md_file.name,
            verdict_label,
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="rejected",
                timestamp=_now_iso(),
                summary=summary,
            ),
        )
    return stamped_or_seen


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
    min_severity: str | None = None,
    *,
    run_started_at: str | None = None,
    coding_agent: str | None = None,
    on_result: Callable[[ProgressEvent], None] | None = None,
    on_phase: Callable[[ProgressEvent], None] | None = None,
) -> dict[str, int]:
    """Process all VALID findings in the output directory.

    Non-VALID findings (REJECTED, BACKLOG, TIMEOUT, unknown) are stamped with
    an Action Taken ``rejected`` record for the summary table and
    review-feedback-browse, but they do not appear in live progress events or
    inflate Fixed/Skipped/Restored counters.

    Already-processed VALID findings (those with a ## Action Taken section) are
    skipped unless force=True.  Findings previously marked "error" are
    retried unless skip_errors=True.

    *on_result* is invoked after each **VALID** finding is considered (including
    restores/skips). *on_phase* is invoked for mid-finding phases such as
    applying a fix or running quality checks.
    """
    if run_started_at is None:
        run_started_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if coding_agent is None:
        agent_model = getattr(agent, "model", None)
        coding_agent = (
            f"{type(agent).__name__} ({agent_model})" if agent_model else type(agent).__name__
        )

    stats: dict[str, int] = {
        "processed": 0,
        "committed": 0,
        "skipped": 0,
        "errors": 0,
        "restored": 0,
        "not_actioned": 0,
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

    finding_files = [p for p in markdown_files if p.name not in _NON_FINDING_FILES]
    logger.info("Found %d finding markdown files", len(finding_files))

    actionable: list[Path] = []
    non_valid: list[Path] = []
    for md_file in finding_files:
        if is_valid_finding(md_file):
            actionable.append(md_file)
        else:
            non_valid.append(md_file)

    # Quiet stamp: summary + browse still see Rejected rows; live UI does not.
    stats["not_actioned"] = _stamp_non_valid_findings(non_valid, force=force)

    stats["total_findings"] = len(actionable)
    total = stats["total_findings"]
    finding_index = 0
    completed = 0
    t0 = time.monotonic()

    if non_valid:
        write_summary_file(
            stats, output_dir, run_started_at=run_started_at, coding_agent=coding_agent
        )

    for md_file in actionable:
        # Check for interrupt signal
        if _shutdown_requested:
            stats["interrupted"] = True
            break

        finding_index += 1
        pct = round(finding_index / total * 100) if total else 0
        file_path = _file_ref_for_finding(md_file)
        finding_t0 = time.monotonic()
        severity = get_severity(md_file)
        logger.info(
            "Addressing Review %d/%d (%d%%): %s",
            finding_index,
            total,
            pct,
            md_file.stem,
        )

        # Skip already-processed VALID findings unless forced
        if not force and has_action_taken(md_file):
            existing = read_action_taken(md_file)
            if existing and existing.status in ("completed", "skipped"):
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
                completed += 1
                write_summary_file(
                    stats, output_dir, run_started_at=run_started_at, coding_agent=coding_agent
                )
                _emit_result(
                    on_result,
                    completed=completed,
                    total=total,
                    finding_id=md_file.stem,
                    file_path=file_path,
                    outcome="restored",
                    summary=f"already {existing.status}",
                    commit_sha=existing.commit_sha,
                    t0=t0,
                    stats=stats,
                    severity=severity,
                    duration_s=time.monotonic() - finding_t0,
                )
                continue
            elif existing and existing.status == "error" and skip_errors:
                logger.info(
                    "Skipping errored finding (--skip-errors): %s",
                    md_file.name,
                )
                logger.info("  -> Skipped (previous error)")
                stats["skipped"] += 1
                completed += 1
                write_summary_file(
                    stats, output_dir, run_started_at=run_started_at, coding_agent=coding_agent
                )
                _emit_result(
                    on_result,
                    completed=completed,
                    total=total,
                    finding_id=md_file.stem,
                    file_path=file_path,
                    outcome="skipped",
                    summary="previous error (--skip-errors)",
                    commit_sha=None,
                    t0=t0,
                    stats=stats,
                    severity=severity,
                    duration_s=time.monotonic() - finding_t0,
                )
                continue
            elif existing:
                logger.info(
                    "Replaying %s finding: %s",
                    existing.status,
                    md_file.name,
                )

        if min_severity is not None and not severity_meets_floor(severity, min_severity):
            skip_msg = (
                f"Skipped: below severity floor "
                f"({severity or 'unknown'} < {min_severity})"
            )
            logger.info("%s: %s", md_file.name, skip_msg)
            write_action_taken(
                md_file,
                FindingStatus(
                    status="skipped",
                    timestamp=_now_iso(),
                    summary=skip_msg,
                ),
            )
            stats["skipped"] += 1
            completed += 1
            write_summary_file(
                stats, output_dir, run_started_at=run_started_at, coding_agent=coding_agent
            )
            _emit_result(
                on_result,
                completed=completed,
                total=total,
                finding_id=md_file.stem,
                file_path=file_path,
                outcome="skipped",
                summary=skip_msg,
                commit_sha=None,
                t0=t0,
                stats=stats,
                severity=severity,
                duration_s=time.monotonic() - finding_t0,
            )
            continue

        stats["processed"] += 1

        status, committed, error = _process_single_finding(
            md_file,
            agent,
            max_retries,
            retry_delay,
            dry_run,
            harness_config,
            skip_llm_checks=skip_llm_checks,
            quality_checks_override=quality_checks_override,
            on_phase=on_phase,
            completed=completed,
            total=total,
            t0=t0,
            stats=stats,
        )

        action = read_action_taken(md_file)
        commit_sha = action.commit_sha if action else None
        summary = (action.summary if action else None) or error or status

        if status == "error":
            logger.error("%s", error or f"Unknown error processing {md_file.name}")
            logger.info("  -> Error: %s", error or "unknown error")
            stats["errors"] += 1
            outcome = "error"
        elif status == "interrupted":
            stats["interrupted"] = True
            outcome = "interrupted"
        elif status == "skipped":
            logger.warning("%s", error or f"Skipped {md_file.name}")
            logger.info("  -> Skipped (no change committed)")
            stats["skipped"] += 1
            outcome = "skipped"
        elif committed:
            logger.info("  -> Change applied and committed")
            stats["committed"] += 1
            outcome = "dry-run" if dry_run else "completed"
        else:
            outcome = status

        completed += 1
        write_summary_file(
            stats, output_dir, run_started_at=run_started_at, coding_agent=coding_agent
        )
        _emit_result(
            on_result,
            completed=completed,
            total=total,
            finding_id=md_file.stem,
            file_path=file_path,
            outcome=outcome,
            summary=summary,
            commit_sha=commit_sha,
            t0=t0,
            stats=stats,
            severity=severity,
            duration_s=time.monotonic() - finding_t0,
        )

        if status == "interrupted":
            break

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
    parser.add_argument(
        "--min-severity",
        choices=MIN_SEVERITY_CHOICES,
        default=None,
        help=(
            "Skip VALID findings below this OCR severity "
            "(low, medium, high). Unset = no severity floor"
        ),
    )
    tui_group = parser.add_mutually_exclusive_group()
    tui_group.add_argument(
        "--tui",
        action="store_true",
        help="Force the interactive full-screen TUI dashboard",
    )
    tui_group.add_argument(
        "--no-tui",
        action="store_true",
        help="Force plain-text progress (disable TUI auto-detect)",
    )
    return parser.parse_args(argv)


def _force_tui_from_args(args: argparse.Namespace) -> bool | None:
    """Map ``--tui`` / ``--no-tui`` to a force flag for :func:`should_use_tui`."""
    if getattr(args, "tui", False):
        return True
    if getattr(args, "no_tui", False):
        return False
    return None


def _escape_table_cell(value: str) -> str:
    """Escape characters that would break a markdown table cell."""
    return value.replace("|", "\\|").replace("\n", " ")


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

        finding_id = md_file.stem
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
    stats: dict[str, int],
    output_dir: Path,
    run_stats: RunStats | None = None,
    *,
    run_started_at: str,
    coding_agent: str,
) -> None:
    """Append this run's counters + per-finding table to review-action_summary.md.

    Each run's block is wrapped in an HTML comment marker keyed by
    run_started_at. Prior runs' blocks (everything before the marker) are
    preserved verbatim; this run's own block is rebuilt fresh on every call
    (once per finding) so a crash mid-run still leaves an accurate partial
    record, without duplicating the block on every write.
    """
    summary_file = output_dir / "review-action_summary.md"
    marker = f"<!-- review-action-run: {run_started_at} -->"

    prior_runs = ""
    if summary_file.exists():
        existing = summary_file.read_text(encoding="utf-8")
        prior_runs = existing.partition(marker)[0]
        if prior_runs:
            prior_runs = prior_runs.rstrip("\n") + "\n\n"

    lines = [
        marker,
        "# Review Action Summary",
        "",
        f"Run started:  {run_started_at}",
        f"Coding agent: {coding_agent}",
        "",
        f"Restored:   {stats['restored']}",
        f"Processed:  {stats['processed']}",
        f"Committed:  {stats['committed']}",
        f"Skipped:    {stats['skipped']}",
        f"Errors:     {stats['errors']}",
    ]
    not_actioned = int(stats.get("not_actioned", 0))
    if not_actioned:
        lines.append(f"Not actioned (non-VALID): {not_actioned}")
    if run_stats is not None:
        lines.append(
            f"Total cost: ${run_stats.total_cost_usd:.4f} "
            f"across {run_stats.num_calls} agent call(s)"
        )
    lines.append(f"Interrupted: {'yes' if stats['interrupted'] else 'no'}")
    processed = stats["processed"]
    total = stats["total_findings"]
    lines.append(f"Progress: {processed} out of {total} findings processed")
    lines.append("")
    lines.append(build_detailed_summary(output_dir))
    lines.append("")

    with summary_file.open("w", encoding="utf-8") as f:
        f.write(prior_runs)
        f.write("\n".join(lines))


def current_run_summary_text(summary_path: Path, run_started_at: str) -> str:
    """Return this run's block from ``review-action_summary.md``.

    Prior-run blocks (everything before this run's marker) are omitted so the
    TUI done screen shows only the just-finished run.
    """
    marker = f"<!-- review-action-run: {run_started_at} -->"
    try:
        text = summary_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, sep, rest = text.partition(marker)
    if not sep:
        return text.strip()
    return (marker + rest).strip()


def print_summary(
    stats: dict[str, int],
    output_dir: Path,
    run_stats: RunStats | None = None,
    *,
    run_started_at: str | None = None,
    coding_agent: str | None = None,
) -> None:
    """Print the final processing summary and write it to file."""
    if run_started_at is None:
        run_started_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if coding_agent is None:
        coding_agent = "unknown"

    print("\n=== Review Action Harness Summary ===")
    print(f"Restored:   {stats['restored']}")
    print(f"Processed:  {stats['processed']}")
    print(f"Committed:  {stats['committed']}")
    print(f"Skipped:    {stats['skipped']}")
    print(f"Errors:     {stats['errors']}")
    not_actioned = int(stats.get("not_actioned", 0))
    if not_actioned:
        print(f"Not actioned (non-VALID): {not_actioned}")
    if run_stats is not None:
        print(
            f"Total cost: ${run_stats.total_cost_usd:.4f} "
            f"across {run_stats.num_calls} agent call(s)"
        )

    write_summary_file(
        stats, output_dir, run_stats, run_started_at=run_started_at, coding_agent=coding_agent
    )


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    args = parse_args(argv)

    # Reset shutdown flag and set up signal handler for graceful interrupt
    global _shutdown_requested
    _shutdown_requested = False
    _install_sigint_handler()

    force_tui = _force_tui_from_args(args)
    use_tui = should_use_tui(force_tui=force_tui)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    # Console handlers stay until the Textual app mounts, so early setup errors
    # remain visible. The app then detaches stream handlers and routes logs
    # into its Log pane (and review-action.log).
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

    run_started_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    coding_agent = f"{provider} ({model})" if model else provider

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

    # Count VALID findings only for RunMeta (non-VALID never appear in live UI).
    total_findings = count_actionable_findings(args.output_dir)

    meta = RunMeta(
        output_dir=args.output_dir,
        provider=provider,
        model=model,
        dry_run=args.dry_run,
        force=args.force,
        skip_errors=args.skip_errors,
        total_findings=total_findings,
        coding_agent=coding_agent,
    )

    def _run_pipeline(
        on_result: Callable[[ProgressEvent], None],
        on_phase: Callable[[ProgressEvent], None],
    ) -> dict[str, int]:
        return process_findings(
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
            min_severity=args.min_severity,
            run_started_at=run_started_at,
            coding_agent=coding_agent,
            on_result=on_result,
            on_phase=on_phase,
        )

    stats: dict[str, int]
    if use_tui:
        # Lazy import keeps plain mode free of Textual at import time.
        from deep_architect.review_action_tui import (  # noqa: PLC0415
            ActionSummaryOutputs,
            run_review_action_tui,
        )

        log_file = args.output_dir / "review-action.log"
        finalize_box: list[ActionSummaryOutputs] = []

        def _finalize(pipeline_stats: dict[str, int]) -> ActionSummaryOutputs:
            write_summary_file(
                pipeline_stats,
                args.output_dir,
                run_stats,
                run_started_at=run_started_at,
                coding_agent=coding_agent,
            )
            summary_path = args.output_dir / "review-action_summary.md"
            outputs = ActionSummaryOutputs(
                text=current_run_summary_text(summary_path, run_started_at),
                summary_path=summary_path,
            )
            finalize_box.append(outputs)
            return outputs

        tui_result = run_review_action_tui(
            meta,
            _run_pipeline,
            log_level=log_level,
            log_file=log_file,
            finalize=_finalize,
        )
        stats = tui_result.stats

        if tui_result.action == "browse" and args.output_dir.is_dir():
            from deep_architect.review_feedback_browse import (  # noqa: PLC0415
                run_feedback_browse,
            )

            return run_feedback_browse(args.output_dir, mode="action")

        if not finalize_box:
            # Force-closed or pipeline exception before finalize ran.
            print_summary(
                stats,
                args.output_dir,
                run_stats,
                run_started_at=run_started_at,
                coding_agent=coding_agent,
            )
        elif tui_result.summary_path is not None:
            print(f"Summary written to {tui_result.summary_path}")

        return 130 if stats["interrupted"] else (0 if stats["errors"] == 0 else 1)

    reporter: ProgressReporter = PlainReporter()
    reporter.start(meta)
    stats = {
        "processed": 0,
        "committed": 0,
        "skipped": 0,
        "errors": 0,
        "restored": 0,
        "not_actioned": 0,
        "total_findings": total_findings,
        "interrupted": 0,
    }
    try:
        stats = _run_pipeline(reporter.on_result, reporter.on_phase)
    except BaseException:
        reporter.finish(stats)
        raise
    else:
        reporter.finish(stats)

    print_summary(
        stats, args.output_dir, run_stats, run_started_at=run_started_at, coding_agent=coding_agent
    )

    return 130 if stats["interrupted"] else (0 if stats["errors"] == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
