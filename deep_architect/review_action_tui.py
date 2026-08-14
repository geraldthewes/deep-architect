"""Full-screen Textual TUI for review-action interactive runs.

Replaces the previous Rich Live dashboard, which shared the terminal with
logging and scrolled into unreadable debris. This app owns the alternate
screen: progress/results stay stable, and all log/agent output is confined
to a scrollable Log pane.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, ProgressBar, RichLog, Static

from deep_architect.action_report import has_action_results
from deep_architect.review_action_harness import ProgressEvent, RunMeta, request_shutdown

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a terminal)
# ---------------------------------------------------------------------------

_OUTCOME_STYLE: dict[str, str] = {
    "completed": "bold green",
    "dry-run": "bold cyan",
    "error": "bold red",
    "skipped": "bold yellow",
    "restored": "dim",
    "rejected": "bold yellow",
    "interrupted": "bold magenta",
    "in-progress": "bold blue",
}

_OUTCOME_ICON: dict[str, str] = {
    "completed": "✓",
    "dry-run": "✓",
    "error": "✗",
    "skipped": "◷",
    "restored": "↻",
    "rejected": "—",
    "interrupted": "!",
    "in-progress": "…",
}

_LEVEL_STYLE: dict[int, str] = {
    logging.DEBUG: "dim",
    logging.INFO: "",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold white on red",
}

# Soft cap for lines shown in the Log pane (full text may still go to file).
DEFAULT_LOG_MESSAGE_MAX_LEN = 500
_MAX_RESULT_ROWS = 200


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


def truncate_message(text: str, max_len: int = DEFAULT_LOG_MESSAGE_MAX_LEN) -> str:
    """Collapse whitespace and truncate for the on-screen Log pane."""
    text = " ".join(text.replace("\r", "\n").splitlines()).strip()
    if max_len <= 0 or len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"
    return text[: max_len - 1] + "…"


def format_log_line(
    record: logging.LogRecord,
    max_len: int = DEFAULT_LOG_MESSAGE_MAX_LEN,
) -> str:
    """Format a log record as a single line for the TUI Log pane."""
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    short_name = record.name.rsplit(".", 1)[-1]
    body = truncate_message(message, max_len=max_len)
    return f"{record.levelname:<7} {short_name}: {body}"


def format_result_line(event: ProgressEvent) -> str:
    """One results-pane line for a finished finding.

    Columns: icon+outcome, severity, duration, finding id, file, preview.
    """
    icon = _OUTCOME_ICON.get(event.outcome, "?")
    sev = (event.severity or "").strip() or "—"
    sev_col = f"{sev:<8}" if sev != "—" else f"{'—':<8}"
    secs = max(0, int(round(event.duration_s)))
    detail = event.summary.replace("\n", " ").strip()
    if event.commit_sha:
        detail = f"`{event.commit_sha}` {detail}"
    detail = truncate_message(detail, max_len=100)
    file_path = truncate_message(event.file_path, max_len=48)
    return (
        f"{icon} {event.outcome:<12} {sev_col} {secs:>4}s  "
        f"{event.finding_id:<16} {file_path}  {detail}"
    )


def format_header(meta: RunMeta) -> str:
    """Markup for the header panel."""
    lines = [
        f"[bold]Feedback:[/bold] {meta.output_dir}",
        f"[bold]Agent:[/bold] {meta.coding_agent}   "
        f"[bold]findings:[/bold] {meta.total_findings}",
    ]
    flags: list[str] = []
    if meta.dry_run:
        flags.append("dry-run")
    if meta.force:
        flags.append("force")
    if meta.skip_errors:
        flags.append("skip-errors")
    if flags:
        lines.append(f"[bold]Flags:[/bold] {', '.join(flags)}")
    return "\n".join(lines)


def format_summary(stats: dict[str, int], total: int, completed: int) -> str:
    """Markup for the summary stats strip."""
    committed = stats.get("committed", 0)
    skipped = stats.get("skipped", 0)
    errors = stats.get("errors", 0)
    restored = stats.get("restored", 0)
    pending = max(total - completed, 0)
    return (
        f"[bold green]✓ Fixed {committed}[/bold green]    "
        f"[bold yellow]◷ Skipped {skipped}[/bold yellow]    "
        f"[bold red]✗ Errors {errors}[/bold red]    "
        f"[dim]↻ Restored {restored}[/dim]    "
        f"[dim]pending {pending}[/dim]"
    )


def format_progress_label(
    completed: int,
    total: int,
    elapsed_s: float,
    *,
    current_finding: str | None = None,
    current_phase: str | None = None,
) -> str:
    """Markup for the progress text block (bar is separate)."""
    total = max(total, 0)
    completed = min(max(completed, 0), total) if total else 0
    fraction = (completed / total) if total else 0.0

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

    line = (
        f"[bold]Processing {completed}/{total}[/bold]  "
        f"[dim]({fraction * 100:.0f}%)[/dim]  "
        f"[dim]Elapsed[/dim] {format_duration(elapsed_s)}  ·  "
        f"[dim]ETA[/dim] {eta_text}  ·  "
        f"{rate_text}"
        + (" findings/s" if rate_text != "—" else "")
    )
    if current_finding or current_phase:
        bits: list[str] = []
        if current_finding:
            bits.append(f"[bold]{current_finding}[/bold]")
        if current_phase:
            bits.append(f"[bold blue]{current_phase}[/bold blue]")
        line += "  [dim]Current:[/dim] " + "  ·  ".join(bits)
    return line


def format_done_body(
    summary_text: str,
    *,
    summary_path: Path | None = None,
    browse_available: bool = False,
    error: str | None = None,
) -> str:
    """Plain-text body for the post-run done screen."""
    parts: list[str] = []
    if error:
        parts.append(f"Pipeline failed: {error}")
        parts.append("")
    stripped = summary_text.strip()
    if stripped:
        parts.append(stripped)
    if summary_path is not None:
        if parts:
            parts.append("")
        parts.append(f"Summary written to {summary_path}")
    if parts:
        parts.append("")
    if browse_available:
        parts.append("q quit · b browse findings")
    else:
        parts.append("q quit")
    return "\n".join(parts) + "\n"


def is_browse_available(
    *,
    output_dir: Path,
    summary_path: Path | None,
) -> bool:
    """True when review-feedback-browse can open the run's output dir."""
    if summary_path is not None and summary_path.is_file():
        return True
    if not output_dir.is_dir():
        return False
    return has_action_results(output_dir)


# ---------------------------------------------------------------------------
# Logging → Log pane
# ---------------------------------------------------------------------------


class TuiLogHandler(logging.Handler):
    """Logging handler that forwards formatted lines to a callback (UI thread).

    Does not write to stdout/stderr. Long messages are truncated for display;
    pair with a FileHandler if full detail is needed on disk.
    """

    def __init__(
        self,
        emit_fn: Callable[[str, int], None],
        *,
        max_message_len: int = DEFAULT_LOG_MESSAGE_MAX_LEN,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        self._emit_fn = emit_fn
        self._max_message_len = max_message_len

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = format_log_line(record, max_len=self._max_message_len)
            self._emit_fn(line, record.levelno)
        except Exception:
            self.handleError(record)


def _is_console_stream_handler(handler: logging.Handler) -> bool:
    """True for StreamHandlers that write to a console stream (not files)."""
    if isinstance(handler, logging.FileHandler):
        return False
    if not isinstance(handler, logging.StreamHandler):
        return False
    return True


@dataclass
class LoggingCapture:
    """Install/remove TUI log routing and optional file mirror."""

    handler: TuiLogHandler
    removed_handlers: list[logging.Handler] = field(default_factory=list)
    file_handler: logging.FileHandler | None = None

    def install(self, root: logging.Logger | None = None) -> None:
        root = root or logging.getLogger()
        self.removed_handlers = []
        for h in list(root.handlers):
            if _is_console_stream_handler(h):
                root.removeHandler(h)
                self.removed_handlers.append(h)
        root.addHandler(self.handler)
        if self.file_handler is not None:
            root.addHandler(self.file_handler)
        # Ensure records at least reach our handler level.
        if root.level > self.handler.level and self.handler.level != logging.NOTSET:
            root.setLevel(self.handler.level)

    def uninstall(self, root: logging.Logger | None = None) -> None:
        root = root or logging.getLogger()
        root.removeHandler(self.handler)
        if self.file_handler is not None:
            root.removeHandler(self.file_handler)
            self.file_handler.close()
            self.file_handler = None
        for h in self.removed_handlers:
            root.addHandler(h)
        self.removed_handlers = []


# ---------------------------------------------------------------------------
# Textual app
# ---------------------------------------------------------------------------

PipelineFn = Callable[
    [Callable[[ProgressEvent], None], Callable[[ProgressEvent], None]],
    dict[str, int],
]
FinalizeFn = Callable[[dict[str, int]], "ActionSummaryOutputs"]
TuiAction = Literal["quit", "browse"]


@dataclass(frozen=True)
class ActionSummaryOutputs:
    """Result of writing the run summary before the done screen."""

    text: str
    summary_path: Path | None = None


@dataclass(frozen=True)
class ActionTuiResult:
    """Value returned when the action TUI exits."""

    stats: dict[str, int]
    action: TuiAction
    summary_path: Path | None = None


class DoneScreen(Screen[None]):
    """Post-run summary: this run's review-action_summary.md, then quit or browse."""

    BINDINGS = [
        Binding("q", "quit_app", "Quit", priority=True),
        Binding("ctrl+c", "quit_app", "Quit", show=False, priority=True),
        Binding("b", "launch_browse", "Browse"),
    ]

    def __init__(self, body: str, *, browse_available: bool) -> None:
        super().__init__()
        self._body = body
        self._browse_available = browse_available

    def compose(self) -> ComposeResult:
        with Vertical(id="done-body"):
            yield Static("[bold]Review complete[/bold]", id="done-header")
            with VerticalScroll(id="done-scroll"):
                yield Static(self._body, id="done-content", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#done-scroll", VerticalScroll).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "launch_browse" and not self._browse_available:
            return False
        return True

    def action_quit_app(self) -> None:
        app = self.app
        if isinstance(app, ReviewActionApp):
            app.exit_with_action("quit")

    def action_launch_browse(self) -> None:
        app = self.app
        if isinstance(app, ReviewActionApp):
            app.launch_browse()


class ReviewActionApp(App[ActionTuiResult]):
    """Full-screen review-action dashboard with a dedicated Log pane."""

    TITLE = "review-action"
    CSS = """
    Screen {
        layout: vertical;
    }
    #header-panel {
        height: auto;
        border: solid cyan;
        padding: 0 1;
        margin: 0;
    }
    #header-meta {
        height: auto;
    }
    #progress-label {
        height: auto;
    }
    #progress-bar {
        height: 1;
        margin: 0;
    }
    #summary-strip {
        height: auto;
    }
    #results-panel {
        height: 1fr;
        border: solid green;
        padding: 0 1;
    }
    #results-title {
        height: 1;
        text-style: bold;
    }
    #results-log {
        height: 1fr;
    }
    #log-panel {
        height: 12;
        border: solid yellow;
        padding: 0 1;
        margin: 0;
    }
    #log-title {
        height: 1;
        text-style: bold;
    }
    #activity-log {
        height: 1fr;
    }
    #status-line {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    #done-body {
        padding: 1 2;
    }
    #done-header {
        margin-bottom: 1;
    }
    #done-scroll {
        height: 1fr;
        border: solid cyan;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "request_stop", "Stop", priority=True),
        Binding("ctrl+c", "request_stop", "Stop", show=False, priority=True),
        Binding("l", "focus_log", "Log", show=True),
        Binding("r", "focus_results", "Results", show=True),
        Binding("b", "launch_browse", "Browse", show=False),
    ]

    def __init__(
        self,
        meta: RunMeta,
        pipeline: PipelineFn,
        *,
        log_level: int = logging.INFO,
        log_file: Path | None = None,
        finalize: FinalizeFn | None = None,
    ) -> None:
        super().__init__()
        self._meta = meta
        self._pipeline = pipeline
        self._log_level = log_level
        self._log_file = log_file
        self._finalize = finalize

        self._stats: dict[str, int] = {
            "processed": 0,
            "committed": 0,
            "skipped": 0,
            "errors": 0,
            "restored": 0,
            "total_findings": meta.total_findings,
            "interrupted": 0,
        }
        self._completed = 0
        self._total = meta.total_findings
        self._elapsed_s = 0.0
        self._current_phase: str | None = None
        self._current_finding: str | None = None
        self._result_count = 0
        self._stop_requested = False
        self._pipeline_finished = False
        self._browse_available = False
        self._outputs: ActionSummaryOutputs | None = None
        self._capture: LoggingCapture | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="header-panel"):
            yield Static(format_header(self._meta), id="header-meta")
            yield Static(
                format_progress_label(0, self._total, 0.0),
                id="progress-label",
            )
            yield ProgressBar(
                total=max(self._total, 1),
                show_eta=False,
                id="progress-bar",
            )
            yield Static(
                format_summary(self._stats, self._total, self._completed),
                id="summary-strip",
            )
        with Vertical(id="results-panel"):
            yield Static(
                "Results (newest last)  ·  sev · secs=duration",
                id="results-title",
            )
            yield RichLog(id="results-log", highlight=False, markup=False, max_lines=500)
        with Vertical(id="log-panel"):
            yield Static("Log (agent / harness output)", id="log-title")
            yield RichLog(id="activity-log", highlight=False, markup=False, max_lines=2000)
        yield Static(
            "Running…  q = graceful stop after current finding",
            id="status-line",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._install_logging()
        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=max(self._total, 1), progress=0)
        self.query_one("#activity-log", RichLog).write(
            Text("TUI started — logging is confined to this pane.", style="dim")
        )
        self._run_pipeline()

    def on_unmount(self) -> None:
        self._uninstall_logging()

    def action_request_stop(self) -> None:
        if self._pipeline_finished:
            self.exit_with_action("quit")
            return
        if self._stop_requested:
            self.query_one("#status-line", Static).update(
                "[yellow]Stop already requested — waiting for current finding…[/yellow]"
            )
            return
        self._stop_requested = True
        request_shutdown()
        self.query_one("#status-line", Static).update(
            "[yellow]Stop requested — finishing current finding before exit…[/yellow]"
        )
        self.query_one("#activity-log", RichLog).write(
            Text("WARN    tui: graceful stop requested (q / Ctrl-C)", style="yellow")
        )

    def action_focus_log(self) -> None:
        self.query_one("#activity-log", RichLog).focus()

    def action_focus_results(self) -> None:
        self.query_one("#results-log", RichLog).focus()

    def action_launch_browse(self) -> None:
        self.launch_browse()

    def exit_with_action(self, action: TuiAction) -> None:
        """Leave the TUI with the given post-run action."""
        self.exit(
            ActionTuiResult(
                stats=self._final_stats(),
                action=action,
                summary_path=self._outputs.summary_path if self._outputs else None,
            )
        )

    def launch_browse(self) -> None:
        """One-way handoff: exit so main() can start review-feedback-browse."""
        if not self._pipeline_finished:
            return
        if not self._browse_available:
            self.notify("No feedback directory to browse", severity="warning")
            return
        self.exit_with_action("browse")

    # --- logging bridge ---------------------------------------------------

    def _install_logging(self) -> None:
        handler = TuiLogHandler(
            self._on_log_line_from_handler,
            max_message_len=DEFAULT_LOG_MESSAGE_MAX_LEN,
            level=self._log_level,
        )
        file_handler: logging.FileHandler | None = None
        if self._log_file is not None:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(self._log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
            )
        self._capture = LoggingCapture(handler=handler, file_handler=file_handler)
        self._capture.install()

    def _uninstall_logging(self) -> None:
        if self._capture is not None:
            self._capture.uninstall()
            self._capture = None

    def _on_log_line_from_handler(self, line: str, levelno: int) -> None:
        # Handler may run on the worker thread.
        self.call_from_thread(self._append_log, line, levelno)

    def _append_log(self, line: str, levelno: int) -> None:
        # Use Rich Text (not markup strings) so agent JSON with "[" cannot
        # break or inject markup into the Log pane.
        style = _LEVEL_STYLE.get(levelno, "")
        content: Text | str = Text(line, style=style) if style else line
        try:
            self.query_one("#activity-log", RichLog).write(content)
        except Exception:
            # App may already be tearing down.
            pass

    # --- progress bridge --------------------------------------------------

    def _bridge_result(self, event: ProgressEvent) -> None:
        self.call_from_thread(self._apply_result, event)

    def _bridge_phase(self, event: ProgressEvent) -> None:
        self.call_from_thread(self._apply_phase, event)

    def _apply_result(self, event: ProgressEvent) -> None:
        self._completed = event.completed
        self._total = event.total
        self._elapsed_s = event.elapsed_s
        if event.stats:
            self._merge_stats(event.stats)
        self._current_phase = None
        self._current_finding = None
        self._result_count += 1
        style = _OUTCOME_STYLE.get(event.outcome, "")
        line = format_result_line(event)
        row: Text | str = Text(line, style=style) if style else line
        results = self.query_one("#results-log", RichLog)
        results.write(row)
        # Cap visible history roughly (RichLog has max_lines too).
        if self._result_count > _MAX_RESULT_ROWS:
            title = (
                f"Results (newest last; older rows trimmed at {_MAX_RESULT_ROWS})"
                "  ·  sev · secs=duration"
            )
        else:
            title = "Results (newest last)  ·  sev · secs=duration"
        self.query_one("#results-title", Static).update(title)
        self._refresh_progress_widgets()

    def _apply_phase(self, event: ProgressEvent) -> None:
        self._elapsed_s = event.elapsed_s
        self._current_phase = event.phase or event.summary
        self._current_finding = event.finding_id
        if event.stats:
            self._merge_stats(event.stats)
        # completed/total on phase events are the pre-completion counts.
        if event.total:
            self._total = event.total
        self._refresh_progress_widgets()

    def _merge_stats(self, stats: dict[str, int]) -> None:
        for key, value in stats.items():
            if key == "interrupted":
                self._stats["interrupted"] = int(bool(value))
                continue
            if isinstance(value, bool):
                self._stats[key] = int(value)
            else:
                self._stats[key] = int(value)

    def _refresh_progress_widgets(self) -> None:
        self.query_one("#progress-label", Static).update(
            format_progress_label(
                self._completed,
                self._total,
                self._elapsed_s,
                current_finding=self._current_finding,
                current_phase=self._current_phase,
            )
        )
        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=max(self._total, 1), progress=self._completed)
        self.query_one("#summary-strip", Static).update(
            format_summary(self._stats, self._total, self._completed)
        )

    def _final_stats(self) -> dict[str, int]:
        out = dict(self._stats)
        out["total_findings"] = self._total
        out["interrupted"] = int(bool(out.get("interrupted")))
        return out

    # --- worker -----------------------------------------------------------

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _run_pipeline(self) -> None:
        try:
            stats = self._pipeline(self._bridge_result, self._bridge_phase)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "review-action pipeline failed: %s", exc
            )
            self.call_from_thread(self._on_pipeline_failed, str(exc))
            return
        self.call_from_thread(self._on_pipeline_done, stats)
        if self._finalize is None:
            self.call_from_thread(self._on_finalize_done, None, None)
            return
        try:
            outputs = self._finalize(stats)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "review-action finalize failed: %s", exc
            )
            self.call_from_thread(self._on_finalize_done, None, str(exc))
            return
        self.call_from_thread(self._on_finalize_done, outputs, None)

    def _on_pipeline_done(self, stats: dict[str, int]) -> None:
        self._pipeline_finished = True
        self._merge_stats(stats)
        self._completed = max(self._completed, int(stats.get("processed", self._completed)))
        # Prefer authoritative totals from pipeline when present.
        if "total_findings" in stats:
            self._total = int(stats["total_findings"])
        # Recompute completed from counters if pipeline filled them.
        finished = (
            int(stats.get("committed", 0))
            + int(stats.get("skipped", 0))
            + int(stats.get("errors", 0))
            + int(stats.get("restored", 0))
        )
        if finished:
            self._completed = max(self._completed, finished)
        self._current_phase = None
        self._current_finding = None
        self._refresh_progress_widgets()
        if self._finalize is not None:
            self.query_one("#status-line", Static).update(
                "[bold]Finalizing — writing review-action_summary.md…[/bold]"
            )
        else:
            interrupted = bool(stats.get("interrupted"))
            if interrupted:
                msg = "Interrupted."
            elif int(stats.get("errors", 0)) > 0:
                msg = f"Done with {stats.get('errors', 0)} error(s)."
            else:
                msg = "Done."
            self.query_one("#status-line", Static).update(f"[bold]{msg}[/bold]")

    def _on_finalize_done(
        self,
        outputs: ActionSummaryOutputs | None,
        error: str | None,
    ) -> None:
        self._outputs = outputs
        self._browse_available = is_browse_available(
            output_dir=self._meta.output_dir,
            summary_path=outputs.summary_path if outputs else None,
        )
        body = format_done_body(
            outputs.text if outputs else "",
            summary_path=outputs.summary_path if outputs else None,
            browse_available=self._browse_available,
            error=error,
        )
        interrupted = bool(self._stats.get("interrupted"))
        if error:
            status = f"Finalize failed: {truncate_message(error, 80)}"
        elif interrupted:
            status = "Interrupted."
        elif int(self._stats.get("errors", 0)) > 0:
            status = f"Done with {self._stats.get('errors', 0)} error(s)."
        else:
            status = "Done."
        try:
            self.query_one("#status-line", Static).update(f"[bold]{status}[/bold]")
        except Exception:
            pass
        self.push_screen(DoneScreen(body, browse_available=self._browse_available))

    def _on_pipeline_failed(self, error: str) -> None:
        self._pipeline_finished = True
        self._stats["errors"] = int(self._stats.get("errors", 0)) + 1
        self._browse_available = False
        short = truncate_message(error, 120)
        self.query_one("#status-line", Static).update(
            f"[bold red]Pipeline failed: {short}[/bold red]"
        )
        self.query_one("#activity-log", RichLog).write(
            Text(
                f"ERROR   tui: pipeline failed: {truncate_message(error, 400)}",
                style="bold red",
            )
        )
        body = format_done_body("", error=error, browse_available=False)
        self.push_screen(DoneScreen(body, browse_available=False))


def _empty_tui_stats(total_findings: int) -> dict[str, int]:
    return {
        "processed": 0,
        "committed": 0,
        "skipped": 0,
        "errors": 0,
        "restored": 0,
        "total_findings": total_findings,
        "interrupted": 1,
    }


def run_review_action_tui(
    meta: RunMeta,
    pipeline: PipelineFn,
    *,
    log_level: int = logging.INFO,
    log_file: Path | None = None,
    finalize: FinalizeFn | None = None,
) -> ActionTuiResult:
    """Run the full-screen TUI and return stats plus the user's post-run action.

    *pipeline* is called on a worker thread as
    ``pipeline(on_result, on_phase)`` and must return the stats dict from
    ``process_findings``.

    *finalize* (optional) runs on the same worker after the pipeline so
    ``review-action_summary.md`` is written before the done screen is shown.
    """
    app = ReviewActionApp(
        meta,
        pipeline,
        log_level=log_level,
        log_file=log_file,
        finalize=finalize,
    )
    result = app.run()
    if result is None:
        # User force-closed without a done-screen choice.
        return ActionTuiResult(
            stats=_empty_tui_stats(meta.total_findings),
            action="quit",
        )
    return result
