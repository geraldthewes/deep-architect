"""Novelty metric and stop predicates for the review-driver loop.

Single source of truth so PROJ-0018 can later import the same functions.
Thresholds (K, max-passes) are passed in by callers from config — this module
does not read TOML.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from deep_architect.feedback_report import load_feedback_dir

HIGH_SIGNAL_SEVERITIES: frozenset[str] = frozenset({"high", "medium"})
DEFAULT_ZERO_NOVELTY_PASSES = 2
DEFAULT_MAX_PASSES = 5

_SEVERITY_BUCKETS: tuple[str, ...] = ("high", "medium", "low", "unknown")


class StopReason(StrEnum):
    CONTINUE = "continue"
    CONVERGED = "converged"
    MAX_PASSES = "max_passes"


@dataclass(frozen=True)
class OcrRunStats:
    """Optional OCR JSON ``summary`` fields. Missing values stay None."""

    comments: int | None = None
    files_reviewed: int | None = None
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    elapsed: int | float | str | None = None


def _severity_bucket(severity: str) -> str:
    key = severity.strip().lower()
    if key in ("high", "medium", "low"):
        return key
    return "unknown"


def _empty_severity_counts() -> dict[str, int]:
    return {key: 0 for key in _SEVERITY_BUCKETS}


def count_high_signal_valid(feedback_dir: Path) -> int:
    """Count VALID findings whose OCR severity is high or medium.

    Missing / unknown / low severity do not count. DUPLICATE, BACKLOG,
    TIMEOUT, REJECTED never count. Catalog/dedup already happened in
    review-analyzer; remaining high/medium VALID *are* the novelty signal.
    """
    report = load_feedback_dir(feedback_dir)
    return sum(
        1
        for finding in report.findings
        if finding.verdict == "VALID" and finding.severity in HIGH_SIGNAL_SEVERITIES
    )


def consecutive_zero_novelty(history: list[int]) -> int:
    """Trailing count of zeros; empty history → 0."""
    n = 0
    for value in reversed(history):
        if value == 0:
            n += 1
        else:
            break
    return n


def decide_stop(
    *,
    novelty_history: list[int],
    k: int,
    max_passes: int,
) -> StopReason:
    """Decide after a pass has been appended to *novelty_history*.

    Converged takes priority when the last pass also hits max-passes.
    """
    if consecutive_zero_novelty(novelty_history) >= k:
        return StopReason.CONVERGED
    if len(novelty_history) >= max_passes:
        return StopReason.MAX_PASSES
    return StopReason.CONTINUE


def count_valid_by_severity(feedback_dir: Path) -> dict[str, int]:
    """VALID findings bucketed by OCR severity (missing → 'unknown')."""
    report = load_feedback_dir(feedback_dir)
    counts = _empty_severity_counts()
    for finding in report.findings:
        if finding.verdict == "VALID":
            counts[_severity_bucket(finding.severity)] += 1
    return counts


def count_findings_by_severity(feedback_dir: Path) -> dict[str, int]:
    """All findings bucketed by OCR severity."""
    report = load_feedback_dir(feedback_dir)
    counts = _empty_severity_counts()
    for finding in report.findings:
        counts[_severity_bucket(finding.severity)] += 1
    return counts


def count_verdicts(feedback_dir: Path) -> dict[str, int]:
    """Verdict histogram via load_feedback_dir."""
    report = load_feedback_dir(feedback_dir)
    return dict(report.counts)


def count_ocr_comments_by_severity(ocr_json: Path) -> dict[str, int]:
    """Histogram of ``comments[].severity`` (missing → 'unknown')."""
    data = json.loads(ocr_json.read_text(encoding="utf-8"))
    counts = _empty_severity_counts()
    comments = data.get("comments")
    if not isinstance(comments, list):
        return counts
    for comment in comments:
        if not isinstance(comment, dict):
            counts["unknown"] += 1
            continue
        raw = comment.get("severity") or comment.get("level") or ""
        counts[_severity_bucket(str(raw))] += 1
    return counts


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    return None


def _optional_elapsed(value: Any) -> int | float | str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | str):
        return value
    return None


def parse_ocr_run_stats(ocr_json: Path) -> OcrRunStats:
    """Read optional OCR JSON ``summary`` object.

    Fields (all optional): comments, files_reviewed, total_tokens,
    input_tokens, output_tokens, cache_read_tokens, elapsed.
    Missing ``summary`` → empty stats, not an error.
    """
    data = json.loads(ocr_json.read_text(encoding="utf-8"))
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return OcrRunStats()
    return OcrRunStats(
        comments=_optional_int(summary.get("comments")),
        files_reviewed=_optional_int(summary.get("files_reviewed")),
        total_tokens=_optional_int(summary.get("total_tokens")),
        input_tokens=_optional_int(summary.get("input_tokens")),
        output_tokens=_optional_int(summary.get("output_tokens")),
        cache_read_tokens=_optional_int(summary.get("cache_read_tokens")),
        elapsed=_optional_elapsed(summary.get("elapsed")),
    )
