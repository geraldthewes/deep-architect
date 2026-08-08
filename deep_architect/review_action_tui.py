"""Rich Live TUI for review-action interactive runs."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from deep_architect.review_action_harness import ProgressEvent, RunMeta

_OUTCOME_STYLE: dict[str, tuple[str, str]] = {
    "completed": ("✓", "bold green"),
    "dry-run": ("✓", "bold cyan"),
    "error": ("✗", "bold red"),
    "skipped": ("◷", "bold yellow"),
    "restored": ("↻", "dim"),
    "rejected": ("—", "bold yellow"),
    "interrupted": ("!", "bold magenta"),
    "in-progress": ("…", "bold blue"),
}

# Rows reserved for header, progress, stats, panel borders, and margins.
_FIXED_ROWS = 16


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


def render_dashboard(
    meta: RunMeta,
    stats: dict[str, int],
    completed: int,
    total: int,
    elapsed_s: float,
    results: Sequence[ProgressEvent],
    *,
    current_phase: str | None = None,
    current_finding: str | None = None,
    console_height: int = 40,
    console_width: int = 100,
) -> RenderableType:
    """Build a pure Rich renderable for the live dashboard (testable without Live)."""
    # --- Header ---
    header = Text()
    header.append("Feedback: ", style="dim")
    header.append(str(meta.output_dir))
    header.append("\n")
    header.append("Agent: ", style="dim")
    header.append(meta.coding_agent)
    header.append("   findings: ", style="dim")
    header.append(str(meta.total_findings))
    flags: list[str] = []
    if meta.dry_run:
        flags.append("dry-run")
    if meta.force:
        flags.append("force")
    if meta.skip_errors:
        flags.append("skip-errors")
    if flags:
        header.append("\n")
        header.append("Flags: ", style="dim")
        header.append(", ".join(flags))

    header_panel = Panel(header, title="Review Action", border_style="cyan")

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
        ("Processing ", "bold"),
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
    phase_bits: list[RenderableType] = [progress_label, bar, timing_line]
    if current_finding or current_phase:
        phase_line = Text()
        phase_line.append("Current: ", style="dim")
        if current_finding:
            phase_line.append(current_finding, style="bold")
        if current_phase:
            if current_finding:
                phase_line.append("  ·  ", style="dim")
            phase_line.append(current_phase, style="bold blue")
        phase_bits.append(phase_line)

    progress_panel = Panel(
        Group(*phase_bits),
        border_style="blue",
        title="Progress",
    )

    # --- Stats ---
    committed = stats.get("committed", 0)
    skipped = stats.get("skipped", 0)
    errors = stats.get("errors", 0)
    restored = stats.get("restored", 0)
    pending = max(total - completed, 0)
    stats_text = Text.assemble(
        ("✓ Fixed ", "bold green"),
        (str(committed), "bold green"),
        ("    ", ""),
        ("◷ Skipped ", "bold yellow"),
        (str(skipped), "bold yellow"),
        ("    ", ""),
        ("✗ Errors ", "bold red"),
        (str(errors), "bold red"),
        ("    ", ""),
        ("↻ Restored ", "dim"),
        (str(restored), "dim"),
        ("    ", ""),
        ("pending ", "dim"),
        (str(pending), "dim"),
    )
    stats_panel = Panel(stats_text, title="Summary", border_style="magenta")

    # --- Results list (height-aware scrolling window) ---
    max_rows = max(3, console_height - _FIXED_ROWS)
    results_list = list(results)
    visible = results_list[-max_rows:] if results_list else []
    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        pad_edge=False,
        expand=True,
    )
    table.add_column("Outcome", width=12, no_wrap=True)
    table.add_column("Finding", width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("File", ratio=2, overflow="ellipsis")
    table.add_column("Detail", ratio=3, overflow="ellipsis")

    path_width = max(16, min(40, console_width // 4))
    detail_width = max(20, console_width - path_width - 36)

    if not visible:
        table.add_row(
            Text("…", style="dim"),
            Text("", style="dim"),
            Text("waiting for results…", style="dim"),
            "",
        )
    else:
        for event in visible:
            icon, style = _OUTCOME_STYLE.get(event.outcome, ("?", "bold white"))
            outcome_cell = Text(f"{icon} {event.outcome}", style=style)
            finding_cell = _truncate(event.finding_id, 16)
            file_cell = _truncate(event.file_path, path_width)
            detail = event.summary
            if event.commit_sha:
                detail = f"`{event.commit_sha}` {detail}"
            detail_cell = _truncate(detail, detail_width)
            table.add_row(outcome_cell, finding_cell, file_cell, detail_cell)

    hidden = max(0, len(results_list) - len(visible))
    title = "Results (newest last)"
    if hidden:
        title = f"Results (newest last; {hidden} older hidden)"
    results_panel = Panel(table, title=title, border_style="green")

    return Group(header_panel, progress_panel, stats_panel, results_panel)


class TuiReporter:
    """Passive Rich Live dashboard for interactive review-action runs."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._meta: RunMeta | None = None
        self._stats: dict[str, int] = {
            "processed": 0,
            "committed": 0,
            "skipped": 0,
            "errors": 0,
            "restored": 0,
            "total_findings": 0,
        }
        self._results: deque[ProgressEvent] = deque()
        self._completed = 0
        self._total = 0
        self._elapsed_s = 0.0
        self._current_phase: str | None = None
        self._current_finding: str | None = None
        self._live: Live | None = None

    def start(self, meta: RunMeta) -> None:
        self._meta = meta
        self._stats = {
            "processed": 0,
            "committed": 0,
            "skipped": 0,
            "errors": 0,
            "restored": 0,
            "total_findings": meta.total_findings,
        }
        self._results = deque()
        self._completed = 0
        self._total = meta.total_findings
        self._elapsed_s = 0.0
        self._current_phase = None
        self._current_finding = None
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
        if event.stats:
            self._stats.update(event.stats)
        self._results.append(event)
        self._current_phase = None
        self._current_finding = None
        if self._live is not None:
            self._live.update(self._render())

    def on_phase(self, event: ProgressEvent) -> None:
        self._elapsed_s = event.elapsed_s
        self._current_phase = event.phase or event.summary
        self._current_finding = event.finding_id
        if event.stats:
            self._stats.update(event.stats)
        if self._live is not None:
            self._live.update(self._render())

    def finish(self, stats: dict[str, int]) -> None:
        for key, value in stats.items():
            if key == "interrupted":
                continue
            if isinstance(value, bool):
                self._stats[key] = int(value)
            else:
                self._stats[key] = int(value)
        if self._live is not None:
            self._live.update(self._render())
            self._live.stop()
            self._live = None

    def _render(self) -> RenderableType:
        meta = self._meta
        if meta is None:
            return Text("Review Action starting…")
        height = self.console.size.height
        width = self.console.size.width
        return render_dashboard(
            meta,
            self._stats,
            self._completed,
            self._total,
            self._elapsed_s,
            self._results,
            current_phase=self._current_phase,
            current_finding=self._current_finding,
            console_height=height,
            console_width=width,
        )
