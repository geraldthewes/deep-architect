from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TextIO

log = logging.getLogger(__name__)

__OPENCODE_BIN = os.environ.get(
    "OPENCODE_BIN", "/home/gerald/.opencode/bin/opencode"
)

# Default wall-clock limit for a single ``opencode run`` attempt (seconds).
# Overridable via ``REVIEW_ANALYZER_TIMEOUT`` env or ``--timeout`` CLI flag.
DEFAULT_OPENCODE_TIMEOUT = 120
# Extra attempts after the first timeout (1 = one retry → two total attempts).
DEFAULT_TIMEOUT_RETRIES = 1

# Global flag for graceful shutdown on SIGINT / TUI stop.
_shutdown_requested = False


def default_opencode_timeout() -> int:
    """Resolve the default opencode timeout from the environment.

    Reads ``REVIEW_ANALYZER_TIMEOUT`` (positive integer seconds). Falls back to
    :data:`DEFAULT_OPENCODE_TIMEOUT` when unset or invalid.
    """
    raw = os.environ.get("REVIEW_ANALYZER_TIMEOUT")
    if raw is None or raw.strip() == "":
        return DEFAULT_OPENCODE_TIMEOUT
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "Invalid REVIEW_ANALYZER_TIMEOUT=%r; using default %ds",
            raw,
            DEFAULT_OPENCODE_TIMEOUT,
        )
        return DEFAULT_OPENCODE_TIMEOUT
    if value < 1:
        log.warning(
            "REVIEW_ANALYZER_TIMEOUT=%d must be >= 1; using default %ds",
            value,
            DEFAULT_OPENCODE_TIMEOUT,
        )
        return DEFAULT_OPENCODE_TIMEOUT
    return value


def request_shutdown() -> None:
    """Request a graceful stop after in-flight analyses finish.

    Used by the SIGINT handler and the full-screen TUI stop binding.
    """
    global _shutdown_requested
    _shutdown_requested = True


def _sigint_handler(signum: int, frame: object) -> None:
    """Signal handler for SIGINT (CTRL-C). Sets shutdown flag and logs."""
    request_shutdown()
    log.info(
        "CTRL-C received, finishing in-flight analyses before shutdown..."
    )


class Verdict(StrEnum):
    """LLM / infrastructure verdict categories for a review finding.

    ``TIMEOUT`` is infrastructure-only: the opencode subprocess hit the wall-
    clock limit (after retries). It is not an intentional LLM triage decision
    and must not be conflated with ``BACKLOG``.
    """

    VALID = "valid"
    REJECTED = "rejected"
    BACKLOG = "backlog"
    TIMEOUT = "timeout"


@dataclass
class AnalysisResult:
    """Result of LLM analysis for a single finding."""

    verdict: Verdict
    analysis: str
    raw_response: str


@dataclass(frozen=True)
class RunMeta:
    """Immutable metadata describing a review-analyzer run (for progress UIs)."""

    ocr_file: Path
    model: str
    concurrency: int
    output_dir: Path | None
    summary_only: bool
    total_findings: int
    raw_findings: int
    ocr_status: str | None = None
    ocr_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressEvent:
    """One completed finding during concurrent analysis."""

    completed: int
    total: int
    finding: dict[str, Any]
    analysis: AnalysisResult
    elapsed_s: float


class ProgressReporter(Protocol):
    """Progress sink used by the plain-text analysis pipeline."""

    def start(self, meta: RunMeta) -> None:
        """Called once before processing begins."""

    def on_result(self, event: ProgressEvent) -> None:
        """Called after each finding completes (may be from a worker thread)."""

    def finish(self, counts: dict[str, int]) -> None:
        """Called after all findings are processed (before or after summary write)."""


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
            f"Analyzing {meta.total_findings} findings "
            f"(model={meta.model}, concurrency={meta.concurrency})…"
        )
        if meta.output_dir is not None and not meta.summary_only:
            print(f"Writing reports to {meta.output_dir}/")

    def on_result(self, event: ProgressEvent) -> None:
        if event.completed % 5 == 0 or event.completed == event.total:
            print(f"  Processed {event.completed}/{event.total} findings...")

    def finish(self, counts: dict[str, int]) -> None:
        # Summary is printed by the pipeline after files are written.
        _ = counts


def load_ocr_json(file_path: Path) -> dict[str, Any]:
    """Load and validate an OCR JSON file.

    Exits with code 1 on file-not-found or invalid JSON.
    """
    if not file_path.is_file():
        log.error("File not found: %s", file_path)
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data: dict[str, Any] = json.loads(file_path.read_text(encoding="utf-8"))
        return data
    except json.JSONDecodeError as exc:
        log.error("Invalid JSON in %s: %s", file_path, exc)
        print(f"Error: Invalid JSON in {file_path}: {exc}", file=sys.stderr)
        sys.exit(1)


def extract_findings(ocr_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten *comments* and *warnings* into a single findings list.

    Each finding gets a ``type`` ("comment" | "warning") and zero-based ``index``
    within its original array.
    """
    findings: list[dict[str, Any]] = []

    for idx, comment in enumerate(ocr_data.get("comments", [])):
        finding = comment.copy()
        finding["type"] = "comment"
        finding["index"] = idx
        findings.append(finding)

    for idx, warning in enumerate(ocr_data.get("warnings", [])):
        finding = warning.copy()
        finding["type"] = "warning"
        finding["index"] = idx
        findings.append(finding)

    return findings


def filter_findings_by_path(
    findings: list[dict[str, Any]],
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return findings whose file path matches the include / exclude globs.

    * If *include_patterns* is given, only findings matching at least one pattern
      are kept.
    * *exclude_patterns* are applied after inclusion (exclude wins).
    * When both are ``None`` / empty the full list is returned.
    """
    if not include_patterns and not exclude_patterns:
        return findings

    import fnmatch  # noqa: PLC0415

    filtered: list[dict[str, Any]] = []
    for finding in findings:
        file_path: str | None = finding.get("path") or finding.get("file")
        if not file_path:
            filtered.append(finding)
            continue

        if include_patterns and not any(
            fnmatch.fnmatch(file_path, pattern) for pattern in include_patterns
        ):
            continue

        if exclude_patterns and any(
            fnmatch.fnmatch(file_path, pattern) for pattern in exclude_patterns
        ):
            continue

        filtered.append(finding)

    return filtered


# ---------------------------------------------------------------------------
# LLM analysis helpers  (Phase 2)
# ---------------------------------------------------------------------------


def get_filepath_hash(filepath: str) -> str:
    """Generate a short SHA-256 hash for a file path."""
    return hashlib.sha256(filepath.encode()).hexdigest()[:8]


class CircuitBreaker:
    """Synchronous circuit breaker for LLM subprocess calls.

    Opens after *failure_threshold* consecutive failures and stays open for
    *recovery_timeout* seconds before allowing a single trial request.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: int = 30,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.state: str = "CLOSED"  # CLOSED | OPEN | HALF_OPEN

    # -- public API --------------------------------------------------------

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute *func* with circuit-breaker protection."""
        if self.state == "OPEN":
            if (
                self.last_failure_time is not None
                and time.time() - self.last_failure_time > self.recovery_timeout
            ):
                self.state = "HALF_OPEN"
            else:
                raise RuntimeError("Circuit breaker is OPEN")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise exc

    # -- internals ---------------------------------------------------------

    def _on_success(self) -> None:
        self.failure_count = 0
        self.state = "CLOSED"

    def _on_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            log.warning(
                "Circuit breaker OPENED (%d consecutive failures)",
                self.failure_count,
            )


def construct_analysis_prompt(finding: dict[str, Any]) -> str:
    """Build an LLM analysis prompt for a single OCR finding."""
    if finding["type"] == "comment":
        return (
            "Analyze this code review comment:\n\n"
            f"**File**: {finding['path']}\n"
            f"**Lines**: {finding['start_line']}-{finding['end_line']}\n"
            f"**Existing Code**:\n```\n{finding.get('existing_code', '(none)')}\n```\n"
            f"**Suggested Code**:\n```\n{finding.get('suggestion_code', '(none)')}\n```\n"
            f"**Review Comment**: {finding['content']}\n\n"
            "Please:\n"
            "1. Confirm the issue: Is this a real problem that needs fixing?\n"
            "2. Explain the issue: Why is this problematic?\n"
            "3. Critique the feedback: Is the suggestion appropriate? Any better alternatives?\n"
            "4. Suggest alternatives if appropriate\n"
            "5. Determine the appropriate testing strategy:\n"
            "   - Red/Green TDD\n"
            "   - New test only\n"
            "   - No new test\n"
            "6. Provide a verdict: VALID, REJECTED, or BACKLOG\n\n"
            "Respond in JSON: {\"verdict\": \"...\", \"analysis\": \"...\"}"
        )
    else:
        return (
            "Analyze this OCR warning:\n\n"
            f"**File**: {finding['file']}\n"
            f"**Message**: {finding['message']}\n"
            f"**Type**: {finding.get('type', 'warning')}\n\n"
            "Please:\n"
            "1. Confirm the issue: Is this a real problem that needs attention?\n"
            "2. Explain the issue: What does this warning indicate?\n"
            "3. Critique the feedback: Actionable or false positive?\n"
            "4. Suggest alternatives if appropriate\n"
            "5. Determine if action is needed: VALID, REJECTED, or BACKLOG\n"
            "6. Provide brief reasoning for your verdict\n\n"
            "Respond in JSON: {\"verdict\": \"...\", \"analysis\": \"...\"}"
        )


def _parse_opencode_json(raw_stdout: str) -> AnalysisResult:
    """Parse opencode --format json output into an AnalysisResult.

    opencode streams NDJSON events; we look for a ``content`` field and then
    try to extract a JSON object from it.
    """
    last_text = ""
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Collect text content from streaming events
        content = event.get("content", "")
        if isinstance(content, str):
            last_text += content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    last_text += block.get("text", "")

        # opencode NDJSON uses {"type": "text", "part": {"type": "text", "text": "..."}}
        part = event.get("part", {})
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text", "")
            if isinstance(text, str):
                last_text += text

    if not last_text:
        return AnalysisResult(
            verdict=Verdict.BACKLOG,
            analysis=f"No text content in opencode output ({raw_stdout[:200]})",
            raw_response=raw_stdout,
        )

    # Try to extract JSON from the text
    json_start = last_text.find("{")
    json_end = last_text.rfind("}") + 1
    if json_start >= 0 and json_end > json_start:
        json_fragment = last_text[json_start:json_end]
        try:
            parsed = json.loads(json_fragment)
            verdict_str = str(parsed.get("verdict", "backlog")).lower()
            try:
                verdict = Verdict(verdict_str)
            except ValueError:
                verdict = Verdict.BACKLOG
            return AnalysisResult(
                verdict=verdict,
                analysis=str(parsed.get("analysis", "No analysis provided")),
                raw_response=raw_stdout,
            )
        except json.JSONDecodeError:
            pass

    return AnalysisResult(
        verdict=Verdict.BACKLOG,
        analysis=f"Could not parse structured JSON from LLM response: {last_text[:300]}",
        raw_response=raw_stdout,
    )


def _run_opencode_once(prompt: str, model: str, *, timeout: int) -> AnalysisResult:
    """Single ``opencode run`` attempt with a wall-clock *timeout* (seconds)."""
    try:
        result = subprocess.run(
            [
                __OPENCODE_BIN,
                "run",
                "--model",
                model,
                "--format",
                "json",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return AnalysisResult(
            verdict=Verdict.BACKLOG,
            analysis=f"opencode binary not found: {__OPENCODE_BIN}",
            raw_response="",
        )
    except subprocess.TimeoutExpired:
        return AnalysisResult(
            verdict=Verdict.TIMEOUT,
            analysis=f"opencode execution timed out (>{timeout}s)",
            raw_response="",
        )

    if result.returncode != 0:
        return AnalysisResult(
            verdict=Verdict.BACKLOG,
            analysis=(
                f"opencode exited with code {result.returncode}: "
                f"{result.stderr[:300]}"
            ),
            raw_response=result.stderr,
        )

    return _parse_opencode_json(result.stdout)


def call_opencode_analysis(
    prompt: str,
    model: str,
    *,
    timeout: int = DEFAULT_OPENCODE_TIMEOUT,
    timeout_retries: int = DEFAULT_TIMEOUT_RETRIES,
) -> AnalysisResult:
    """Invoke ``opencode run`` and return a structured analysis result.

    On timeout, retries up to *timeout_retries* extra times (default: once).
    Exhausted timeouts yield :attr:`Verdict.TIMEOUT` rather than BACKLOG.
    """
    if timeout < 1:
        raise ValueError(f"timeout must be >= 1, got {timeout}")
    retries = max(0, timeout_retries)
    attempts = 1 + retries
    last: AnalysisResult | None = None

    for attempt in range(1, attempts + 1):
        result = _run_opencode_once(prompt, model, timeout=timeout)
        if result.verdict != Verdict.TIMEOUT:
            return result
        last = result
        if attempt < attempts:
            log.warning(
                "opencode timed out after %ds (attempt %d/%d); retrying",
                timeout,
                attempt,
                attempts,
            )

    assert last is not None
    if attempts > 1:
        return AnalysisResult(
            verdict=Verdict.TIMEOUT,
            analysis=(
                f"opencode execution timed out (>{timeout}s) "
                f"after {attempts} attempts"
            ),
            raw_response=last.raw_response,
        )
    return last


def analyze_finding(
    finding: dict[str, Any],
    model: str,
    breaker: CircuitBreaker,
    *,
    timeout: int = DEFAULT_OPENCODE_TIMEOUT,
    timeout_retries: int = DEFAULT_TIMEOUT_RETRIES,
) -> AnalysisResult:
    """Analyze a single finding through opencode with circuit-breaker protection."""
    prompt = construct_analysis_prompt(finding)

    def _invoke() -> AnalysisResult:
        return call_opencode_analysis(
            prompt,
            model,
            timeout=timeout,
            timeout_retries=timeout_retries,
        )

    try:
        result: AnalysisResult = breaker.call(_invoke)
        return result
    except Exception as exc:
        return AnalysisResult(
            verdict=Verdict.BACKLOG,
            analysis=f"Circuit breaker / subprocess error: {exc}",
            raw_response="",
        )


def process_findings_concurrently(
    findings: list[dict[str, Any]],
    model: str,
    max_workers: int,
    output_dir: Path | None = None,
    on_result: Callable[[ProgressEvent], None] | None = None,
    *,
    timeout: int = DEFAULT_OPENCODE_TIMEOUT,
    timeout_retries: int = DEFAULT_TIMEOUT_RETRIES,
) -> list[tuple[dict[str, Any], AnalysisResult]]:
    """Process findings through LLM with controlled concurrency, writing
    each result to disk immediately (when *output_dir* is given) so progress
    is preserved on crash.

    *on_result* is invoked after each finding completes (caller thread of
    :func:`as_completed` — typically a worker thread when the Textual TUI
    runs the pipeline).

    When :func:`request_shutdown` has been called, pending (not-yet-started)
    futures are cancelled; in-flight analyses are still collected.
    """
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30)
    results: list[tuple[dict[str, Any], AnalysisResult]] = []
    total = len(findings)
    t0 = time.monotonic()
    cancel_pending = False
    # Allow wall time for all attempts plus a small grace period for process teardown.
    future_wait = timeout * (1 + max(0, timeout_retries)) + 60

    def _analyze_one(finding: dict[str, Any]) -> AnalysisResult | None:
        # Skip work queued after a graceful stop (cancel alone is not always
        # enough for ThreadPoolExecutor work still sitting in the queue).
        if _shutdown_requested:
            return None
        return analyze_finding(
            finding,
            model,
            breaker,
            timeout=timeout,
            timeout_retries=timeout_retries,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_finding = {
            executor.submit(_analyze_one, finding): finding for finding in findings
        }

        for future in as_completed(future_to_finding):
            if future.cancelled():
                continue

            finding = future_to_finding[future]
            try:
                analysis = future.result(timeout=future_wait)
            except Exception as exc:
                analysis = AnalysisResult(
                    verdict=Verdict.BACKLOG,
                    analysis=f"Task exception: {exc}",
                    raw_response="",
                )

            if analysis is None:
                # Worker saw the shutdown flag before starting the LLM call.
                continue

            results.append((finding, analysis))

            # Write file immediately so progress survives a crash
            if output_dir is not None:
                try:
                    filename = generate_output_filename(finding)
                    output_path = output_dir / filename
                    content = generate_markdown_content(finding, analysis)
                    output_path.write_text(content, encoding="utf-8")
                except OSError as exc:
                    log.error("Failed to write %s: %s", output_path, exc)
                    print(f"Error writing {output_path}: {exc}", file=sys.stderr)

            if on_result is not None:
                on_result(
                    ProgressEvent(
                        completed=len(results),
                        total=total,
                        finding=finding,
                        analysis=analysis,
                        elapsed_s=time.monotonic() - t0,
                    )
                )

            if _shutdown_requested and not cancel_pending:
                cancel_pending = True
                log.info(
                    "Shutdown requested — cancelling pending analyses "
                    "(%d completed so far)",
                    len(results),
                )
                for pending in future_to_finding:
                    if not pending.done():
                        pending.cancel()

    return results


# ---------------------------------------------------------------------------
# Output generation  (Phase 3)
# ---------------------------------------------------------------------------


def generate_output_filename(finding: dict[str, Any]) -> str:
    """Generate ``{filepath_hash}-{item_index}.md`` for a finding."""
    if finding["type"] == "comment":
        filepath = finding["path"]
        item_index = finding["index"]
    else:
        filepath = finding["file"]
        item_index = finding["index"]
    return f"{get_filepath_hash(filepath)}-{item_index}.md"


def is_timeout_report(file_path: Path) -> bool:
    """Return True if *file_path* is a timeout analysis report.

    Matches:
    - Current format: ``**Verdict**: TIMEOUT``
    - Legacy format: ``BACKLOG`` whose analysis text mentions opencode timed out
    """
    # Local import avoids a circular dependency at module load time.
    from deep_architect.feedback_report import (  # noqa: PLC0415
        NON_FINDING_FILES,
        get_verdict,
    )

    if file_path.name in NON_FINDING_FILES:
        return False
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Failed to read %s for timeout check: %s", file_path, exc)
        return False

    verdict = get_verdict(file_path)
    if verdict == "TIMEOUT":
        return True
    if verdict == "BACKLOG":
        lower = content.lower()
        return "timed out" in lower and "opencode" in lower
    return False


def select_timeout_findings_for_retry(
    findings: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Return OCR findings whose prior report in *output_dir* timed out.

    Matching is by output filename (``{hash}-{index}.md``), so include/exclude
    filters applied to *findings* still apply.
    """
    if not output_dir.is_dir():
        return []

    timeout_names: set[str] = set()
    for md_path in output_dir.glob("*.md"):
        if is_timeout_report(md_path):
            timeout_names.add(md_path.name)

    if not timeout_names:
        return []

    return [
        finding
        for finding in findings
        if generate_output_filename(finding) in timeout_names
    ]


def tally_output_dir_verdicts(output_dir: Path) -> dict[str, int]:
    """Count verdicts from all finding markdown files in *output_dir*."""
    from deep_architect.feedback_report import (  # noqa: PLC0415
        NON_FINDING_FILES,
        get_verdict,
    )

    counts: dict[str, int] = {v.value: 0 for v in Verdict}
    if not output_dir.is_dir():
        return counts

    for md_path in sorted(output_dir.glob("*.md")):
        if md_path.name in NON_FINDING_FILES:
            continue
        verdict = get_verdict(md_path)
        if verdict is None:
            continue
        key = verdict.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def generate_markdown_content(
    finding: dict[str, Any],
    analysis_result: AnalysisResult,
) -> str:
    """Build markdown report for a single finding + its LLM analysis."""
    from datetime import datetime  # noqa: PLC0415

    timestamp = datetime.now(UTC).isoformat()
    lines: list[str] = [
        "# OCR Review Analysis",
        "",
        f"**Timestamp**: {timestamp}",
        "",
        "**Original OCR Finding**:",
        "",
    ]

    if finding["type"] == "comment":
        lines.extend(
            [
                f"- **File**: {finding['path']}",
                f"- **Lines**: {finding['start_line']}-{finding['end_line']}",
                "- **Type**: Comment",
            ]
        )
        existing = finding.get("existing_code")
        if existing:
            lines.append(f"- **Existing Code**:\n```\n{existing}\n```\n")
        suggested = finding.get("suggestion_code")
        if suggested:
            lines.append(f"- **Suggested Code**:\n```\n{suggested}\n```\n")
        lines.append(f"- **Review Comment**: {finding['content']}")
    else:
        lines.extend(
            [
                f"- **File**: {finding['file']}",
                f"- **Type**: Warning ({finding.get('warning_type', 'unknown')})",
                f"- **Message**: {finding['message']}",
            ]
        )

    lines.extend(
        [
            "",
            "## LLM Analysis",
            "",
            f"**Verdict**: {analysis_result.verdict.value.upper()}",
            "",
            "**Analysis**:",
            "",
            analysis_result.analysis,
            "",
            "---",
            "",
            "*Generated by review-analyzer.*",
        ]
    )

    return "\n".join(lines) + "\n"





def generate_summary_report(
    counts: dict[str, int],
    total: int,
    *,
    model: str,
) -> str:
    """Build a human-readable summary of verdict distribution.

    *model* is the opencode model id used for analysis; the summary always
    labels the backend as ``opencode`` (the only analyzer backend today).
    """
    lines: list[str] = [
        "# Review Analysis Summary",
        "",
        f"Coding agent: opencode ({model})",
        "",
        f"Total findings processed: {total}",
        "",
        "Breakdown by verdict:",
    ]
    for verdict in Verdict:
        count = counts.get(verdict.value, 0)
        pct = (count / total * 100) if total else 0
        lines.append(f"- {verdict.value.upper()}: {count} ({pct:.1f}%)")
    return "\n".join(lines) + "\n"


def _finding_path(finding: dict[str, Any]) -> str:
    """Return the file path string for a finding."""
    if finding["type"] == "comment":
        return str(finding.get("path", "(unknown)"))
    return str(finding.get("file", "(unknown)"))


def _finding_lines(finding: dict[str, Any]) -> str:
    """Return the line range string for a comment finding, or empty for warnings."""
    if finding["type"] == "comment":
        start = finding.get("start_line")
        end = finding.get("end_line")
        if start is not None and end is not None:
            return f"`:{start}-{end}`"
    return ""


def generate_index_report(
    results: list[tuple[dict[str, Any], AnalysisResult]],
) -> str:
    """Build an INDEX.md listing all findings grouped by verdict."""
    grouped: dict[str, list[tuple[dict[str, Any], AnalysisResult]]] = {
        v.value: [] for v in Verdict
    }
    for finding, analysis in results:
        grouped[analysis.verdict.value].append((finding, analysis))

    lines: list[str] = [
        "# Review Findings Index",
        "",
    ]

    for verdict in Verdict:
        entries = grouped[verdict.value]
        if not entries:
            continue

        lines.append(f"## {verdict.value.upper()} ({len(entries)})")
        lines.append("")
        lines.append("| # | File | Lines | Report | Analysis Preview |")
        lines.append("|---|------|-------|--------|------------------|")

        for i, (finding, analysis) in enumerate(entries, 1):
            path = _finding_path(finding)
            ln = _finding_lines(finding)
            report = generate_output_filename(finding)
            preview = analysis.analysis[:120].replace("|", "\\|").replace("\n", " ")
            if len(analysis.analysis) > 120:
                preview += "…"
            lines.append(
                f"| {i} | `{path}` | {ln} | "
                f"[{report}]({report}) | {preview} |"
            )

        lines.append("")

    return "\n".join(lines) + "\n"


def _force_tui_from_args(args: argparse.Namespace) -> bool | None:
    """Map ``--tui`` / ``--no-tui`` to a force flag for :func:`should_use_tui`."""
    if getattr(args, "tui", False):
        return True
    if getattr(args, "no_tui", False):
        return False
    return None


def _tally_counts(
    results: list[tuple[dict[str, Any], AnalysisResult]],
) -> dict[str, int]:
    """Count results by verdict."""
    counts: dict[str, int] = {v.value: 0 for v in Verdict}
    for _, analysis in results:
        counts[analysis.verdict.value] = counts.get(analysis.verdict.value, 0) + 1
    return counts


def _emit_summary_outputs(
    results: list[tuple[dict[str, Any], AnalysisResult]],
    counts: dict[str, int],
    *,
    model: str,
    output_dir: Path,
    summary_only: bool,
    total_findings: int,
) -> None:
    """Print and optionally write SUMMARY.md / INDEX.md after analysis.

    When writing to *output_dir*, SUMMARY counts are tallied from disk so a
    ``--retry-timeouts`` pass still reflects the full feedback directory.
    INDEX uses the in-memory *results* for a full run; on partial/retry runs
    remaining files stay on disk and INDEX is rebuilt from the current
    *results* union is not attempted — INDEX lists only the just-processed
    batch when results are a subset. Prefer disk tally for SUMMARY always
    when files were written.
    """
    if not summary_only:
        log.info("Per-finding reports written to %s", output_dir)

    if not summary_only and output_dir.is_dir():
        disk_counts = tally_output_dir_verdicts(output_dir)
        # Prefer disk when it has at least as many findings as this run.
        disk_total = sum(disk_counts.get(v.value, 0) for v in Verdict)
        run_total = sum(counts.get(v.value, 0) for v in Verdict)
        if disk_total >= run_total and disk_total > 0:
            summary_counts = disk_counts
            summary_total = disk_total
        else:
            summary_counts = counts
            summary_total = run_total if run_total else total_findings
    else:
        summary_counts = counts
        processed = sum(counts.get(v.value, 0) for v in Verdict)
        # Prefer actual processed count (partial run on interrupt) over planned total.
        summary_total = processed if processed else total_findings

    summary = generate_summary_report(summary_counts, summary_total, model=model)
    print("\n" + summary)

    if not summary_only:
        summary_path = output_dir / "SUMMARY.md"
        try:
            summary_path.write_text(summary, encoding="utf-8")
            print(f"\nSummary written to {summary_path}")
        except OSError as exc:
            log.error("Failed to write summary: %s", summary_path, exc)
            print(f"Error writing summary: {exc}", file=sys.stderr)

        # For INDEX: when the run covered only a subset (retry), rebuild from
        # every on-disk finding by reusing filename stems + verdict text.
        if results and len(results) >= summary_total:
            index = generate_index_report(results)
        else:
            index = generate_index_report_from_output_dir(output_dir, results)
        index_path = output_dir / "INDEX.md"
        try:
            index_path.write_text(index, encoding="utf-8")
            print(f"Index written to {index_path}")
        except OSError as exc:
            log.error("Failed to write index: %s", index_path, exc)
            print(f"Error writing index: {exc}", file=sys.stderr)


def generate_index_report_from_output_dir(
    output_dir: Path,
    fresh_results: list[tuple[dict[str, Any], AnalysisResult]] | None = None,
) -> str:
    """Build INDEX.md from existing feedback files, preferring *fresh_results*.

    Used after ``--retry-timeouts`` so INDEX covers the whole directory while
    still using full finding metadata for just-reprocessed items.
    """
    from deep_architect.feedback_report import (  # noqa: PLC0415
        NON_FINDING_FILES,
        get_verdict,
        parse_markdown_finding,
    )

    fresh_by_name: dict[str, tuple[dict[str, Any], AnalysisResult]] = {}
    if fresh_results:
        for fresh_finding, fresh_analysis in fresh_results:
            name = generate_output_filename(fresh_finding)
            fresh_by_name[name] = (fresh_finding, fresh_analysis)

    combined: list[tuple[dict[str, Any], AnalysisResult]] = []
    if not output_dir.is_dir():
        return generate_index_report(list(fresh_by_name.values()))

    for md_path in sorted(output_dir.glob("*.md")):
        if md_path.name in NON_FINDING_FILES:
            continue
        if md_path.name in fresh_by_name:
            combined.append(fresh_by_name[md_path.name])
            continue

        verdict_str = get_verdict(md_path)
        if verdict_str is None:
            continue
        try:
            verdict = Verdict(verdict_str.lower())
        except ValueError:
            verdict = Verdict.BACKLOG

        parsed = parse_markdown_finding(md_path)
        disk_finding: dict[str, Any]
        if parsed is not None:
            disk_finding = {
                "type": "comment",
                "path": str(parsed.file_path),
                "start_line": parsed.line_start,
                "end_line": parsed.line_end,
                "index": 0,
                "content": parsed.review_comment,
            }
            # Recover index from filename stem when possible (hash-index).
            stem = md_path.stem
            if "-" in stem:
                maybe_idx = stem.rsplit("-", 1)[-1]
                if maybe_idx.isdigit():
                    disk_finding["index"] = int(maybe_idx)
            disk_analysis = AnalysisResult(
                verdict=verdict,
                analysis=parsed.analysis,
                raw_response="",
            )
        else:
            disk_finding = {
                "type": "comment",
                "path": "(unknown)",
                "start_line": None,
                "end_line": None,
                "index": 0,
                "content": "",
            }
            disk_analysis = AnalysisResult(
                verdict=verdict,
                analysis="",
                raw_response="",
            )
        combined.append((disk_finding, disk_analysis))

    return generate_index_report(combined)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Process OCR JSON findings with LLM analysis for triage",
    )
    parser.add_argument(
        "ocr_file",
        type=Path,
        help="Path to OCR JSON output file",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Glob patterns to include (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob patterns to exclude (repeatable)",
    )
    parser.add_argument(
        "--model",
        default="standard/coder",
        help="Model identifier for opencode (default: standard/coder)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Maximum concurrent LLM requests (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Wall-clock limit per opencode attempt in seconds "
            f"(default: env REVIEW_ANALYZER_TIMEOUT or {DEFAULT_OPENCODE_TIMEOUT}; "
            f"timed-out calls retry once by default)"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("feedback"),
        help="Directory for per-finding markdown reports (default: feedback/)",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print summary counts without writing per-finding files",
    )
    parser.add_argument(
        "--retry-timeouts",
        action="store_true",
        help=(
            "Re-analyze only findings whose prior report in --output-dir is "
            "TIMEOUT (or legacy timed-out BACKLOG); requires existing reports"
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


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``review-analyzer``.

    Returns 0 on success, 130 if interrupted, 1 on other failures that still
    produce partial output (reserved for future use).
    """
    global _shutdown_requested
    _shutdown_requested = False
    signal.signal(signal.SIGINT, _sigint_handler)

    args = parse_args(argv)

    force_tui = _force_tui_from_args(args)
    use_tui = should_use_tui(force_tui=force_tui)

    log_level = logging.INFO
    # Console handlers stay until the Textual app mounts, so early setup
    # messages remain visible. The app then detaches stream handlers and
    # routes logs into its Log pane (and review-analyzer.log).
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    timeout_seconds = (
        args.timeout if args.timeout is not None else default_opencode_timeout()
    )
    if timeout_seconds < 1:
        print(
            f"Error: --timeout must be >= 1 (got {timeout_seconds})",
            file=sys.stderr,
        )
        return 1

    ocr_data = load_ocr_json(args.ocr_file)
    findings = extract_findings(ocr_data)
    log.info("Loaded %d findings from %s", len(findings), args.ocr_file)
    print(f"Loaded {len(findings)} findings from {args.ocr_file}")

    filtered = filter_findings_by_path(
        findings, args.include or None, args.exclude or None
    )
    log.info("After filtering: %d findings", len(filtered))
    print(f"After filtering: {len(filtered)} findings")

    if args.retry_timeouts:
        if args.summary_only:
            print(
                "Error: --retry-timeouts requires writing to --output-dir "
                "(incompatible with --summary-only)",
                file=sys.stderr,
            )
            return 1
        if not args.output_dir.is_dir():
            print(
                f"Error: --retry-timeouts needs existing reports in "
                f"{args.output_dir}/ (directory missing)",
                file=sys.stderr,
            )
            return 1
        before_retry = len(filtered)
        filtered = select_timeout_findings_for_retry(filtered, args.output_dir)
        log.info(
            "Retry-timeouts: %d of %d filtered findings timed out previously",
            len(filtered),
            before_retry,
        )
        print(
            f"Retry-timeouts: re-analyzing {len(filtered)} timed-out finding(s) "
            f"(of {before_retry} after path filters)"
        )

    if not filtered:
        if args.retry_timeouts:
            print("No timed-out findings to re-analyze.")
        else:
            print("No findings to process after filtering.")
        return 0

    ocr_summary_raw = ocr_data.get("summary")
    ocr_summary: dict[str, Any] = (
        ocr_summary_raw if isinstance(ocr_summary_raw, dict) else {}
    )
    ocr_status = ocr_data.get("status")
    meta = RunMeta(
        ocr_file=args.ocr_file,
        model=args.model,
        concurrency=args.concurrency,
        output_dir=None if args.summary_only else args.output_dir,
        summary_only=args.summary_only,
        total_findings=len(filtered),
        raw_findings=len(findings),
        ocr_status=str(ocr_status) if ocr_status is not None else None,
        ocr_summary=ocr_summary,
    )

    output_dir_for_write: Path | None = (
        None if args.summary_only else args.output_dir
    )
    results_box: list[tuple[dict[str, Any], AnalysisResult]] = []

    def _run_pipeline(
        on_result: Callable[[ProgressEvent], None],
    ) -> dict[str, int]:
        log.info(
            "Processing %d findings (model=%s, concurrency=%d, timeout=%ds)",
            len(filtered),
            args.model,
            args.concurrency,
            timeout_seconds,
        )
        results = process_findings_concurrently(
            filtered,
            args.model,
            args.concurrency,
            output_dir_for_write,
            on_result=on_result,
            timeout=timeout_seconds,
            timeout_retries=DEFAULT_TIMEOUT_RETRIES,
        )
        results_box[:] = results
        counts = _tally_counts(results)
        counts["total_findings"] = meta.total_findings
        if _shutdown_requested:
            counts["interrupted"] = 1
        return counts

    if use_tui:
        # Lazy import keeps plain mode free of Textual at import time.
        from deep_architect.review_analyzer_tui import (  # noqa: PLC0415
            run_review_analyzer_tui,
        )

        log_file = (
            None
            if args.summary_only
            else args.output_dir / "review-analyzer.log"
        )
        counts = run_review_analyzer_tui(
            meta,
            _run_pipeline,
            log_level=log_level,
            log_file=log_file,
        )
        results = list(results_box)
        # Prefer authoritative pipeline tallies when available; fall back to
        # UI-tracked counts from the app return value.
        if results:
            counts = _tally_counts(results)
            counts["total_findings"] = meta.total_findings
            if _shutdown_requested or counts.get("interrupted"):
                counts["interrupted"] = 1
    else:
        reporter: ProgressReporter = PlainReporter()
        reporter.start(meta)
        try:
            counts = _run_pipeline(reporter.on_result)
        except BaseException:
            reporter.finish({v.value: 0 for v in Verdict})
            raise
        else:
            reporter.finish(counts)
        results = list(results_box)

    _emit_summary_outputs(
        results,
        counts,
        model=args.model,
        output_dir=args.output_dir,
        summary_only=args.summary_only,
        total_findings=meta.total_findings,
    )

    return 130 if counts.get("interrupted") else 0


if __name__ == "__main__":
    sys.exit(main())

