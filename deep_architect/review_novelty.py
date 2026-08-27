"""Novelty metric and stop predicates for the review-driver loop.

Single source of truth so PROJ-0018 can later import the same functions.
Thresholds (K, max-passes) are passed in by callers from config — this module
does not read TOML.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
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
    """Optional OCR JSON ``summary`` and run-status fields. Missing values stay None."""

    comments: int | None = None
    files_reviewed: int | None = None
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    elapsed: int | float | str | None = None
    status: str | None = None
    message: str | None = None
    model: str | None = None
    failed_requests: int | None = None
    files_failed: int | None = None
    timeout_failures: int | None = None


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
    *max_passes* ``0`` means unlimited (never ``MAX_PASSES``).
    """
    if consecutive_zero_novelty(novelty_history) >= k:
        return StopReason.CONVERGED
    if max_passes > 0 and len(novelty_history) >= max_passes:
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


_ITEMS_FAILED_RE = re.compile(
    r"(\d+)\s+of\s+(\d+)\s+selected item\(s\) failed",
    re.IGNORECASE,
)
_ALL_FILES_FAILED_RE = re.compile(
    r"all\s+(\d+)\s+file review\(s\) failed",
    re.IGNORECASE,
)
_API_KEY_HINT_RE = re.compile(
    r"\s*[—-]\s*check your LLM configuration and API key\s*$",
    re.IGNORECASE,
)


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _files_failed_from_text(text: str | None) -> int | None:
    if not text:
        return None
    match = _ITEMS_FAILED_RE.search(text)
    if match:
        return int(match.group(1))
    match = _ALL_FILES_FAILED_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def _parse_retry_report(data: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return (failed_requests, timeout_failures) from OCR ``retry_report``."""
    report = data.get("retry_report")
    if not isinstance(report, dict):
        return None, None
    failed = _optional_int(report.get("failed_requests"))
    requests = report.get("requests")
    timeout_n = 0
    if isinstance(requests, list):
        for req in requests:
            if not isinstance(req, dict):
                continue
            attempts = req.get("attempts")
            if not isinstance(attempts, list):
                continue
            if any(
                isinstance(attempt, dict) and attempt.get("error_class") == "timeout"
                for attempt in attempts
            ):
                timeout_n += 1
    timeout_failures = timeout_n if timeout_n > 0 else None
    return failed, timeout_failures


def _ocr_run_stats_from_payload(data: dict[str, Any]) -> OcrRunStats:
    summary = data.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    llm = data.get("llm")
    model = None
    if isinstance(llm, dict):
        model = _optional_str(llm.get("model"))
    message = _optional_str(data.get("message"))
    failed_requests, timeout_failures = _parse_retry_report(data)
    files_reviewed = _optional_int(summary.get("files_reviewed"))
    files_failed = _files_failed_from_text(message)
    return OcrRunStats(
        comments=_optional_int(summary.get("comments")),
        files_reviewed=files_reviewed,
        total_tokens=_optional_int(summary.get("total_tokens")),
        input_tokens=_optional_int(summary.get("input_tokens")),
        output_tokens=_optional_int(summary.get("output_tokens")),
        cache_read_tokens=_optional_int(summary.get("cache_read_tokens")),
        elapsed=_optional_elapsed(summary.get("elapsed")),
        status=_optional_str(data.get("status")),
        message=message,
        model=model,
        failed_requests=failed_requests,
        files_failed=files_failed,
        timeout_failures=timeout_failures,
    )


def parse_ocr_run_stats(ocr_json: Path) -> OcrRunStats:
    """Read optional OCR JSON ``summary`` and status fields.

    Fields (all optional): comments, files_reviewed, total_tokens,
    input_tokens, output_tokens, cache_read_tokens, elapsed, status,
    message, model, failed_requests, files_failed, timeout_failures.
    Missing ``summary`` → those numeric fields stay None, not an error.
    """
    data = json.loads(ocr_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return OcrRunStats()
    return _ocr_run_stats_from_payload(data)


def load_ocr_run_stats(ocr_json: Path) -> OcrRunStats:
    """Like :func:`parse_ocr_run_stats` but missing/invalid files yield empty stats."""
    if not ocr_json.is_file():
        return OcrRunStats()
    try:
        return parse_ocr_run_stats(ocr_json)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return OcrRunStats()


def apply_ocr_stderr(stats: OcrRunStats, stderr_tail: str) -> OcrRunStats:
    """Fill status/message/files_failed from OCR stderr when the JSON omitted them."""
    updates: dict[str, Any] = {}
    error_line = _last_error_line(stderr_tail)
    if stats.message is None and error_line is not None:
        updates["message"] = error_line
    if stats.files_failed is None:
        found = _files_failed_from_text(stats.message) or _files_failed_from_text(
            stderr_tail
        )
        if found is not None:
            updates["files_failed"] = found
    if updates:
        return replace(stats, **updates)
    return stats


def _last_error_line(stderr_tail: str) -> str | None:
    for line in reversed(stderr_tail.splitlines()):
        stripped = line.strip()
        if stripped.lower().startswith("error:"):
            body = stripped[6:].strip()
            return body or stripped
    return None


def _strip_api_key_hint(text: str) -> str:
    return _API_KEY_HINT_RE.sub("", text).strip()


def _looks_like_timeout(stats: OcrRunStats, text: str) -> bool:
    if stats.timeout_failures:
        return True
    lowered = text.lower()
    return "deadline exceeded" in lowered or "context deadline" in lowered


def summarize_ocr_failure(
    stats: OcrRunStats,
    stderr_tail: str = "",
    *,
    rc: int | None = None,
) -> str:
    """One-line operator-facing reason for a failed or partial OCR pass."""
    combined = " ".join(part for part in (stats.message, stderr_tail) if part)
    if _looks_like_timeout(stats, combined):
        count = stats.timeout_failures or stats.failed_requests
        if count:
            return (
                f"context deadline exceeded ({count} LLM requests timed out "
                "at the per-request HTTP deadline)"
            )
        return "context deadline exceeded"

    if stats.message:
        return _strip_api_key_hint(stats.message)

    error_line = _last_error_line(stderr_tail)
    if error_line:
        return _strip_api_key_hint(error_line)

    if rc is not None:
        return f"ocr exited rc={rc}"
    return "ocr failed"
