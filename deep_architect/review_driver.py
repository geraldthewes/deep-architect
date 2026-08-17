"""Review-driver orchestrator: OCR → analyzer → action until novelty is gone."""

from __future__ import annotations

import argparse
import logging
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TextIO

import git
from pydantic import BaseModel, Field

from deep_architect.action_report import SUMMARY_FILENAME, load_action_report
from deep_architect.config import HarnessConfig, _resolve_default_config_path, load_config
from deep_architect.logger import get_logger
from deep_architect.review_novelty import (
    OcrRunStats,
    StopReason,
    consecutive_zero_novelty,
    count_high_signal_valid,
    count_ocr_comments_by_severity,
    count_valid_by_severity,
    count_verdicts,
    decide_stop,
    parse_ocr_run_stats,
)

logger = get_logger(__name__)

PROGRESS_FILENAME = "progress.json"
REPORT_FILENAME = "REPORT.md"
PASS_FOOTER = "─────────────────────────────────────────────────────"
DEFAULT_OUTPUT_DIR = Path(".review-runs")
DEFAULT_TARGET = "main"
DEFAULT_OCR_BIN = "ocr"
DEFAULT_OCR_TIMEOUT_SECONDS = 3600
ACTION_MIN_SEVERITY = "medium"

_interrupt_requested = False


def request_interrupt() -> None:
    """Request a graceful stop after the current step returns."""
    global _interrupt_requested
    _interrupt_requested = True


def _sigint_handler(signum: int, frame: object) -> None:
    request_interrupt()
    logger.info("CTRL-C received, finishing current step before shutdown...")

_COST_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")
_ELAPSED_RE = re.compile(
    r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$",
    re.IGNORECASE,
)


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


def format_pass_header(pass_index: int, max_passes: int) -> str:
    return f"── Pass {pass_index}/{max_passes} ─────────────────────────────────────────"


def format_ocr_summary(
    stats: OcrRunStats,
    severity: Mapping[str, int],
    wall_seconds: float,
) -> str:
    """One OCR phase line. Omits the word ``tokens`` when total_tokens is None."""
    comments = stats.comments
    if comments is None:
        comments = sum(severity.values())
    parts: list[str] = [f"comments {comments}"]
    if stats.files_reviewed is not None:
        parts.append(f"files {stats.files_reviewed}")
    parts.append(f"high {severity.get('high', 0)}")
    parts.append(f"med {severity.get('medium', 0)}")
    parts.append(f"low {severity.get('low', 0)}")
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
    return "OCR      " + "  ".join(parts)


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


def format_stop_line(status: str, k: int) -> str:
    if status == "converged":
        return f"Converged (K={k})."
    if status == "max_passes":
        return "Stopped: max-passes with novelty remaining."
    if status == "failed":
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
        print(format_stop_line(progress.status, progress.k))
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
        f"- Max passes: {progress.max_passes}",
        f"- Stop reason: {progress.status}",
        f"- Novelty history: {progress.novelty_history}",
        "",
        "Stop is the count of high/medium VALID findings, **not** the OCR comment count.",
        "",
    ]
    for record in progress.passes:
        blocks.append(f"## Pass {record.pass_index}")
        blocks.append("")
        blocks.append(format_pass_header(record.pass_index, progress.max_passes))
        blocks.append(
            format_ocr_summary(
                OcrRunStats(
                    comments=sum(record.ocr_severity.values()) or None,
                    total_tokens=record.ocr_tokens_total,
                    elapsed=record.ocr_elapsed_s,
                ),
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
            phase_seconds=phase_seconds,
            wall_seconds=wall_seconds,
            status="failed",
        )
    )
    progress.status = "failed"
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
    resume: bool = False,
    knowledge_dir: Path | None = None,
    exclude: list[str] | None = None,
    source_sha: str = "",
    target_sha: str = "",
    reporter: ProgressReporter | None = None,
) -> DriverProgress:
    """Run OCR → analyzer → action until stop predicates fire.

    All external tools go through *runners*. Mid-pass crash is not recorded as
    a completed pass; ``--resume`` restarts that pass and overwrites artifacts.
    """
    exclude_globs = list(exclude) if exclude is not None else []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sink: ProgressReporter = reporter if reporter is not None else PlainReporter()
    sink.start(
        DriverRunMeta(
            source=source,
            target=target,
            source_sha=source_sha,
            target_sha=target_sha,
            max_passes=max_passes,
            k=k,
            output_dir=output_dir,
            resume=resume,
        )
    )

    if resume:
        progress_path = output_dir / PROGRESS_FILENAME
        if not progress_path.is_file():
            raise FileNotFoundError(
                f"Cannot --resume: {progress_path} is missing. "
                "Omit --resume to start a new run."
            )
        progress = load_driver_progress(output_dir)
        if progress.source != source or progress.target != target:
            raise ValueError(
                f"Resume state is for {progress.source} → {progress.target}, "
                f"not {source} → {target}"
            )
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
        start = 1

    save_driver_progress(output_dir, progress)

    try:
        for pass_index in range(start, max_passes + 1):
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
                logger.error("OCR failed on pass %s (rc=%s)", pass_index, ocr_rc)
                return _fail_pass(
                    progress,
                    output_dir,
                    pass_index=pass_index,
                    ocr_json=ocr_json,
                    feedback_dir=feedback_dir,
                    phase_seconds={"ocr": ocr_wall},
                    wall_seconds=ocr_wall,
                    reporter=sink,
                )

            ocr_stats = parse_ocr_run_stats(ocr_json)
            ocr_severity = count_ocr_comments_by_severity(ocr_json)
            sink.phase_done(format_ocr_summary(ocr_stats, ocr_severity, ocr_wall))

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


def _ocr_timeout_seconds() -> float:
    raw = os.environ.get("REVIEW_DRIVER_OCR_TIMEOUT")
    if raw is None or raw.strip() == "":
        return float(DEFAULT_OCR_TIMEOUT_SECONDS)
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid REVIEW_DRIVER_OCR_TIMEOUT=%r, using %s",
            raw,
            DEFAULT_OCR_TIMEOUT_SECONDS,
        )
        return float(DEFAULT_OCR_TIMEOUT_SECONDS)


def _pass_index_from_artifact(path: Path) -> int:
    name = path.name if path.suffix == "" else path.stem
    return int(name.rsplit("r", 1)[1])


def _ocr_log_path(output_json: Path) -> Path:
    return output_json.parent / "logs" / f"r{_pass_index_from_artifact(output_json)}-ocr.log"


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
) -> int:
    """Run ``ocr review`` with ``--from`` = target and ``--to`` = source.

    JSON is collected from stdout. stderr is streamed live into the log
    (and optional *on_child_log*) so a long review is not a black box.
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
        "agent",
        "--repo",
        str(cwd),
    ]
    if exclude:
        cmd.extend(["--exclude", ",".join(exclude)])

    log = log_path if log_path is not None else _ocr_log_path(output_json)
    timeout = _ocr_timeout_seconds()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
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

    def _pump_stdout() -> None:
        if proc.stdout is None:
            return
        stdout_chunks.append(proc.stdout.read())

    def _pump_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            _append_log(log, line, tee=verbose, on_child_log=on_child_log)

    out_thread = threading.Thread(target=_pump_stdout, daemon=True)
    err_thread = threading.Thread(target=_pump_stderr, daemon=True)
    out_thread.start()
    err_thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("ocr timed out after %s seconds", timeout)
        proc.kill()
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

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(stdout, encoding="utf-8")
    return 0


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

    def run_ocr(
        self, *, source: str, target: str, output_json: Path, exclude: list[str]
    ) -> int:
        return run_ocr_subprocess(
            source=source,
            target=target,
            output_json=output_json,
            exclude=exclude,
            cwd=self.cwd,
            ocr_bin=self.ocr_bin,
            verbose=self.verbose,
            on_child_log=self.on_child_log,
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
        help="Directory for per-pass artifacts (default: .review-runs)",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=None,
        help="Safety cap on OCR→action passes (default: config / 5)",
    )
    parser.add_argument(
        "--zero-novelty-passes",
        type=int,
        default=None,
        help="Consecutive zero-novelty passes required to converge (default: config / 2)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from output-dir/progress.json (fails if the file is missing)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob passed to ocr and review-analyzer (repeatable)",
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
    global _interrupt_requested
    _interrupt_requested = False
    signal.signal(signal.SIGINT, _sigint_handler)

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
    k = (
        args.zero_novelty_passes
        if args.zero_novelty_passes is not None
        else cfg.thresholds.review_driver_zero_novelty_passes
    )

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

    child_logs = ChildLogFanout()
    runners = ProductionRunners(
        cwd=cwd,
        output_dir=args.output_dir,
        ocr_bin=ocr_bin,
        verbose=args.verbose,
        provider=args.provider,
        model=args.model,
        config=args.config,
        on_child_log=child_logs.emit,
    )

    def _run_pipeline(reporter: ProgressReporter) -> DriverProgress:
        return run_driver(
            source=args.source,
            target=args.target,
            output_dir=args.output_dir,
            runners=runners,
            max_passes=max_passes,
            k=k,
            resume=args.resume,
            knowledge_dir=args.knowledge_dir,
            exclude=args.exclude,
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
                output_dir=args.output_dir,
                resume=args.resume,
            )

            def _finalize(progress: DriverProgress) -> Path:
                return write_driver_report(args.output_dir, progress)

            tui_result = run_review_driver_tui(
                meta,
                _run_pipeline,
                log_level=log_level,
                log_file=args.output_dir / "review-driver.log",
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

    write_driver_report(args.output_dir, progress)
    return _driver_exit_code(progress, interrupted=_interrupt_requested)


if __name__ == "__main__":
    sys.exit(main())
