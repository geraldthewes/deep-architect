from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__OPENCODE_BIN = os.environ.get(
    "OPENCODE_BIN", "/home/gerald/.opencode/bin/opencode"
)


class Verdict(StrEnum):
    """LLM verdict categories for a review finding."""

    VALID = "valid"
    REJECTED = "rejected"
    BACKLOG = "backlog"


@dataclass
class AnalysisResult:
    """Result of LLM analysis for a single finding."""

    verdict: Verdict
    analysis: str
    raw_response: str


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


def call_opencode_analysis(prompt: str, model: str) -> AnalysisResult:
    """Invoke ``opencode run`` and return a structured analysis result."""
    try:
        result = subprocess.run(
            [
                __OPENCODE_BIN,
                "run",
                "--model",
                model,
                "--command",
                prompt,
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return AnalysisResult(
            verdict=Verdict.BACKLOG,
            analysis=f"opencode binary not found: {__OPENCODE_BIN}",
            raw_response="",
        )
    except subprocess.TimeoutExpired:
        return AnalysisResult(
            verdict=Verdict.BACKLOG,
            analysis="opencode execution timed out (>120s)",
            raw_response="",
        )

    if result.returncode != 0:
        return AnalysisResult(
            verdict=Verdict.BACKLOG,
            analysis=f"opencode exited with code {result.returncode}: {result.stderr[:300]}",
            raw_response=result.stderr,
        )

    return _parse_opencode_json(result.stdout)


def analyze_finding(
    finding: dict[str, Any],
    model: str,
    breaker: CircuitBreaker,
) -> AnalysisResult:
    """Analyze a single finding through opencode with circuit-breaker protection."""
    prompt = construct_analysis_prompt(finding)

    def _invoke() -> AnalysisResult:
        return call_opencode_analysis(prompt, model)

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
) -> list[tuple[dict[str, Any], AnalysisResult]]:
    """Process findings through LLM with controlled concurrency."""
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30)
    results: list[tuple[dict[str, Any], AnalysisResult]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_finding = {
            executor.submit(analyze_finding, finding, model, breaker): finding
            for finding in findings
        }

        for i, future in enumerate(as_completed(future_to_finding), 1):
            finding = future_to_finding[future]
            try:
                analysis = future.result(timeout=180)
                results.append((finding, analysis))
            except Exception as exc:
                results.append(
                    (
                        finding,
                        AnalysisResult(
                            verdict=Verdict.BACKLOG,
                            analysis=f"Task exception: {exc}",
                            raw_response="",
                        ),
                    )
                )

            # Progress indicator (every 5 findings)
            if i % 5 == 0:
                print(f"  Processed {i}/{len(findings)} findings...")

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


def write_analysis_files(
    results: list[tuple[dict[str, Any], AnalysisResult]],
    output_dir: Path,
) -> dict[str, int]:
    """Write one markdown file per finding. Returns verdict counts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {v.value: 0 for v in Verdict}

    for finding, analysis in results:
        filename = generate_output_filename(finding)
        output_path = output_dir / filename
        content = generate_markdown_content(finding, analysis)
        try:
            output_path.write_text(content, encoding="utf-8")
            counts[analysis.verdict.value] += 1
        except OSError as exc:
            log.error("Failed to write %s: %s", output_path, exc)
            print(f"Error writing {output_path}: {exc}", file=sys.stderr)

    return counts


def generate_summary_report(
    counts: dict[str, int],
    total: int,
) -> str:
    """Build a human-readable summary of verdict distribution."""
    lines: list[str] = [
        "# Review Analysis Summary",
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


def _run_analysis(
    findings: list[dict[str, Any]],
    model: str,
    concurrency: int,
    output_dir: Path,
    summary_only: bool,
) -> None:
    """End-to-end analysis pipeline: process → write → summary."""
    log.info(
        "Processing %d findings (model=%s, concurrency=%d)",
        len(findings),
        model,
        concurrency,
    )
    print(
        f"Analyzing {len(findings)} findings "
        f"(model={model}, concurrency={concurrency})…"
    )

    results = process_findings_concurrently(findings, model, concurrency)

    if summary_only:
        counts: dict[str, int] = {v.value: 0 for v in Verdict}
        for _, analysis in results:
            counts[analysis.verdict.value] += 1
    else:
        log.info("Writing analysis files to %s", output_dir)
        print(f"Writing reports to {output_dir}/")
        counts = write_analysis_files(results, output_dir)

    summary = generate_summary_report(counts, len(findings))
    print("\n" + summary)

    summary_path = output_dir / "SUMMARY.md"
    try:
        summary_path.write_text(summary, encoding="utf-8")
        print(f"\nSummary written to {summary_path}")
    except OSError as exc:
        log.error("Failed to write summary: %s", exc)
        print(f"Error writing summary: {exc}", file=sys.stderr)


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``review-analyzer``."""
    args = parse_args(argv)

    ocr_data = load_ocr_json(args.ocr_file)
    findings = extract_findings(ocr_data)
    log.info("Loaded %d findings from %s", len(findings), args.ocr_file)
    print(f"Loaded {len(findings)} findings from {args.ocr_file}")

    filtered = filter_findings_by_path(
        findings, args.include or None, args.exclude or None
    )
    log.info("After filtering: %d findings", len(filtered))
    print(f"After filtering: {len(filtered)} findings")

    if not filtered:
        print("No findings to process after filtering.")
        return

    _run_analysis(
        filtered,
        args.model,
        args.concurrency,
        args.output_dir,
        args.summary_only,
    )


if __name__ == "__main__":
    main()
