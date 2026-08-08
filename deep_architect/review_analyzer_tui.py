"""Rich Live TUI for review-analyzer interactive runs."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from deep_architect.review_analyzer import (
    AnalysisResult,
    ProgressEvent,
    RunMeta,
    Verdict,
    _finding_lines,
    _finding_path,
)

_VERDICT_STYLE: dict[Verdict, tuple[str, str]] = {
    Verdict.VALID: ("✓", "bold green"),
    Verdict.REJECTED: ("✗", "bold red"),
    Verdict.BACKLOG: ("◷", "bold yellow"),
}

# Rows reserved for header, progress, stats, panel borders, and margins.
_FIXED_ROWS = 14


def format_duration(seconds: float) -> str:
    """Format *seconds* as a compact human-readable duration."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _truncate(text: str, max_len: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"
    return text[: max_len - 1] + "…"


def _location(finding: dict[str, Any]) -> str:
    path = _finding_path(finding)
    lines = _finding_lines(finding)
    # _finding_lines returns markdown backticks like `:10-15`
    if lines:
        bare = lines.strip("`")
        return f"{path}{bare}"
    return path


def _preview(analysis: AnalysisResult, max_len: int = 60) -> str:
    return _truncate(analysis.analysis, max_len)


def _ocr_summary_bits(summary: dict[str, Any]) -> str:
    """Render OCR summary keys that are present as a compact string."""
    if not summary:
        return ""
    preferred = (
        "files_reviewed",
        "comments",
        "warnings",
        "files",
        "total_comments",
        "total_warnings",
    )
    parts: list[str] = []
    seen: set[str] = set()
    for key in preferred:
        if key in summary:
            parts.append(f"{key}: {summary[key]}")
            seen.add(key)
    for key, value in summary.items():
        if key in seen:
            continue
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}: {value}")
    return "  ".join(parts)


def render_dashboard(
    meta: RunMeta,
    counts: dict[str, int],
    completed: int,
    total: int,
    elapsed_s: float,
    results: Sequence[tuple[dict[str, Any], AnalysisResult]],
    *,
    console_height: int = 40,
    console_width: int = 100,
) -> RenderableType:
    """Build a pure Rich renderable for the live dashboard (testable without Live)."""
    # --- Header ---
    header = Text()
    header.append("OCR: ", style="dim")
    header.append(str(meta.ocr_file))
    if meta.ocr_status:
        header.append("   status: ", style="dim")
        header.append(str(meta.ocr_status))
    header.append("\n")
    header.append("Model: ", style="dim")
    header.append(meta.model)
    header.append("   concurrency: ", style="dim")
    header.append(str(meta.concurrency))
    if meta.output_dir is not None and not meta.summary_only:
        header.append("   output: ", style="dim")
        header.append(str(meta.output_dir))
    header.append("\n")
    header.append("Findings: ", style="dim")
    header.append(str(meta.total_findings))
    if meta.raw_findings != meta.total_findings:
        header.append(f" (of {meta.raw_findings} raw)", style="dim")
    ocr_bits = _ocr_summary_bits(meta.ocr_summary)
    if ocr_bits:
        header.append("\n")
        header.append(ocr_bits, style="dim")

    header_panel = Panel(header, title="Review Analyzer", border_style="cyan")

    # --- Progress ---
    total = max(total, 0)
    completed = min(max(completed, 0), total) if total else 0
    fraction = (completed / total) if total else 0.0
    bar = ProgressBar(total=max(total, 1), completed=completed, width=40)

    if completed >= 1 and completed < total and elapsed_s > 0:
        rate = completed / elapsed_s
        eta_s = elapsed_s / completed * (total - completed)
        eta_text = format_duration(eta_s)
        rate_text = f"{rate:.2f}/s"
    elif completed >= total and total > 0:
        rate = completed / elapsed_s if elapsed_s > 0 else 0.0
        eta_text = "0s"
        rate_text = f"{rate:.2f}/s"
    else:
        eta_text = "—"
        rate_text = "—"

    progress_label = Text.assemble(
        ("Analyzing ", "bold"),
        (f"{completed}/{total}", "bold"),
        (f"  ({fraction * 100:.0f}%)", "dim"),
    )
    timing_line = Text.assemble(
        ("Elapsed ", "dim"),
        format_duration(elapsed_s),
        ("  ·  ETA ", "dim"),
        eta_text,
        ("  ·  ", "dim"),
        rate_text,
        (" findings/s" if rate_text != "—" else "", "dim"),
    )
    progress_panel = Panel(
        Group(progress_label, bar, timing_line),
        border_style="blue",
        title="Progress",
    )

    # --- Stats ---
    valid = counts.get(Verdict.VALID.value, 0)
    rejected = counts.get(Verdict.REJECTED.value, 0)
    backlog = counts.get(Verdict.BACKLOG.value, 0)
    pending = max(total - completed, 0)
    stats = Text.assemble(
        ("✓ VALID ", "bold green"),
        (str(valid), "bold green"),
        ("    ", ""),
        ("✗ REJECTED ", "bold red"),
        (str(rejected), "bold red"),
        ("    ", ""),
        ("◷ BACKLOG ", "bold yellow"),
        (str(backlog), "bold yellow"),
        ("    ", ""),
        ("pending ", "dim"),
        (str(pending), "dim"),
    )
    stats_panel = Panel(stats, title="Summary", border_style="magenta")

    # --- Results list (height-aware scrolling window) ---
    max_rows = max(3, console_height - _FIXED_ROWS)
    # Sequence may be a deque (no slice support) — materialize first.
    results_list = list(results)
    visible = results_list[-max_rows:] if results_list else []
    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        pad_edge=False,
        expand=True,
    )
    table.add_column("Verdict", width=12, no_wrap=True)
    table.add_column("Location", ratio=2, overflow="ellipsis")
    table.add_column("Preview", ratio=3, overflow="ellipsis")

    path_width = max(20, min(50, console_width // 3))
    preview_width = max(20, console_width - path_width - 20)

    if not visible:
        table.add_row(Text("…", style="dim"), Text("waiting for results…", style="dim"), "")
    else:
        for finding, analysis in visible:
            icon, style = _VERDICT_STYLE.get(
                analysis.verdict, ("?", "bold white")
            )
            verdict_cell = Text(f"{icon} {analysis.verdict.value.upper()}", style=style)
            loc = _truncate(_location(finding), path_width)
            prev = _preview(analysis, preview_width)
            table.add_row(verdict_cell, loc, prev)

    hidden = max(0, len(results_list) - len(visible))
    title = "Results (newest last)"
    if hidden:
        title = f"Results (newest last; {hidden} older hidden)"
    results_panel = Panel(table, title=title, border_style="green")

    return Group(header_panel, progress_panel, stats_panel, results_panel)


class TuiReporter:
    """Passive Rich Live dashboard for interactive review-analyzer runs."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._meta: RunMeta | None = None
        self._counts: dict[str, int] = {v.value: 0 for v in Verdict}
        self._results: deque[tuple[dict[str, Any], AnalysisResult]] = deque()
        self._completed = 0
        self._total = 0
        self._elapsed_s = 0.0
        self._live: Live | None = None

    def start(self, meta: RunMeta) -> None:
        self._meta = meta
        self._counts = {v.value: 0 for v in Verdict}
        self._results = deque()
        self._completed = 0
        self._total = meta.total_findings
        self._elapsed_s = 0.0
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()

    def on_result(self, event: ProgressEvent) -> None:
        self._completed = event.completed
        self._total = event.total
        self._elapsed_s = event.elapsed_s
        self._counts[event.analysis.verdict.value] = (
            self._counts.get(event.analysis.verdict.value, 0) + 1
        )
        self._results.append((event.finding, event.analysis))
        if self._live is not None:
            self._live.update(self._render())

    def finish(self, counts: dict[str, int]) -> None:
        # Prefer caller-supplied totals if present (should match).
        for key, value in counts.items():
            self._counts[key] = value
        if self._live is not None:
            self._live.update(self._render())
            self._live.stop()
            self._live = None

    def _render(self) -> RenderableType:
        meta = self._meta
        if meta is None:
            return Text("Review Analyzer starting…")
        height = self.console.size.height
        width = self.console.size.width
        return render_dashboard(
            meta,
            self._counts,
            self._completed,
            self._total,
            self._elapsed_s,
            self._results,
            console_height=height,
            console_width=width,
        )
