"""Load and parse review-analyzer feedback directories.

Shared helpers used by ``review-action`` and ``review-feedback-browse``.
No UI or agent dependencies.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from deep_architect.logger import get_logger

logger = get_logger(__name__)

# Files in a feedback dir that are not findings and must never be processed
# as findings (includes review-action's own summary file).
NON_FINDING_FILES = frozenset({"SUMMARY.md", "INDEX.md", "review-action_summary.md"})

# Preferred display / sort order for known verdicts.
VERDICT_ORDER: tuple[str, ...] = ("VALID", "REJECTED", "BACKLOG", "UNKNOWN")

DEFAULT_FEEDBACK_DIR = Path("feedback")


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


@dataclass(frozen=True)
class FeedbackFinding:
    """A finding ready for browsing (includes verdict and disk path)."""

    path: Path
    finding_id: str
    source_file: str
    line_start: int | None
    line_end: int | None
    verdict: str
    existing_code: str
    suggested_code: str
    review_comment: str
    analysis: str
    raw_markdown: str


@dataclass(frozen=True)
class FeedbackReport:
    """All findings loaded from a review-analyzer output directory."""

    directory: Path
    summary_text: str | None
    findings: list[FeedbackFinding]
    counts: dict[str, int]


def parse_markdown_finding(file_path: Path) -> ReviewFinding | None:
    """Parse a review-analyzer markdown file into a ReviewFinding."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read %s: %s", file_path, exc)
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
    except (TypeError, ValueError) as exc:
        logger.error(
            "Invalid file path '%s' in %s: %s", file_str, file_path, exc
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
    """Return the finding's verdict (\"VALID\"/\"REJECTED\"/\"BACKLOG\"), or None."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read %s for verdict: %s", file_path, exc)
        return None

    verdict_match = re.search(
        r"\*\*Verdict\*\*:?\s*(VALID|REJECTED|BACKLOG)", content
    )
    return verdict_match.group(1) if verdict_match else None


def is_valid_finding(file_path: Path) -> bool:
    """Check if a markdown file contains a VALID verdict."""
    return get_verdict(file_path) == "VALID"


def _verdict_sort_key(verdict: str) -> tuple[int, str]:
    try:
        return (VERDICT_ORDER.index(verdict), verdict)
    except ValueError:
        return (len(VERDICT_ORDER), verdict)


def _finding_sort_key(f: FeedbackFinding) -> tuple[tuple[int, str], str, int, str]:
    line = f.line_start if f.line_start is not None else -1
    return (_verdict_sort_key(f.verdict), f.source_file, line, f.finding_id)


def findings_for_verdict(
    report: FeedbackReport, verdict: str
) -> list[FeedbackFinding]:
    """Return findings for *verdict* in stable sort order."""
    return [f for f in report.findings if f.verdict == verdict]


def analysis_preview(text: str, max_len: int = 80) -> str:
    """Single-line truncated preview of analysis text."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    if max_len <= 1:
        return "…"
    return collapsed[: max_len - 1] + "…"


def line_range_label(line_start: int | None, line_end: int | None) -> str:
    """Human-readable line range, or empty string if unknown."""
    if line_start is None:
        return ""
    if line_end is None or line_end == line_start:
        return f":{line_start}"
    return f":{line_start}-{line_end}"


def load_feedback_dir(directory: Path) -> FeedbackReport:
    """Load all findings from a review-analyzer output directory.

    Raises:
        FileNotFoundError: if *directory* does not exist.
        NotADirectoryError: if *directory* is not a directory.
    """
    if not directory.exists():
        raise FileNotFoundError(f"Feedback directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    summary_path = directory / "SUMMARY.md"
    summary_text: str | None = None
    if summary_path.is_file():
        try:
            summary_text = summary_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read summary %s: %s", summary_path, exc)

    findings: list[FeedbackFinding] = []
    for md_path in sorted(directory.glob("*.md")):
        if md_path.name in NON_FINDING_FILES:
            continue

        try:
            raw = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read finding %s: %s", md_path, exc)
            continue

        verdict = get_verdict(md_path) or "UNKNOWN"
        parsed = parse_markdown_finding(md_path)

        if parsed is not None:
            findings.append(
                FeedbackFinding(
                    path=md_path,
                    finding_id=parsed.finding_id,
                    source_file=str(parsed.file_path),
                    line_start=parsed.line_start,
                    line_end=parsed.line_end,
                    verdict=verdict,
                    existing_code=parsed.existing_code,
                    suggested_code=parsed.suggested_code,
                    review_comment=parsed.review_comment,
                    analysis=parsed.analysis,
                    raw_markdown=raw,
                )
            )
        else:
            # Degraded entry so timeouts / partial reports still appear in the browser.
            logger.warning(
                "Using degraded parse for %s (missing structured sections)", md_path
            )
            findings.append(
                FeedbackFinding(
                    path=md_path,
                    finding_id=md_path.stem,
                    source_file="(unknown)",
                    line_start=None,
                    line_end=None,
                    verdict=verdict,
                    existing_code="",
                    suggested_code="",
                    review_comment="",
                    analysis="",
                    raw_markdown=raw,
                )
            )

    findings.sort(key=_finding_sort_key)
    counts = dict(Counter(f.verdict for f in findings))

    return FeedbackReport(
        directory=directory,
        summary_text=summary_text,
        findings=findings,
        counts=counts,
    )
