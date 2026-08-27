"""Review-driver orchestrator: OCR → analyzer → action until novelty is gone."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

import git
from pydantic import BaseModel, Field

from deep_architect.action_report import SUMMARY_FILENAME, load_action_report
from deep_architect.config import HarnessConfig, _resolve_default_config_path, load_config
from deep_architect.logger import get_logger
from deep_architect.review_novelty import (
    OcrRunStats,
    StopReason,
    apply_ocr_stderr,
    consecutive_zero_novelty,
    count_high_signal_valid,
    count_ocr_comments_by_severity,
    count_valid_by_severity,
    count_verdicts,
    decide_stop,
    load_ocr_run_stats,
    parse_ocr_run_stats,
    summarize_ocr_failure,
)

logger = get_logger(__name__)

PROGRESS_FILENAME = "progress.json"
REPORT_FILENAME = "REPORT.md"
LATEST_FILENAME = "LATEST"
PASS_FOOTER = "─────────────────────────────────────────────────────"
DEFAULT_OUTPUT_DIR = Path(".review-runs")
DEFAULT_TARGET = "main"
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
MAX_RUN_SLUG_LEN = 80
_TERMINAL_RUN_STATUSES = frozenset({"converged", "max_passes"})
_RESUMABLE_RUN_STATUSES = frozenset({"running", "failed"})
DEFAULT_OCR_BIN = "ocr"
DEFAULT_OCR_TIMEOUT_SECONDS = 3600
DEFAULT_OCR_FILE_TIMEOUT_MINUTES = 10
DEFAULT_OCR_CONCURRENCY = 8
DEFAULT_OCR_LLM_TIMEOUT_SECONDS = 0
DEFAULT_OCR_AUDIENCE = "agent"
OCR_PROCESS_TIMEOUT_FILE_BUDGET = 16
OCR_PROCESS_TIMEOUT_SLACK_SECONDS = 120
DEFAULT_OCR_REPORT_EXCLUDES: tuple[str, ...] = (
    "code-review*.json",
    "code-review-*.json",
)
_OCR_START_BANNER = "OCR starting:"
_OCR_PROCESS_TIMEOUT_RE = re.compile(
    r"ocr timed out after ([0-9.]+) seconds",
    re.IGNORECASE,
)
OCR_SESSION_POLL_SECONDS = 0.25
OCR_SESSION_DISCOVER_SLACK_SECONDS = 5.0
OCR_WAIT_POLL_SECONDS = 0.25
OCR_FORCE_KILL_GRACE_SECONDS = 2.0
ACTION_MIN_SEVERITY = "medium"

_interrupt_requested = False
_force_stop_requested = False
_ocr_proc_lock = threading.Lock()
_ocr_proc: subprocess.Popen[str] | None = None


def _reset_interrupt_state() -> None:
    """Clear graceful / force-stop flags. Called at CLI start and from tests."""
    global _interrupt_requested, _force_stop_requested
    _interrupt_requested = False
    _force_stop_requested = False


def request_interrupt() -> None:
    """Request a graceful stop after the current step returns."""
    global _interrupt_requested
    _interrupt_requested = True


def request_force_stop() -> None:
    """Kill the in-flight OCR subprocess (second q / Ctrl-C).

    Also marks a graceful driver interrupt so the loop does not start the
    next phase. Safe to call from the TUI thread.
    """
    global _force_stop_requested
    _force_stop_requested = True
    request_interrupt()
    with _ocr_proc_lock:
        proc = _ocr_proc
    if proc is None:
        return
    logger.info("Force stop: signalling OCR pid %s", proc.pid)
    _kill_ocr_process(proc, force=False)


def _kill_ocr_process(proc: subprocess.Popen[str], *, force: bool) -> None:
    """Send SIGTERM (or SIGKILL if *force*) to the OCR process group."""
    if proc.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    pid = proc.pid
    if os.name == "posix" and pid:
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except OSError as exc:
            logger.warning(
                "killpg(%s, %s) failed: %s; falling back to the parent process",
                pid,
                sig,
                exc,
            )
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        return


def _sigint_handler(signum: int, frame: object) -> None:
    if _interrupt_requested:
        request_force_stop()
        logger.info("CTRL-C received again, killing current OCR subprocess...")
        return
    request_interrupt()
    logger.info(
        "CTRL-C received, finishing current step before shutdown "
        "(press Ctrl-C again to kill it)..."
    )


def _install_sigint_handler() -> None:
    """Install the SIGINT handler when running on the main thread.

    ``signal.signal`` raises ``ValueError`` off the main thread. The TUI
    worker must not reinstall this; the CLI entry point already did.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGINT, _sigint_handler)

_COST_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")
_ELAPSED_RE = re.compile(
    r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$",
    re.IGNORECASE,
)
_SLUG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_DASH_RE = re.compile(r"-{2,}")


class DriverPassRecord(BaseModel):
    pass_index: int  # 1-based
    ocr_json: str
    feedback_dir: str
    novelty: int
    valid_total: int
    ocr_severity: dict[str, int] = Field(default_factory=dict)
    valid_by_severity: dict[str, int] = Field(default_factory=dict)
    verdicts: dict[str, int] = Field(default_factory=dict)
    action_errors: int
    action_committed: int
    action_skipped: int = 0
    ocr_tokens_total: int | None = None
    ocr_elapsed_s: float | None = None
    phase_seconds: dict[str, float] = Field(default_factory=dict)
    wall_seconds: float = 0.0
    action_cost_usd: float | None = None
    status: Literal["complete", "failed"]
    failure_reason: str | None = None
    ocr_status: str | None = None


class DriverProgress(BaseModel):
    status: Literal["running", "converged", "max_passes", "failed"] = "running"
    source: str
    target: str
    source_sha: str
    target_sha: str
    max_passes: int
    k: int
    current_pass: int = 0  # last *completed* pass; 0 if none
    consecutive_zero_novelty: int = 0
    novelty_history: list[int] = Field(default_factory=list)
    passes: list[DriverPassRecord] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    output_dir: str
    stop_detail: str | None = None


class ReviewStepRunners(Protocol):
    def run_ocr(
        self, *, source: str, target: str, output_json: Path, exclude: list[str]
    ) -> int: ...

    def run_analyzer(
        self,
        *,
        ocr_json: Path,
        feedback_dir: Path,
        prior_feedback: list[Path],
        knowledge_dir: Path | None,
        exclude: list[str],
    ) -> int: ...

    def run_action(self, *, feedback_dir: Path) -> int: ...


def save_driver_progress(output_dir: Path, progress: DriverProgress) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / PROGRESS_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(progress.model_dump_json(indent=2))
    os.replace(tmp, path)
    return path


def load_driver_progress(output_dir: Path) -> DriverProgress:
    path = output_dir / PROGRESS_FILENAME
    return DriverProgress.model_validate_json(path.read_text())


def sanitize_run_slug(name: str, *, fallback: str = "") -> str:
    """Make *name* safe as a single path component.

    Empty or punctuation-only names fall back to *fallback* (truncated) or
    ``unnamed``.
    """
    slug = _normalize_run_slug(name)
    if not slug:
        slug = _normalize_run_slug(fallback)[:12]
    if not slug:
        slug = "unnamed"
    return slug[:MAX_RUN_SLUG_LEN]


def _normalize_run_slug(name: str) -> str:
    slug = _SLUG_UNSAFE_RE.sub("-", name)
    slug = _SLUG_DASH_RE.sub("-", slug)
    return slug.strip(".-")


def branch_run_parent(
    root: Path,
    source: str,
    target: str,
    *,
    source_sha: str = "",
) -> Path:
    """Directory that groups all runs of *source* (vs *target*) under *root*."""
    source_slug = sanitize_run_slug(source, fallback=source_sha)
    if target == DEFAULT_TARGET:
        return Path(root) / source_slug
    target_slug = sanitize_run_slug(target)
    return Path(root) / f"{source_slug}__{target_slug}"


def default_output_excludes(root: Path, cwd: Path) -> list[str]:
    """Glob that keeps OCR/analyzer from reviewing the output tree.

    Returns empty when *root* is the repo cwd (would hide the whole tree)
    or is outside *cwd* (OCR will not see those files as repo paths).
    """
    resolved_root = _resolve_output_dir(cwd, root)
    resolved_cwd = cwd.resolve()
    if resolved_root == resolved_cwd:
        return []
    try:
        relative = resolved_root.relative_to(resolved_cwd)
    except ValueError:
        return []
    posix = relative.as_posix()
    if posix in ("", "."):
        return []
    return [f"{posix}/**"]


def default_ocr_report_excludes() -> list[str]:
    """Globs that keep OCR from reviewing committed OCR JSON reports."""
    return list(DEFAULT_OCR_REPORT_EXCLUDES)


def _dedupe_globs(*groups: list[str]) -> list[str]:
    """Preserve order while dropping duplicate exclude globs."""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for glob in group:
            if glob in seen:
                continue
            seen.add(glob)
            out.append(glob)
    return out


def resolve_driver_run_dir(
    root: Path,
    *,
    source: str,
    target: str,
    source_sha: str,
    resume: bool,
    target_sha: str = "",
    now: datetime | None = None,
) -> tuple[Path, bool]:
    """Pick or create the directory for one driver run.

    Returns ``(run_dir, is_resume)``. ``run_dir`` is where ``progress.json``
    and per-pass artifacts live. ``is_resume`` is True when an existing run
    is being continued (including an already-terminal run that should
    no-op).

    ``root`` is the operator-facing ``--output-dir``. Nested layout is
    ``{root}/{branch}/{timestamp}/``. A legacy flat ``{root}/progress.json``
    is resumed in place; ``--no-resume`` against that layout creates a
    nested sibling so the flat files are not overwritten.
    """
    root = Path(root)
    stamp = _utc_stamp(now)
    flat_progress = root / PROGRESS_FILENAME
    if flat_progress.is_file():
        return _resolve_legacy_flat_run(
            root,
            source=source,
            target=target,
            source_sha=source_sha,
            target_sha=target_sha,
            resume=resume,
            stamp=stamp,
        )

    parent = branch_run_parent(root, source, target, source_sha=source_sha)
    if resume:
        existing = _newest_matching_run(parent, source, target)
        if existing is not None:
            progress = load_driver_progress(existing)
            if _should_reuse_existing_run(
                progress, source_sha=source_sha, target_sha=target_sha
            ):
                _write_latest(parent, existing.name)
                return existing, True

    return _create_new_run_dir(parent, stamp), False


def _resolve_legacy_flat_run(
    root: Path,
    *,
    source: str,
    target: str,
    source_sha: str,
    target_sha: str,
    resume: bool,
    stamp: str,
) -> tuple[Path, bool]:
    if resume:
        progress = load_driver_progress(root)
        if progress.source != source or progress.target != target:
            # Let run_driver raise the existing mismatch error.
            return root, True
        if _should_reuse_existing_run(
            progress, source_sha=source_sha, target_sha=target_sha
        ):
            return root, True
    parent = branch_run_parent(root, source, target, source_sha=source_sha)
    return _create_new_run_dir(parent, stamp), False


def _should_reuse_existing_run(
    progress: DriverProgress, *, source_sha: str, target_sha: str
) -> bool:
    if progress.status in _RESUMABLE_RUN_STATUSES:
        return True
    if progress.status not in _TERMINAL_RUN_STATUSES:
        return False
    return _shas_unchanged(progress, source_sha=source_sha, target_sha=target_sha)


def _shas_unchanged(
    progress: DriverProgress, *, source_sha: str, target_sha: str
) -> bool:
    if source_sha and progress.source_sha and progress.source_sha != source_sha:
        return False
    if target_sha and progress.target_sha and progress.target_sha != target_sha:
        return False
    return True


def _newest_matching_run(parent: Path, source: str, target: str) -> Path | None:
    for run_dir in reversed(_list_run_dirs(parent)):
        progress = _try_load_progress(run_dir)
        if progress is None:
            continue
        if progress.source == source and progress.target == target:
            return run_dir
    return None


def _list_run_dirs(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    runs = [
        child
        for child in parent.iterdir()
        if child.is_dir() and (child / PROGRESS_FILENAME).is_file()
    ]
    return sorted(runs, key=lambda path: path.name)


def _try_load_progress(run_dir: Path) -> DriverProgress | None:
    try:
        return load_driver_progress(run_dir)
    except Exception as exc:
        logger.warning("Skipping unreadable run state %s: %s", run_dir, exc)
        return None


def _create_new_run_dir(parent: Path, stamp: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = parent / stamp
    suffix = 2
    while run_dir.exists():
        run_dir = parent / f"{stamp}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)
    _write_latest(parent, run_dir.name)
    return run_dir


def _write_latest(parent: Path, run_id: str) -> None:
    path = parent / LATEST_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{run_id}\n", encoding="utf-8")
    os.replace(tmp, path)


def _utc_stamp(now: datetime | None) -> str:
    if now is None:
        stamp = datetime.now(UTC)
    elif now.tzinfo is None:
        stamp = now.replace(tzinfo=UTC)
    else:
        stamp = now.astimezone(UTC)
    return stamp.strftime(RUN_ID_FORMAT)


def format_duration(seconds: float) -> str:
    """Compact wall-clock: ``12s``, ``4m01s``, ``1h02m03s``."""
    total = int(round(max(seconds, 0.0)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def parse_action_cost_usd(cost_line: str | None) -> float | None:
    """Parse ``Total cost: $0.12 …``; missing or unparseable → None."""
    if not cost_line:
        return None
    match = _COST_RE.search(cost_line)
    if match is None:
        return None
    return float(match.group(1))


def elapsed_to_seconds(elapsed: int | float | str | None) -> float | None:
    """Coerce an OCR ``summary.elapsed`` value to seconds."""
    if elapsed is None:
        return None
    if isinstance(elapsed, int | float):
        return float(elapsed)
    text = elapsed.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    match = _ELAPSED_RE.fullmatch(text)
    if match is None or not any(match.groups()):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    secs = float(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + secs


def format_pass_fraction(pass_index: int, max_passes: int) -> str:
    """``1/5`` or ``1/∞`` when *max_passes* is 0 (unlimited)."""
    if max_passes <= 0:
        return f"{pass_index}/∞"
    return f"{pass_index}/{max_passes}"


def format_pass_header(pass_index: int, max_passes: int) -> str:
    return (
        f"── Pass {format_pass_fraction(pass_index, max_passes)} "
        f"─────────────────────────────────────────"
    )


def format_ocr_summary(
    stats: OcrRunStats,
    severity: Mapping[str, int],
    wall_seconds: float,
) -> str:
    """One OCR phase line. Omits the word ``tokens`` when total_tokens is None."""
    status = (stats.status or "").lower()
    if status == "failed":
        prefix = "OCR      FAILED   "
    elif status == "partial":
        prefix = "OCR      PARTIAL  "
    else:
        prefix = "OCR      "
    comments = stats.comments
    if comments is None:
        comments = sum(severity.values())
    parts: list[str] = [f"comments {comments}"]
    if stats.files_reviewed is not None:
        parts.append(f"files {stats.files_reviewed}")
    if stats.files_failed is not None:
        parts.append(f"{stats.files_failed} failed")
    parts.append(f"high {severity.get('high', 0)}")
    parts.append(f"med {severity.get('medium', 0)}")
    parts.append(f"low {severity.get('low', 0)}")
    if status == "failed":
        reason = summarize_ocr_failure(stats)
        if reason:
            parts.append(reason)
    elif stats.timeout_failures:
        parts.append(f"{stats.timeout_failures} LLM timeouts")
    elif stats.failed_requests:
        parts.append(f"{stats.failed_requests} LLM failures")
    if stats.total_tokens is not None:
        token_bit = f"tokens {stats.total_tokens}"
        if stats.input_tokens is not None or stats.output_tokens is not None:
            inn = stats.input_tokens if stats.input_tokens is not None else 0
            out = stats.output_tokens if stats.output_tokens is not None else 0
            token_bit += f" (in {inn} / out {out})"
        parts.append(token_bit)
    if stats.cache_read_tokens is not None:
        parts.append(f"cache {stats.cache_read_tokens}")
    elapsed_s = (
        elapsed_to_seconds(stats.elapsed) if not isinstance(stats.elapsed, str) else None
    )
    if isinstance(stats.elapsed, str) and stats.elapsed.strip():
        parts.append(f"elapsed {stats.elapsed.strip()}")
    elif elapsed_s is not None:
        parts.append(f"elapsed {format_duration(elapsed_s)}")
    parts.append(format_duration(wall_seconds))
    return prefix + "  ".join(parts)


def format_analyzer_summary(
    verdicts: Mapping[str, int],
    valid_by_severity: Mapping[str, int],
    wall_seconds: float,
) -> str:
    valid = verdicts.get("VALID", 0)
    high = valid_by_severity.get("high", 0)
    medium = valid_by_severity.get("medium", 0)
    low = valid_by_severity.get("low", 0)
    parts = [
        f"VALID {valid} (H{high} M{medium} L{low})",
        f"BACKLOG {verdicts.get('BACKLOG', 0)}",
        f"REJECTED {verdicts.get('REJECTED', 0)}",
        f"DUP {verdicts.get('DUPLICATE', 0)}",
        f"TIMEOUT {verdicts.get('TIMEOUT', 0)}",
        format_duration(wall_seconds),
    ]
    return "Analyzer " + "  ".join(parts)


def format_action_summary(
    committed: int,
    skipped: int,
    errors: int,
    cost_usd: float | None,
    wall_seconds: float,
) -> str:
    parts = [
        f"committed {committed}",
        f"skipped {skipped}",
        f"errors {errors}",
    ]
    if cost_usd is not None:
        parts.append(f"${cost_usd:.2f}")
    parts.append(format_duration(wall_seconds))
    return "Action   " + "  ".join(parts)


def format_pass_rollup(
    pass_index: int,
    novelty: int,
    zeros: int,
    k: int,
    wall_seconds: float,
) -> str:
    return (
        f"Pass {pass_index}   novelty={novelty}  zeros={zeros}/{k}  "
        f"wall={format_duration(wall_seconds)}"
    )


def format_trend(previous: DriverPassRecord, current: DriverPassRecord) -> str:
    return (
        f"Trend    novelty {previous.novelty}→{current.novelty}  "
        f"high {previous.ocr_severity.get('high', 0)}→{current.ocr_severity.get('high', 0)}  "
        f"med {previous.ocr_severity.get('medium', 0)}→{current.ocr_severity.get('medium', 0)}  "
        f"low {previous.ocr_severity.get('low', 0)}→{current.ocr_severity.get('low', 0)}  "
        f"VALID {previous.valid_total}→{current.valid_total}"
    )


def format_stop_line(status: str, k: int, detail: str | None = None) -> str:
    if status == "converged":
        return f"Converged (K={k})."
    if status == "max_passes":
        return "Stopped: max-passes with novelty remaining."
    if status == "failed":
        if detail:
            return f"Stopped: failed — {detail}"
        return "Stopped: failed."
    return f"Stopped: {status}."


def format_pass_table(progress: DriverProgress) -> str:
    """Plain-text pass table printed at the end and embedded in REPORT.md."""
    header = (
        "Pass  Novelty  High  Med  Low  VALID  Committed  Errors  "
        "Tokens  Wall     Artifacts"
    )
    lines = [header]
    for record in progress.passes:
        tokens = (
            str(record.ocr_tokens_total) if record.ocr_tokens_total is not None else "—"
        )
        artifacts = f"{Path(record.ocr_json).name}, {Path(record.feedback_dir).name}/"
        lines.append(
            f"{record.pass_index:<4}  "
            f"{record.novelty:<7}  "
            f"{record.ocr_severity.get('high', 0):<4}  "
            f"{record.ocr_severity.get('medium', 0):<3}  "
            f"{record.ocr_severity.get('low', 0):<3}  "
            f"{record.valid_total:<5}  "
            f"{record.action_committed:<9}  "
            f"{record.action_errors:<6}  "
            f"{tokens:<6}  "
            f"{format_duration(record.wall_seconds):<8}  "
            f"{artifacts}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class DriverRunMeta:
    """Immutable run metadata shown in the progress header."""

    source: str
    target: str
    source_sha: str
    target_sha: str
    max_passes: int
    k: int
    output_dir: Path
    resume: bool = False


class ProgressReporter(Protocol):
    """Progress sink used by the driver loop (plain text or TUI)."""

    def start(self, meta: DriverRunMeta) -> None:
        """Called once before the first pass."""

    def phase_start(self, pass_index: int, max_passes: int, phase: str) -> None:
        """Called when a phase (ocr / analyzer / action) begins."""

    def phase_done(self, line: str) -> None:
        """Called with a formatted phase-summary line."""

    def pass_done(
        self, rollup: str, trend: str | None, progress: DriverProgress
    ) -> None:
        """Called after a completed pass rollup (and optional trend)."""

    def finish(self, progress: DriverProgress) -> None:
        """Called once when the loop stops (converged, max-passes, or failed)."""


class PlainReporter:
    """Plain-text progress reporter for non-interactive terminals and CI."""

    def start(self, meta: DriverRunMeta) -> None:
        _ = meta

    def phase_start(self, pass_index: int, max_passes: int, phase: str) -> None:
        if phase == "ocr":
            print(format_pass_header(pass_index, max_passes))
            print("OCR starting…")
            print()
        elif phase == "analyzer":
            print("Analyzer starting…")
        elif phase == "action":
            print("Action starting…")

    def phase_done(self, line: str) -> None:
        print(line)

    def pass_done(
        self, rollup: str, trend: str | None, progress: DriverProgress
    ) -> None:
        _ = progress
        print()
        print(rollup)
        if trend is not None:
            print(trend)
        print(PASS_FOOTER)

    def finish(self, progress: DriverProgress) -> None:
        print(format_stop_line(progress.status, progress.k, progress.stop_detail))
        print()
        print(format_pass_table(progress))


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


def _force_tui_from_args(args: argparse.Namespace) -> bool | None:
    """Map ``--tui`` / ``--no-tui`` to a force flag for :func:`should_use_tui`."""
    if getattr(args, "tui", False):
        return True
    if getattr(args, "no_tui", False):
        return False
    return None


def write_driver_report(output_dir: Path, progress: DriverProgress) -> Path:
    """Refresh REPORT.md from *progress* so a crash still has a partial report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = [
        "# Review Driver Report",
        "",
        f"- Source: `{progress.source}` (`{progress.source_sha or 'unresolved'}`)",
        f"- Target: `{progress.target}` (`{progress.target_sha or 'unresolved'}`)",
        f"- K: {progress.k}",
        f"- Max passes: {'unlimited' if progress.max_passes <= 0 else progress.max_passes}",
        f"- Stop reason: {progress.status}"
        + (f" — {progress.stop_detail}" if progress.stop_detail else ""),
        f"- Novelty history: {progress.novelty_history}",
        "",
        "Stop is the count of high/medium VALID findings, **not** the OCR comment count.",
        "",
    ]
    for record in progress.passes:
        blocks.append(f"## Pass {record.pass_index}")
        blocks.append("")
        blocks.append(format_pass_header(record.pass_index, progress.max_passes))
        ocr_path = Path(record.ocr_json)
        if ocr_path.is_file():
            ocr_stats = load_ocr_run_stats(ocr_path)
        else:
            ocr_stats = OcrRunStats(
                comments=sum(record.ocr_severity.values()) or None,
                total_tokens=record.ocr_tokens_total,
                elapsed=record.ocr_elapsed_s,
                status=record.ocr_status,
                message=record.failure_reason,
            )
        blocks.append(
            format_ocr_summary(
                ocr_stats,
                record.ocr_severity,
                record.phase_seconds.get("ocr", 0.0),
            )
        )
        blocks.append(
            format_analyzer_summary(
                record.verdicts,
                record.valid_by_severity,
                record.phase_seconds.get("analyzer", 0.0),
            )
        )
        blocks.append(
            format_action_summary(
                record.action_committed,
                record.action_skipped,
                record.action_errors,
                record.action_cost_usd,
                record.phase_seconds.get("action", 0.0),
            )
        )
        blocks.append(
            format_pass_rollup(
                record.pass_index,
                record.novelty,
                # zeros-at-this-pass: trailing zeros in history[:pass_index]
                consecutive_zero_novelty(progress.novelty_history[: record.pass_index]),
                progress.k,
                record.wall_seconds,
            )
        )
        prev = _previous_complete(progress, record.pass_index)
        if prev is not None and record.status == "complete":
            blocks.append(format_trend(prev, record))
        blocks.append(PASS_FOOTER)
        blocks.append("")
    blocks.extend(["## Summary", "", format_pass_table(progress), ""])
    path = output_dir / REPORT_FILENAME
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _previous_complete(
    progress: DriverProgress, pass_index: int
) -> DriverPassRecord | None:
    prior = [
        p
        for p in progress.passes
        if p.pass_index < pass_index and p.status == "complete"
    ]
    return prior[-1] if prior else None


def _load_action_pass_stats(
    feedback_dir: Path, action_rc: int
) -> tuple[int, int, int, float | None]:
    """Return (committed, skipped, errors, cost_usd)."""
    committed = 0
    skipped = 0
    errors = 0
    cost_usd: float | None = None
    if (feedback_dir / SUMMARY_FILENAME).is_file():
        report = load_action_report(feedback_dir)
        if report.latest_run is not None:
            committed = report.latest_run.committed
            skipped = report.latest_run.skipped
            errors = report.latest_run.errors
            cost_usd = parse_action_cost_usd(report.latest_run.cost_line)
    if action_rc == 1 and errors == 0:
        errors = 1
    return committed, skipped, errors, cost_usd


def _fail_pass(
    progress: DriverProgress,
    output_dir: Path,
    *,
    pass_index: int,
    ocr_json: Path,
    feedback_dir: Path,
    phase_seconds: dict[str, float],
    wall_seconds: float,
    action_errors: int = 0,
    reporter: ProgressReporter | None = None,
    failure_reason: str | None = None,
    ocr_status: str | None = None,
    ocr_tokens_total: int | None = None,
    ocr_elapsed_s: float | None = None,
) -> DriverProgress:
    progress.passes.append(
        DriverPassRecord(
            pass_index=pass_index,
            ocr_json=str(ocr_json),
            feedback_dir=str(feedback_dir),
            novelty=0,
            valid_total=0,
            action_errors=action_errors,
            action_committed=0,
            ocr_tokens_total=ocr_tokens_total,
            ocr_elapsed_s=ocr_elapsed_s,
            phase_seconds=phase_seconds,
            wall_seconds=wall_seconds,
            status="failed",
            failure_reason=failure_reason,
            ocr_status=ocr_status,
        )
    )
    progress.status = "failed"
    progress.stop_detail = failure_reason
    save_driver_progress(output_dir, progress)
    write_driver_report(output_dir, progress)
    sink: ProgressReporter = reporter if reporter is not None else PlainReporter()
    sink.finish(progress)
    return progress


def run_driver(
    *,
    source: str,
    target: str,
    output_dir: Path,
    runners: ReviewStepRunners,
    max_passes: int,
    k: int,
    resume: bool = True,
    knowledge_dir: Path | None = None,
    exclude: list[str] | None = None,
    source_sha: str = "",
    target_sha: str = "",
    reporter: ProgressReporter | None = None,
) -> DriverProgress:
    """Run OCR → analyzer → action until stop predicates fire.

    All external tools go through *runners*. Mid-pass crash is not recorded as
    a completed pass; resume restarts that pass and overwrites artifacts.
    Resume is on by default when ``progress.json`` exists; pass ``resume=False``
    (``--no-resume``) to start a new run.
    """
    exclude_globs = list(exclude) if exclude is not None else []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sink: ProgressReporter = reporter if reporter is not None else PlainReporter()
    progress_path = output_dir / PROGRESS_FILENAME

    actually_resuming = False
    already_done = False
    start = 1
    if resume and progress_path.is_file():
        progress = load_driver_progress(output_dir)
        if progress.source != source or progress.target != target:
            raise ValueError(
                f"Resume state is for {progress.source} → {progress.target}, "
                f"not {source} → {target}. Pass --no-resume to start a new run."
            )
        actually_resuming = True
        if progress.status in ("converged", "max_passes"):
            already_done = True
        else:
            progress.status = "running"
            start = progress.current_pass + 1
    else:
        progress = DriverProgress(
            source=source,
            target=target,
            source_sha=source_sha,
            target_sha=target_sha,
            max_passes=max_passes,
            k=k,
            output_dir=str(output_dir),
        )

    sink.start(
        DriverRunMeta(
            source=source,
            target=target,
            source_sha=source_sha,
            target_sha=target_sha,
            max_passes=max_passes,
            k=k,
            output_dir=output_dir,
            resume=actually_resuming,
        )
    )
    if already_done:
        sink.finish(progress)
        return progress

    save_driver_progress(output_dir, progress)

    try:
        pass_index = start
        while True:
            if max_passes > 0 and pass_index > max_passes:
                break
            progress.passes = [p for p in progress.passes if p.pass_index != pass_index]
            ocr_json = output_dir / f"code-review-r{pass_index}.json"
            feedback_dir = output_dir / f"feedback-r{pass_index}"
            prior = [
                output_dir / f"feedback-r{i}"
                for i in range(1, pass_index)
                if (output_dir / f"feedback-r{i}").is_dir()
            ]

            if _interrupt_requested:
                return _fail_pass(
                    progress,
                    output_dir,
                    pass_index=pass_index,
                    ocr_json=ocr_json,
                    feedback_dir=feedback_dir,
                    phase_seconds={},
                    wall_seconds=0.0,
                    reporter=sink,
                )

            sink.phase_start(pass_index, max_passes, "ocr")
            t_ocr = time.monotonic()
            ocr_rc = runners.run_ocr(
                source=source,
                target=target,
                output_json=ocr_json,
                exclude=exclude_globs,
            )
            ocr_wall = time.monotonic() - t_ocr
            if ocr_rc == 0 and _interrupt_requested:
                ocr_rc = 130
            if ocr_rc != 0:
                stderr_tail = _ocr_log_text(output_dir, pass_index)
                process_timeout = ocr_process_timeout_reason(stderr_tail)
                interrupted = ocr_rc == 130 or _interrupt_requested
                if process_timeout:
                    # Ignore leftover JSON from a previous attempt of this pass.
                    ocr_stats = OcrRunStats(status="failed", message=process_timeout)
                    reason = process_timeout
                    logger.error(
                        "OCR failed on pass %s (rc=%s): %s",
                        pass_index,
                        ocr_rc,
                        reason,
                    )
                elif interrupted:
                    ocr_stats = apply_ocr_stderr(
                        load_ocr_run_stats(ocr_json), stderr_tail
                    )
                    if ocr_stats.status is None:
                        ocr_stats = replace(ocr_stats, status="failed")
                    reason = "interrupted"
                    logger.info(
                        "OCR interrupted on pass %s (rc=%s)", pass_index, ocr_rc
                    )
                else:
                    ocr_stats = apply_ocr_stderr(
                        load_ocr_run_stats(ocr_json), stderr_tail
                    )
                    if ocr_stats.status is None:
                        ocr_stats = replace(ocr_stats, status="failed")
                    reason = summarize_ocr_failure(ocr_stats, stderr_tail, rc=ocr_rc)
                    logger.error(
                        "OCR failed on pass %s (rc=%s): %s",
                        pass_index,
                        ocr_rc,
                        reason,
                    )
                sink.phase_done(format_ocr_summary(ocr_stats, {}, ocr_wall))
                return _fail_pass(
                    progress,
                    output_dir,
                    pass_index=pass_index,
                    ocr_json=ocr_json,
                    feedback_dir=feedback_dir,
                    phase_seconds={"ocr": ocr_wall},
                    wall_seconds=ocr_wall,
                    reporter=sink,
                    failure_reason=reason,
                    ocr_status=ocr_stats.status or "failed",
                    ocr_tokens_total=ocr_stats.total_tokens,
                    ocr_elapsed_s=elapsed_to_seconds(ocr_stats.elapsed),
                )

            ocr_stats = parse_ocr_run_stats(ocr_json)
            ocr_severity = count_ocr_comments_by_severity(ocr_json)
            sink.phase_done(format_ocr_summary(ocr_stats, ocr_severity, ocr_wall))
            if (ocr_stats.status or "").lower() == "partial" or (
                ocr_stats.failed_requests or 0
            ) > 0:
                logger.warning(
                    "OCR partial on pass %s: %s",
                    pass_index,
                    summarize_ocr_failure(ocr_stats),
                )

            sink.phase_start(pass_index, max_passes, "analyzer")
            t_an = time.monotonic()
            analyzer_rc = runners.run_analyzer(
                ocr_json=ocr_json,
                feedback_dir=feedback_dir,
                prior_feedback=prior,
                knowledge_dir=knowledge_dir,
                exclude=exclude_globs,
            )
            analyzer_wall = time.monotonic() - t_an
            if analyzer_rc == 0 and _interrupt_requested:
                analyzer_rc = 130
            if analyzer_rc != 0:
                logger.error(
                    "Analyzer failed on pass %s (rc=%s)", pass_index, analyzer_rc
                )
                return _fail_pass(
                    progress,
                    output_dir,
                    pass_index=pass_index,
                    ocr_json=ocr_json,
                    feedback_dir=feedback_dir,
                    phase_seconds={"ocr": ocr_wall, "analyzer": analyzer_wall},
                    wall_seconds=ocr_wall + analyzer_wall,
                    reporter=sink,
                )

            verdicts = count_verdicts(feedback_dir)
            valid_by_sev = count_valid_by_severity(feedback_dir)
            sink.phase_done(
                format_analyzer_summary(verdicts, valid_by_sev, analyzer_wall)
            )

            sink.phase_start(pass_index, max_passes, "action")
            t_act = time.monotonic()
            action_rc = runners.run_action(feedback_dir=feedback_dir)
            action_wall = time.monotonic() - t_act
            if action_rc != 130 and _interrupt_requested:
                action_rc = 130
            if action_rc == 130:
                logger.error("Action interrupted on pass %s", pass_index)
                committed, skipped, errors, cost_usd = _load_action_pass_stats(
                    feedback_dir, action_rc
                )
                return _fail_pass(
                    progress,
                    output_dir,
                    pass_index=pass_index,
                    ocr_json=ocr_json,
                    feedback_dir=feedback_dir,
                    phase_seconds={
                        "ocr": ocr_wall,
                        "analyzer": analyzer_wall,
                        "action": action_wall,
                    },
                    wall_seconds=ocr_wall + analyzer_wall + action_wall,
                    action_errors=errors,
                    reporter=sink,
                )

            committed, skipped, errors, cost_usd = _load_action_pass_stats(
                feedback_dir, action_rc
            )
            sink.phase_done(
                format_action_summary(
                    committed, skipped, errors, cost_usd, action_wall
                )
            )

            novelty = count_high_signal_valid(feedback_dir)
            valid_total = verdicts.get("VALID", 0)
            wall = ocr_wall + analyzer_wall + action_wall
            record = DriverPassRecord(
                pass_index=pass_index,
                ocr_json=str(ocr_json),
                feedback_dir=str(feedback_dir),
                novelty=novelty,
                valid_total=valid_total,
                ocr_severity=dict(ocr_severity),
                valid_by_severity=dict(valid_by_sev),
                verdicts=dict(verdicts),
                action_errors=errors,
                action_committed=committed,
                action_skipped=skipped,
                ocr_tokens_total=ocr_stats.total_tokens,
                ocr_elapsed_s=elapsed_to_seconds(ocr_stats.elapsed),
                phase_seconds={
                    "ocr": ocr_wall,
                    "analyzer": analyzer_wall,
                    "action": action_wall,
                },
                wall_seconds=wall,
                action_cost_usd=cost_usd,
                status="complete",
                ocr_status=ocr_stats.status,
            )
            progress.novelty_history.append(novelty)
            progress.passes.append(record)
            progress.consecutive_zero_novelty = consecutive_zero_novelty(
                progress.novelty_history
            )
            progress.current_pass = pass_index

            previous = _previous_complete(progress, pass_index)
            sink.pass_done(
                format_pass_rollup(
                    pass_index,
                    novelty,
                    progress.consecutive_zero_novelty,
                    k,
                    wall,
                ),
                format_trend(previous, record) if previous is not None else None,
                progress,
            )

            reason = decide_stop(
                novelty_history=progress.novelty_history,
                k=k,
                max_passes=max_passes,
            )
            if reason is StopReason.CONVERGED:
                progress.status = "converged"
            elif reason is StopReason.MAX_PASSES:
                progress.status = "max_passes"

            save_driver_progress(output_dir, progress)
            write_driver_report(output_dir, progress)

            if reason is not StopReason.CONTINUE:
                break
            pass_index += 1
    except Exception:
        logger.exception("Review driver failed")
        progress.status = "failed"
        save_driver_progress(output_dir, progress)
        write_driver_report(output_dir, progress)
        raise

    sink.finish(progress)
    return progress


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class DriverPreflightError(Exception):
    """Fatal preflight failure; ``main()`` maps this to stderr + exit 1."""


def _resolve_output_dir(cwd: Path, output_dir: Path) -> Path:
    if output_dir.is_absolute():
        return output_dir.resolve()
    return (cwd / output_dir).resolve()


def _path_under_output(rel_path: str, repo_root: Path, output_dir: Path) -> bool:
    abs_path = (repo_root / rel_path).resolve()
    output_resolved = _resolve_output_dir(repo_root, output_dir)
    try:
        abs_path.relative_to(output_resolved)
        return True
    except ValueError:
        return False


def _dirty_tracked_paths(repo: git.Repo) -> list[str]:
    paths: set[str] = set()
    for diff in repo.index.diff(None):
        if diff.a_path:
            paths.add(os.fspath(diff.a_path))
        if diff.b_path:
            paths.add(os.fspath(diff.b_path))
    try:
        for diff in repo.index.diff("HEAD"):
            if diff.a_path:
                paths.add(os.fspath(diff.a_path))
            if diff.b_path:
                paths.add(os.fspath(diff.b_path))
    except git.BadName:
        for entry_key in repo.index.entries:
            raw = entry_key[0] if isinstance(entry_key, tuple) else entry_key
            paths.add(os.fspath(raw))
    return sorted(paths)


def preflight_driver(
    *,
    cwd: Path,
    source: str,
    target: str,
    output_dir: Path,
    ocr_bin: str,
) -> tuple[git.Repo, str, str]:
    """Return (repo, source_sha, target_sha). Raise DriverPreflightError on failure."""
    try:
        repo = git.Repo(str(cwd), search_parent_directories=True)
    except git.InvalidGitRepositoryError as exc:
        raise DriverPreflightError(f"Not a git repository: {cwd}") from exc

    if shutil.which(ocr_bin) is None:
        raise DriverPreflightError(
            f"{ocr_bin!r} not found on PATH. Install the OpenCodeReview `ocr` CLI."
        )

    try:
        source_sha = repo.commit(source).hexsha
    except Exception as exc:
        raise DriverPreflightError(
            f"--source {source!r} does not resolve to a commit"
        ) from exc

    try:
        target_sha = repo.commit(target).hexsha
    except Exception as exc:
        raise DriverPreflightError(
            f"--target {target!r} does not resolve to a commit"
        ) from exc

    head_sha = repo.head.commit.hexsha
    if head_sha != source_sha:
        raise DriverPreflightError(
            f"HEAD ({head_sha[:12]}) is not --source {source} ({source_sha[:12]}). "
            "Check out --source first; the driver does not checkout."
        )

    repo_root = Path(repo.working_dir)
    dirty = [
        path
        for path in _dirty_tracked_paths(repo)
        if not _path_under_output(path, repo_root, output_dir)
    ]
    if dirty:
        raise DriverPreflightError(
            "Dirty tracked files outside the output directory: " + ", ".join(dirty)
        )

    return repo, source_sha, target_sha


# ---------------------------------------------------------------------------
# Production runners
# ---------------------------------------------------------------------------


def ocr_process_timeout_seconds(
    *,
    file_timeout_minutes: int,
    concurrency: int,
    file_budget: int = OCR_PROCESS_TIMEOUT_FILE_BUDGET,
    slack_seconds: int = OCR_PROCESS_TIMEOUT_SLACK_SECONDS,
) -> float:
    """Safety cap so the driver does not kill OCR before per-file timeouts fire.

    Worst case: every file in *file_budget* uses the full ``--timeout``,
    serialized into ``ceil(budget / concurrency)`` batches.
    """
    conc = max(1, concurrency)
    budget = max(1, file_budget)
    batches = math.ceil(budget / conc)
    return float(batches * max(1, file_timeout_minutes) * 60 + max(0, slack_seconds))


def _ocr_timeout_seconds(
    file_timeout_minutes: int = DEFAULT_OCR_FILE_TIMEOUT_MINUTES,
    concurrency: int = DEFAULT_OCR_CONCURRENCY,
) -> float:
    """Process cap: env ``REVIEW_DRIVER_OCR_TIMEOUT`` or derived (floored at 3600s)."""
    raw = os.environ.get("REVIEW_DRIVER_OCR_TIMEOUT")
    if raw is not None and raw.strip() != "":
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "Invalid REVIEW_DRIVER_OCR_TIMEOUT=%r; using derived process cap",
                raw,
            )
    derived = ocr_process_timeout_seconds(
        file_timeout_minutes=file_timeout_minutes,
        concurrency=concurrency,
    )
    return max(float(DEFAULT_OCR_TIMEOUT_SECONDS), derived)


def _pass_index_from_artifact(path: Path) -> int:
    name = path.name if path.suffix == "" else path.stem
    return int(name.rsplit("r", 1)[1])


def _ocr_log_path(output_json: Path) -> Path:
    return output_json.parent / "logs" / f"r{_pass_index_from_artifact(output_json)}-ocr.log"


def last_ocr_invocation_log(text: str) -> str:
    """Return the last OCR invocation in an append-only pass log."""
    if not text:
        return ""
    idx = text.rfind(_OCR_START_BANNER)
    if idx < 0:
        return text
    return text[idx:]


def ocr_process_timeout_reason(log: str) -> str | None:
    """Parse a driver process-timeout line from the current invocation log."""
    for line in reversed(log.splitlines()):
        match = _OCR_PROCESS_TIMEOUT_RE.search(line.strip())
        if match is None:
            continue
        seconds = float(match.group(1))
        return f"ocr process timeout after {format_duration(seconds)}"
    return None


def _ocr_log_text(output_dir: Path, pass_index: int) -> str:
    path = output_dir / "logs" / f"r{pass_index}-ocr.log"
    if not path.is_file():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return last_ocr_invocation_log(raw)


_LOG_LOCK = threading.Lock()


def ocr_session_dir(cwd: Path) -> Path:
    """Directory where OCR writes ``<session-id>.jsonl`` for *cwd*."""
    slug = str(cwd.resolve()).lstrip("/").replace("/", "-")
    return Path.home() / ".opencodereview" / "sessions" / slug


def _event_file_path(event: Mapping[str, Any]) -> str:
    raw = event.get("filePath", event.get("file_path", ""))
    return str(raw) if raw else ""


def format_ocr_session_event(
    event: Mapping[str, Any],
    *,
    first_request_for_file: bool = False,
) -> str | None:
    """Compact one-liner for an OCR session JSONL event, or ``None`` to skip."""
    event_type = str(event.get("type") or "")
    path = _event_file_path(event)
    if event_type == "session_start":
        session_id = event.get("sessionId", event.get("session_id", ""))
        diff_from = event.get("diffFrom", event.get("diff_from", ""))
        diff_to = event.get("diffTo", event.get("diff_to", ""))
        return f"[ocr] session {session_id} {diff_from}..{diff_to}"
    if event_type == "llm_request":
        if not first_request_for_file or not path:
            return None
        task = event.get("taskType", event.get("task_type", "review"))
        return f"[ocr] reviewing {path} ({task})"
    if event_type == "review_item_done":
        if not path:
            return None
        return f"[ocr] done {path}"
    if event_type == "review_item_failed":
        error = event.get("error") or "failed"
        return f"[ocr] failed {path}: {error}"
    if event_type == "llm_error":
        error = event.get("error") or "error"
        return f"[ocr] llm error {path}: {error}"
    return None


def _parse_jsonl_object(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _first_jsonl_event(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                event = _parse_jsonl_object(raw)
                if event is not None:
                    return event
    except OSError:
        return None
    return None


def _session_start_matches(
    event: Mapping[str, Any],
    *,
    cwd: Path,
    source: str,
    target: str,
) -> bool:
    if event.get("type") != "session_start":
        return False
    ev_cwd = event.get("cwd")
    if ev_cwd:
        try:
            if Path(str(ev_cwd)).resolve() != cwd.resolve():
                return False
        except OSError:
            return False
    ev_from = event.get("diffFrom", event.get("diff_from"))
    ev_to = event.get("diffTo", event.get("diff_to"))
    if ev_from is not None and str(ev_from) != target:
        return False
    if ev_to is not None and str(ev_to) != source:
        return False
    return True


def _discover_ocr_session_file(
    session_dir: Path,
    *,
    cwd: Path,
    source: str,
    target: str,
    not_before: float,
) -> Path | None:
    if not session_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    try:
        paths = list(session_dir.glob("*.jsonl"))
    except OSError:
        return None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < not_before - OCR_SESSION_DISCOVER_SLACK_SECONDS:
            continue
        first = _first_jsonl_event(path)
        if first is None or not _session_start_matches(
            first, cwd=cwd, source=source, target=target
        ):
            continue
        candidates.append((mtime, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _follow_ocr_session_jsonl(
    path: Path,
    *,
    stop: threading.Event,
    emit: Callable[[str], None],
    poll_s: float | None = None,
) -> None:
    interval = OCR_SESSION_POLL_SECONDS if poll_s is None else poll_s
    seen_files: set[str] = set()
    offset = 0
    buf = ""

    def _handle_line(raw: str) -> None:
        event = _parse_jsonl_object(raw)
        if event is None:
            return
        file_path = _event_file_path(event)
        first = False
        if event.get("type") == "llm_request" and file_path:
            first = file_path not in seen_files
            if first:
                seen_files.add(file_path)
        formatted = format_ocr_session_event(event, first_request_for_file=first)
        if formatted:
            emit(formatted)

    while True:
        try:
            with path.open("r", encoding="utf-8") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
        except OSError:
            chunk = ""
        if chunk:
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                _handle_line(line)
        if stop.is_set():
            if buf.strip():
                _handle_line(buf)
            return
        stop.wait(interval)


def _ocr_session_follow_loop(
    *,
    cwd: Path,
    source: str,
    target: str,
    launched_at: float,
    stop: threading.Event,
    emit: Callable[[str], None],
    session_dir: Path | None = None,
    poll_s: float | None = None,
) -> None:
    interval = OCR_SESSION_POLL_SECONDS if poll_s is None else poll_s
    directory = session_dir if session_dir is not None else ocr_session_dir(cwd)
    path: Path | None = None
    while not stop.is_set() and path is None:
        path = _discover_ocr_session_file(
            directory,
            cwd=cwd,
            source=source,
            target=target,
            not_before=launched_at,
        )
        if path is None:
            stop.wait(interval)
    if path is None:
        return
    _follow_ocr_session_jsonl(path, stop=stop, emit=emit, poll_s=interval)


def _append_log(
    log_path: Path,
    text: str,
    *,
    tee: bool,
    on_child_log: Callable[[str], None] | None = None,
) -> None:
    if not text:
        return
    payload = text if text.endswith("\n") else text + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(payload)
    if tee:
        sys.stderr.write(payload)
    if on_child_log is not None:
        on_child_log(payload)


class ChildLogFanout:
    """Optional sink for child stdout/stderr lines (TUI log pane)."""

    def __init__(self) -> None:
        self._sink: Callable[[str], None] | None = None

    def set_sink(self, sink: Callable[[str], None] | None) -> None:
        self._sink = sink

    def emit(self, text: str) -> None:
        sink = self._sink
        if sink is not None and text:
            sink(text)


class _Writable(Protocol):
    def write(self, data: str) -> int: ...

    def flush(self) -> None: ...


class _Tee:
    """Write to several streams; never a TTY so child CLIs stay --no-tui."""

    def __init__(self, *streams: _Writable) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

    def isatty(self) -> bool:
        return False


class _CallbackStream:
    """Line-buffered TextIO that forwards writes to a callback. Never a TTY."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback
        self._buf = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._callback(line + "\n")
        return len(data)

    def flush(self) -> None:
        if self._buf:
            self._callback(self._buf)
            self._buf = ""

    def isatty(self) -> bool:
        return False


@contextmanager
def _redirect_stdio(
    log_path: Path,
    *,
    tee: bool,
    on_child_log: Callable[[str], None] | None = None,
) -> Iterator[None]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    old_out, old_err = sys.stdout, sys.stderr
    extras: list[_Writable] = []
    if tee:
        extras.append(old_err)
    if on_child_log is not None:
        extras.append(_CallbackStream(on_child_log))
    if extras:
        sys.stdout = _Tee(log_file, *extras)
        sys.stderr = _Tee(log_file, *extras)
    else:
        sys.stdout = log_file
        sys.stderr = log_file
    try:
        yield
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
        log_file.close()


def _call_inprocess_main(main_fn: object, argv: list[str]) -> int:
    try:
        rc = main_fn(argv)  # type: ignore[operator]
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    if rc is None:
        return 0
    return int(rc)


def run_ocr_subprocess(
    *,
    source: str,
    target: str,
    output_json: Path,
    exclude: list[str],
    cwd: Path,
    ocr_bin: str = DEFAULT_OCR_BIN,
    log_path: Path | None = None,
    verbose: bool = False,
    on_child_log: Callable[[str], None] | None = None,
    ocr_timeout_minutes: int = DEFAULT_OCR_FILE_TIMEOUT_MINUTES,
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY,
    ocr_llm_timeout_seconds: int = DEFAULT_OCR_LLM_TIMEOUT_SECONDS,
    audience: str = DEFAULT_OCR_AUDIENCE,
    session_dir: Path | None = None,
) -> int:
    """Run ``ocr review`` with ``--from`` = target and ``--to`` = source.

    JSON is collected from stdout. stderr is streamed live into the log
    (and optional *on_child_log*). OCR session JSONL events are tailed into
    the same log so a ``--format json`` review is not a black box.
    """
    cmd = [
        ocr_bin,
        "review",
        "--from",
        target,
        "--to",
        source,
        "--format",
        "json",
        "--audience",
        audience,
        "--repo",
        str(cwd),
        "--timeout",
        str(ocr_timeout_minutes),
        "--concurrency",
        str(ocr_concurrency),
    ]
    if exclude:
        cmd.extend(["--exclude", ",".join(exclude)])

    log = log_path if log_path is not None else _ocr_log_path(output_json)
    timeout = _ocr_timeout_seconds(ocr_timeout_minutes, ocr_concurrency)
    llm_timeout_label = (
        f"{ocr_llm_timeout_seconds}s"
        if ocr_llm_timeout_seconds > 0
        else "ocr-default"
    )
    if (
        ocr_llm_timeout_seconds > 0
        and ocr_llm_timeout_seconds * 3 > ocr_timeout_minutes * 60
    ):
        logger.warning(
            "OCR per-file timeout %sm may be shorter than 3 HTTP calls at "
            "OCR_LLM_TIMEOUT=%ss",
            ocr_timeout_minutes,
            ocr_llm_timeout_seconds,
        )
    _append_log(
        log,
        (
            f"OCR starting: {' '.join(cmd)}\n"
            f"concurrency={ocr_concurrency} file-timeout={ocr_timeout_minutes}m "
            f"llm-http-timeout={llm_timeout_label} "
            f"process-timeout={format_duration(timeout)} log={log}\n"
        ),
        tee=verbose,
        on_child_log=on_child_log,
    )
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": cwd,
    }
    if ocr_llm_timeout_seconds > 0:
        env = os.environ.copy()
        env["OCR_LLM_TIMEOUT"] = str(ocr_llm_timeout_seconds)
        popen_kwargs["env"] = env
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        logger.error("ocr binary not found: %s", ocr_bin)
        _append_log(
            log,
            f"ocr binary not found: {ocr_bin}\n",
            tee=verbose,
            on_child_log=on_child_log,
        )
        return 1

    stdout_chunks: list[str] = []
    session_stop = threading.Event()
    launched_at = time.time()

    def _pump_stdout() -> None:
        if proc.stdout is None:
            return
        stdout_chunks.append(proc.stdout.read())

    def _pump_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            _append_log(log, line, tee=verbose, on_child_log=on_child_log)

    def _emit_session(text: str) -> None:
        _append_log(log, text, tee=verbose, on_child_log=on_child_log)

    def _pump_session() -> None:
        _ocr_session_follow_loop(
            cwd=cwd,
            source=source,
            target=target,
            launched_at=launched_at,
            stop=session_stop,
            emit=_emit_session,
            session_dir=session_dir,
        )

    out_thread = threading.Thread(target=_pump_stdout, daemon=True)
    err_thread = threading.Thread(target=_pump_stderr, daemon=True)
    session_thread = threading.Thread(target=_pump_session, daemon=True)
    out_thread.start()
    err_thread.start()
    session_thread.start()
    global _ocr_proc
    with _ocr_proc_lock:
        _ocr_proc = proc
    try:
        deadline = time.monotonic() + timeout
        timed_out = False
        while True:
            if _force_stop_requested:
                _kill_ocr_process(proc, force=False)
                try:
                    proc.wait(timeout=OCR_FORCE_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.warning("OCR did not exit after SIGTERM; sending SIGKILL")
                    _kill_ocr_process(proc, force=True)
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        logger.error("OCR still running after SIGKILL")
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                proc.wait(timeout=min(OCR_WAIT_POLL_SECONDS, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        if timed_out:
            logger.error("ocr timed out after %s seconds", timeout)
            _kill_ocr_process(proc, force=True)
            out_thread.join(timeout=1)
            err_thread.join(timeout=1)
            _append_log(
                log,
                f"ocr timed out after {timeout} seconds\n",
                tee=verbose,
                on_child_log=on_child_log,
            )
            return 1
        out_thread.join(timeout=1)
        err_thread.join(timeout=1)

        stdout = "".join(stdout_chunks)
        rc = proc.returncode if proc.returncode is not None else 1
        if stdout.strip():
            output_json.parent.mkdir(parents=True, exist_ok=True)
            output_json.write_text(stdout, encoding="utf-8")
        if _force_stop_requested:
            logger.info("ocr killed by force stop (rc=%s)", rc)
            _append_log(
                log,
                "ocr killed (force stop)\n",
                tee=verbose,
                on_child_log=on_child_log,
            )
            return 130
        if rc != 0:
            logger.error("ocr exited %s", rc)
            return rc if rc else 1
        if not stdout.strip():
            logger.error("ocr produced empty stdout")
            _append_log(
                log,
                "ocr produced empty stdout\n",
                tee=verbose,
                on_child_log=on_child_log,
            )
            return 1
        return 0
    finally:
        with _ocr_proc_lock:
            if _ocr_proc is proc:
                _ocr_proc = None
        session_stop.set()
        session_thread.join(timeout=2)


def run_analyzer_main(
    *,
    ocr_json: Path,
    feedback_dir: Path,
    prior_feedback: list[Path],
    knowledge_dir: Path | None,
    exclude: list[str],
    output_dir: Path,
    verbose: bool = False,
    on_child_log: Callable[[str], None] | None = None,
) -> int:
    from deep_architect.review_analyzer import main as analyzer_main

    argv: list[str] = [
        str(ocr_json),
        "--output-dir",
        str(feedback_dir),
        "--no-tui",
    ]
    if knowledge_dir is not None:
        argv.extend(["--knowledge-dir", str(knowledge_dir)])
    for prior in prior_feedback:
        argv.extend(["--prior-feedback", str(prior)])
    for glob in exclude:
        argv.extend(["--exclude", glob])

    pass_index = _pass_index_from_artifact(feedback_dir)
    log_path = output_dir / "logs" / f"r{pass_index}-analyzer.log"
    with _redirect_stdio(log_path, tee=verbose, on_child_log=on_child_log):
        return _call_inprocess_main(analyzer_main, argv)


def run_action_main(
    *,
    feedback_dir: Path,
    output_dir: Path,
    verbose: bool = False,
    provider: str | None = None,
    model: str | None = None,
    config: Path | None = None,
    on_child_log: Callable[[str], None] | None = None,
) -> int:
    from deep_architect.review_action_harness import main as action_main

    argv: list[str] = [
        str(feedback_dir),
        "--no-tui",
        "--min-severity",
        ACTION_MIN_SEVERITY,
        "--exclude-from-commit",
        str(output_dir),
    ]
    if provider:
        argv.extend(["--provider", provider])
    if model:
        argv.extend(["--model", model])
    if config is not None:
        argv.extend(["--config", str(config)])

    pass_index = _pass_index_from_artifact(feedback_dir)
    log_path = output_dir / "logs" / f"r{pass_index}-action.log"
    with _redirect_stdio(log_path, tee=verbose, on_child_log=on_child_log):
        return _call_inprocess_main(action_main, argv)


@dataclass
class ProductionRunners:
    """OCR via subprocess; analyzer and action via in-process ``main(argv)``."""

    cwd: Path
    output_dir: Path
    ocr_bin: str = DEFAULT_OCR_BIN
    verbose: bool = False
    provider: str | None = None
    model: str | None = None
    config: Path | None = None
    on_child_log: Callable[[str], None] | None = None
    ocr_timeout_minutes: int = DEFAULT_OCR_FILE_TIMEOUT_MINUTES
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY
    ocr_llm_timeout_seconds: int = DEFAULT_OCR_LLM_TIMEOUT_SECONDS
    ocr_audience: str = DEFAULT_OCR_AUDIENCE

    def run_ocr(
        self, *, source: str, target: str, output_json: Path, exclude: list[str]
    ) -> int:
        merged_exclude = _dedupe_globs(
            default_output_excludes(DEFAULT_OUTPUT_DIR, self.cwd),
            default_output_excludes(self.output_dir, self.cwd),
            default_ocr_report_excludes(),
            exclude,
        )
        return run_ocr_subprocess(
            source=source,
            target=target,
            output_json=output_json,
            exclude=merged_exclude,
            cwd=self.cwd,
            ocr_bin=self.ocr_bin,
            verbose=self.verbose,
            on_child_log=self.on_child_log,
            ocr_timeout_minutes=self.ocr_timeout_minutes,
            ocr_concurrency=self.ocr_concurrency,
            ocr_llm_timeout_seconds=self.ocr_llm_timeout_seconds,
            audience=self.ocr_audience,
        )

    def run_analyzer(
        self,
        *,
        ocr_json: Path,
        feedback_dir: Path,
        prior_feedback: list[Path],
        knowledge_dir: Path | None,
        exclude: list[str],
    ) -> int:
        return run_analyzer_main(
            ocr_json=ocr_json,
            feedback_dir=feedback_dir,
            prior_feedback=prior_feedback,
            knowledge_dir=knowledge_dir,
            exclude=exclude,
            output_dir=self.output_dir,
            verbose=self.verbose,
            on_child_log=self.on_child_log,
        )

    def run_action(self, *, feedback_dir: Path) -> int:
        return run_action_main(
            feedback_dir=feedback_dir,
            output_dir=self.output_dir,
            verbose=self.verbose,
            provider=self.provider,
            model=self.model,
            config=self.config,
            on_child_log=self.on_child_log,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run ocr review → review-analyzer → review-action until "
            "high/medium VALID novelty is gone for K consecutive passes"
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        help="PR branch (or SHA) to review; HEAD must already be this commit",
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=f"Base branch for the OCR diff (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Root directory for review runs (default: .review-runs). "
            "Each run is stored under {root}/{branch}/{timestamp}/"
        ),
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=None,
        help=(
            "Safety cap on OCR→action passes (default: config / 5). "
            "0 = unlimited; stop only when novelty is 0 for K consecutive passes"
        ),
    )
    parser.add_argument(
        "--zero-novelty-passes",
        type=int,
        default=None,
        help="Consecutive zero-novelty passes required to converge (default: config / 2)",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Continue a stopped run for this source/target when present "
            "(default). Pass --no-resume to start a new timestamped run "
            "without overwriting previous artifacts."
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help=(
            "Glob passed to ocr and review-analyzer (repeatable). "
            "The output root and code-review*.json reports are always excluded."
        ),
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=None,
        help="knowledge/ directory for analyzer catalog memory (default: <cwd>/knowledge)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Coding-agent provider passed through to review-action",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Coding-agent model passed through to review-action",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to deep-architect config.toml (defaults if missing)",
    )
    parser.add_argument(
        "--ocr-timeout",
        type=int,
        default=None,
        metavar="MINUTES",
        help=(
            "OCR per-file timeout in minutes (ocr --timeout). "
            "Default: env REVIEW_DRIVER_OCR_FILE_TIMEOUT → "
            "config review_driver_ocr_timeout_minutes → "
            f"{DEFAULT_OCR_FILE_TIMEOUT_MINUTES}"
        ),
    )
    parser.add_argument(
        "--ocr-concurrency",
        type=int,
        default=None,
        metavar="N",
        help=(
            "OCR max concurrent file reviews (ocr --concurrency). "
            "Default: env REVIEW_DRIVER_OCR_CONCURRENCY → "
            "config review_driver_ocr_concurrency → "
            f"{DEFAULT_OCR_CONCURRENCY}"
        ),
    )
    parser.add_argument(
        "--ocr-llm-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "OCR per-request HTTP timeout in seconds (OCR_LLM_TIMEOUT). "
            "Default: env REVIEW_DRIVER_OCR_LLM_TIMEOUT → "
            "config review_driver_ocr_llm_timeout_seconds → "
            f"{DEFAULT_OCR_LLM_TIMEOUT_SECONDS} (0 = do not export; OCR "
            "uses timeout_sec or its 300s client default)"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG logging; tee child logs to stderr in plain mode",
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


def _positive_int_from_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; ignoring", name, raw)
        return None
    if value < 1:
        logger.warning("%s=%d must be >= 1; ignoring", name, value)
        return None
    return value


def resolve_ocr_timeout_minutes(
    cli_value: int | None, cfg: HarnessConfig
) -> int:
    """CLI > REVIEW_DRIVER_OCR_FILE_TIMEOUT > TOML > default (minutes)."""
    if cli_value is not None:
        if cli_value < 1:
            raise ValueError(f"ocr-timeout must be >= 1, got {cli_value}")
        return cli_value
    env_value = _positive_int_from_env("REVIEW_DRIVER_OCR_FILE_TIMEOUT")
    if env_value is not None:
        return env_value
    value = int(cfg.thresholds.review_driver_ocr_timeout_minutes)
    if value < 1:
        logger.warning(
            "thresholds.review_driver_ocr_timeout_minutes=%d must be >= 1; "
            "using %s",
            value,
            DEFAULT_OCR_FILE_TIMEOUT_MINUTES,
        )
        return DEFAULT_OCR_FILE_TIMEOUT_MINUTES
    return value


def resolve_ocr_concurrency(cli_value: int | None, cfg: HarnessConfig) -> int:
    """CLI > REVIEW_DRIVER_OCR_CONCURRENCY > TOML > default."""
    if cli_value is not None:
        if cli_value < 1:
            raise ValueError(f"ocr-concurrency must be >= 1, got {cli_value}")
        return cli_value
    env_value = _positive_int_from_env("REVIEW_DRIVER_OCR_CONCURRENCY")
    if env_value is not None:
        return env_value
    value = int(cfg.thresholds.review_driver_ocr_concurrency)
    if value < 1:
        logger.warning(
            "thresholds.review_driver_ocr_concurrency=%d must be >= 1; using %s",
            value,
            DEFAULT_OCR_CONCURRENCY,
        )
        return DEFAULT_OCR_CONCURRENCY
    return value


def resolve_ocr_llm_timeout_seconds(
    cli_value: int | None, cfg: HarnessConfig
) -> int:
    """CLI > REVIEW_DRIVER_OCR_LLM_TIMEOUT > TOML > 0 (do not export)."""
    if cli_value is not None:
        if cli_value < 0:
            raise ValueError(f"ocr-llm-timeout must be >= 0, got {cli_value}")
        return cli_value
    env_value = _non_negative_int_from_env("REVIEW_DRIVER_OCR_LLM_TIMEOUT")
    if env_value is not None:
        return env_value
    value = int(cfg.thresholds.review_driver_ocr_llm_timeout_seconds)
    if value < 0:
        logger.warning(
            "thresholds.review_driver_ocr_llm_timeout_seconds=%d must be >= 0; "
            "using %s",
            value,
            DEFAULT_OCR_LLM_TIMEOUT_SECONDS,
        )
        return DEFAULT_OCR_LLM_TIMEOUT_SECONDS
    return value


def _non_negative_int_from_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; ignoring", name, raw)
        return None
    if value < 0:
        logger.warning("%s=%d must be >= 0; ignoring", name, value)
        return None
    return value


def _load_driver_config(config_path: Path | None) -> HarnessConfig:
    cfg = HarnessConfig()
    path = config_path if config_path is not None else _resolve_default_config_path()
    if not path.exists():
        logger.warning("Config file not found at %s, using defaults", path)
        return cfg
    try:
        return load_config(path)
    except Exception as exc:
        logger.warning("Failed to load config: %s, using defaults", exc)
        return cfg


def _driver_exit_code(progress: DriverProgress, *, interrupted: bool) -> int:
    if interrupted:
        return 130
    action_errors = any(record.action_errors for record in progress.passes)
    if progress.status == "converged" and not action_errors:
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``review-driver``.

    TTY auto-starts the observational TUI; CI / pipes stay plain-text.
    The loop itself is unattended (no confirm between passes).
    """
    _reset_interrupt_state()
    _install_sigint_handler()

    args = parse_args(argv)

    force_tui = _force_tui_from_args(args)
    use_tui = should_use_tui(force_tui=force_tui)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    cfg = _load_driver_config(args.config)
    max_passes = (
        args.max_passes
        if args.max_passes is not None
        else cfg.thresholds.review_driver_max_passes
    )
    if max_passes < 0:
        print("Error: --max-passes must be >= 0 (0 = unlimited)", file=sys.stderr)
        return 1
    k = (
        args.zero_novelty_passes
        if args.zero_novelty_passes is not None
        else cfg.thresholds.review_driver_zero_novelty_passes
    )
    try:
        ocr_timeout_minutes = resolve_ocr_timeout_minutes(args.ocr_timeout, cfg)
        ocr_concurrency = resolve_ocr_concurrency(args.ocr_concurrency, cfg)
        ocr_llm_timeout_seconds = resolve_ocr_llm_timeout_seconds(
            args.ocr_llm_timeout, cfg
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    cwd = Path.cwd()
    ocr_bin = os.environ.get("OCR_BIN", DEFAULT_OCR_BIN)
    try:
        _repo, source_sha, target_sha = preflight_driver(
            cwd=cwd,
            source=args.source,
            target=args.target,
            output_dir=args.output_dir,
            ocr_bin=ocr_bin,
        )
    except DriverPreflightError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_root = args.output_dir
    run_dir, resuming_run = resolve_driver_run_dir(
        output_root,
        source=args.source,
        target=args.target,
        source_sha=source_sha,
        target_sha=target_sha,
        resume=args.resume,
    )
    logger.info(
        "Review run directory: %s (%s)",
        run_dir,
        "resume" if resuming_run else "new",
    )
    exclude = _dedupe_globs(
        default_output_excludes(DEFAULT_OUTPUT_DIR, cwd),
        default_output_excludes(output_root, cwd),
        default_ocr_report_excludes(),
        list(args.exclude),
    )

    child_logs = ChildLogFanout()
    runners = ProductionRunners(
        cwd=cwd,
        output_dir=run_dir,
        ocr_bin=ocr_bin,
        verbose=args.verbose,
        provider=args.provider,
        model=args.model,
        config=args.config,
        on_child_log=child_logs.emit,
        ocr_timeout_minutes=ocr_timeout_minutes,
        ocr_concurrency=ocr_concurrency,
        ocr_llm_timeout_seconds=ocr_llm_timeout_seconds,
        ocr_audience="human" if use_tui else DEFAULT_OCR_AUDIENCE,
    )

    def _run_pipeline(reporter: ProgressReporter) -> DriverProgress:
        return run_driver(
            source=args.source,
            target=args.target,
            output_dir=run_dir,
            runners=runners,
            max_passes=max_passes,
            k=k,
            resume=args.resume,
            knowledge_dir=args.knowledge_dir,
            exclude=exclude,
            source_sha=source_sha,
            target_sha=target_sha,
            reporter=reporter,
        )

    try:
        if use_tui:
            from deep_architect.review_driver_tui import (  # noqa: PLC0415
                last_feedback_dir,
                run_review_driver_tui,
            )

            meta = DriverRunMeta(
                source=args.source,
                target=args.target,
                source_sha=source_sha,
                target_sha=target_sha,
                max_passes=max_passes,
                k=k,
                output_dir=run_dir,
                resume=resuming_run,
            )

            def _finalize(progress: DriverProgress) -> Path:
                return write_driver_report(run_dir, progress)

            tui_result = run_review_driver_tui(
                meta,
                _run_pipeline,
                log_level=log_level,
                log_file=run_dir / "review-driver.log",
                finalize=_finalize,
                attach_child_logs=child_logs.set_sink,
            )
            browse_dir = last_feedback_dir(tui_result.progress)
            if tui_result.action == "browse" and browse_dir is not None:
                from deep_architect.review_feedback_browse import (  # noqa: PLC0415
                    run_feedback_browse,
                )

                return run_feedback_browse(browse_dir, mode="action")
            if tui_result.report_path is not None:
                print(f"Report written to {tui_result.report_path}")
            return _driver_exit_code(
                tui_result.progress, interrupted=_interrupt_requested
            )

        progress = _run_pipeline(PlainReporter())
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    write_driver_report(run_dir, progress)
    return _driver_exit_code(progress, interrupted=_interrupt_requested)


if __name__ == "__main__":
    sys.exit(main())
