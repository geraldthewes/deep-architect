"""Full-screen Textual TUI for review-driver interactive runs.

Owns the alternate screen: pass/phase progress stays stable, and child
OCR/analyzer/action output is confined to a scrollable Log pane.
"""

from __future__ import annotations

import logging
import re
import time
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

from deep_architect.review_driver import (
    DriverProgress,
    DriverRunMeta,
    ProgressReporter,
    format_duration,
    format_pass_fraction,
    request_force_stop,
    request_interrupt,
)

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a terminal)
# ---------------------------------------------------------------------------

_LEVEL_STYLE: dict[int, str] = {
    logging.DEBUG: "dim",
    logging.INFO: "",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold white on red",
}

_PHASE_LABEL: dict[str, str] = {
    "ocr": "OCR",
    "analyzer": "Analyzer",
    "action": "Action",
}

DEFAULT_LOG_MESSAGE_MAX_LEN = 500
DEFAULT_ERROR_LOG_MESSAGE_MAX_LEN = 240
_MAX_RESULT_ROWS = 200
_CHILD_ERROR_RE = re.compile(
    r"^(Error:|\[ocr\] Subtask error|ocr exited|ocr timed out|"
    r"\[ocr\] failed |\[ocr\] llm error )"
    r"|context deadline exceeded",
    re.IGNORECASE,
)


def classify_child_log_level(line: str) -> int:
    """ERROR for OCR/child failure lines; INFO otherwise."""
    if _CHILD_ERROR_RE.match(line.strip()):
        return logging.ERROR
    return logging.INFO


def child_log_display_max_len(level: int) -> int:
    if level >= logging.ERROR:
        return max(DEFAULT_LOG_MESSAGE_MAX_LEN, DEFAULT_ERROR_LOG_MESSAGE_MAX_LEN)
    return DEFAULT_LOG_MESSAGE_MAX_LEN


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


def format_header(meta: DriverRunMeta, *, pass_index: int = 0) -> str:
    """Markup for the header panel."""
    src_sha = (meta.source_sha or "")[:12]
    tgt_sha = (meta.target_sha or "")[:12]
    sha_bit = ""
    if src_sha or tgt_sha:
        sha_bit = f"   [dim]{src_sha or '—'} → {tgt_sha or '—'}[/dim]"
    resume = "   [bold]resume[/bold]" if meta.resume else ""
    return (
        f"[bold]{meta.source}[/bold] → [bold]{meta.target}[/bold]{sha_bit}\n"
        f"[bold]pass:[/bold] {format_pass_fraction(pass_index, meta.max_passes)}   "
        f"[bold]K:[/bold] {meta.k}   "
        f"[bold]output:[/bold] {meta.output_dir}{resume}"
    )


def format_summary(
    *,
    novelty: int | None,
    zeros: int,
    k: int,
    valid_total: int,
    valid_high: int,
    valid_medium: int,
    valid_low: int,
    committed: int,
    errors: int,
) -> str:
    """Markup for the summary stats strip."""
    nov = "—" if novelty is None else str(novelty)
    return (
        f"[bold]novelty {nov}[/bold]    "
        f"zeros {zeros}/{k}    "
        f"[bold green]VALID {valid_total}[/bold green] "
        f"(H{valid_high} M{valid_medium} L{valid_low})    "
        f"[bold green]committed {committed}[/bold green]    "
        f"[bold red]errors {errors}[/bold red]"
    )


def _progress_bar_total(max_passes: int, completed: int) -> int:
    """ProgressBar total. Unlimited (max_passes=0) stays one step ahead of done."""
    if max_passes > 0:
        return max(max_passes, 1)
    return max(completed + 1, 1)


def format_progress_label(
    pass_index: int,
    max_passes: int,
    phase: str | None,
    elapsed_s: float,
) -> str:
    """Markup for the progress text block (bar is separate)."""
    if phase in _PHASE_LABEL:
        phase_label = _PHASE_LABEL[phase]
    elif phase:
        phase_label = phase
    else:
        phase_label = "—"
    return (
        f"[bold]Pass {format_pass_fraction(pass_index, max_passes)}[/bold]  "
        f"[bold blue]{phase_label}[/bold blue]  "
        f"[dim]Elapsed[/dim] {format_duration(elapsed_s)}"
    )


def format_done_header(*, failed: bool) -> str:
    """Markup for the DoneScreen title."""
    if failed:
        return "[bold red]Review failed[/bold red]"
    return "[bold]Review complete[/bold]"


def format_done_body(
    report_text: str,
    *,
    report_path: Path | None = None,
    browse_available: bool = False,
    error: str | None = None,
) -> str:
    """Plain-text body for the post-run done screen."""
    parts: list[str] = []
    if error:
        parts.append(f"Pipeline failed: {error}")
        parts.append("")
    stripped = report_text.strip()
    if stripped:
        parts.append(stripped)
    if report_path is not None:
        if parts:
            parts.append("")
        parts.append(f"Report written to {report_path}")
    if parts:
        parts.append("")
    if browse_available:
        parts.append("q quit · b browse findings")
    else:
        parts.append("q quit")
    return "\n".join(parts) + "\n"


def last_feedback_dir(progress: DriverProgress | None) -> Path | None:
    """Return the newest on-disk feedback directory from *progress*."""
    if progress is None:
        return None
    for record in reversed(progress.passes):
        path = Path(record.feedback_dir)
        if path.is_dir():
            return path
    return None


def is_browse_available(feedback_dir: Path | None) -> bool:
    """True when review-feedback-browse can open a pass feedback dir."""
    return feedback_dir is not None and feedback_dir.is_dir()


def infra_error_count(progress: DriverProgress) -> int:
    """Action errors plus one per failed or partial OCR pass."""
    total = 0
    for record in progress.passes:
        total += record.action_errors
        ocr_status = (record.ocr_status or "").lower()
        if record.status == "failed" or ocr_status in {"failed", "partial"}:
            total += 1
    return total


def _request_nested_shutdown() -> None:
    """Ask in-process analyzer/action to stop after current in-flight work."""
    from deep_architect.review_action_harness import (  # noqa: PLC0415
        request_shutdown as action_shutdown,
    )
    from deep_architect.review_analyzer import (  # noqa: PLC0415
        request_shutdown as analyzer_shutdown,
    )

    analyzer_shutdown()
    action_shutdown()


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

PipelineFn = Callable[[ProgressReporter], DriverProgress]
FinalizeFn = Callable[[DriverProgress], Path]
TuiAction = Literal["quit", "browse"]
ChildLogSink = Callable[[Callable[[str], None] | None], None]


@dataclass(frozen=True)
class DriverTuiResult:
    """Value returned when the driver TUI exits."""

    progress: DriverProgress
    action: TuiAction
    report_path: Path | None = None


class DoneScreen(Screen[None]):
    """Post-run summary: REPORT.md, then quit or browse."""

    BINDINGS = [
        Binding("q", "quit_app", "Quit", priority=True),
        Binding("ctrl+c", "quit_app", "Quit", show=False, priority=True),
        Binding("b", "launch_browse", "Browse"),
    ]

    def __init__(
        self, body: str, *, browse_available: bool, failed: bool = False
    ) -> None:
        super().__init__()
        self._body = body
        self._browse_available = browse_available
        self._failed = failed

    def compose(self) -> ComposeResult:
        with Vertical(id="done-body"):
            yield Static(format_done_header(failed=self._failed), id="done-header")
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
        if isinstance(app, ReviewDriverApp):
            app.exit_with_action("quit")

    def action_launch_browse(self) -> None:
        app = self.app
        if isinstance(app, ReviewDriverApp):
            app.launch_browse()


class _TuiReporter:
    """ProgressReporter that hops driver events onto the Textual UI thread."""

    def __init__(self, app: ReviewDriverApp) -> None:
        self._app = app

    def start(self, meta: DriverRunMeta) -> None:
        self._app.call_from_thread(self._app.apply_start, meta)

    def phase_start(self, pass_index: int, max_passes: int, phase: str) -> None:
        self._app.call_from_thread(
            self._app.apply_phase_start, pass_index, max_passes, phase
        )

    def phase_done(self, line: str) -> None:
        self._app.call_from_thread(self._app.apply_phase_done, line)

    def pass_done(
        self, rollup: str, trend: str | None, progress: DriverProgress
    ) -> None:
        self._app.call_from_thread(self._app.apply_pass_done, rollup, trend, progress)

    def finish(self, progress: DriverProgress) -> None:
        self._app.call_from_thread(self._app.apply_finish, progress)


class ReviewDriverApp(App[DriverTuiResult]):
    """Full-screen review-driver dashboard with a dedicated Log pane."""

    TITLE = "review-driver"
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
        meta: DriverRunMeta,
        pipeline: PipelineFn,
        *,
        log_level: int = logging.INFO,
        log_file: Path | None = None,
        finalize: FinalizeFn | None = None,
        attach_child_logs: ChildLogSink | None = None,
    ) -> None:
        super().__init__()
        self._meta = meta
        self._pipeline = pipeline
        self._log_level = log_level
        self._log_file = log_file
        self._finalize = finalize
        self._attach_child_logs = attach_child_logs

        self._pass_index = 0
        self._max_passes = meta.max_passes
        self._phase: str | None = None
        self._started_at = 0.0
        self._elapsed_s = 0.0
        self._novelty: int | None = None
        self._zeros = 0
        self._valid_total = 0
        self._valid_high = 0
        self._valid_medium = 0
        self._valid_low = 0
        self._committed = 0
        self._errors = 0
        self._result_count = 0
        self._stop_requested = False
        self._force_stop_requested = False
        self._pipeline_finished = False
        self._browse_available = False
        self._progress: DriverProgress | None = None
        self._report_path: Path | None = None
        self._capture: LoggingCapture | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="header-panel"):
            yield Static(format_header(self._meta), id="header-meta")
            yield Static(
                format_progress_label(0, self._max_passes, None, 0.0),
                id="progress-label",
            )
            yield ProgressBar(
                total=_progress_bar_total(self._max_passes, 0),
                show_eta=False,
                id="progress-bar",
            )
            yield Static(
                format_summary(
                    novelty=None,
                    zeros=0,
                    k=self._meta.k,
                    valid_total=0,
                    valid_high=0,
                    valid_medium=0,
                    valid_low=0,
                    committed=0,
                    errors=0,
                ),
                id="summary-strip",
            )
        with Vertical(id="results-panel"):
            yield Static(
                "Results (newest last)  ·  phase summaries",
                id="results-title",
            )
            yield RichLog(id="results-log", highlight=False, markup=False, max_lines=500)
        with Vertical(id="log-panel"):
            yield Static("Log (ocr / analyzer / action)", id="log-title")
            yield RichLog(
                id="activity-log", highlight=False, markup=False, max_lines=2000
            )
        yield Static(
            "Running…  q = stop after the current step; second q kills it",
            id="status-line",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._started_at = time.monotonic()
        self._install_logging()
        if self._attach_child_logs is not None:
            self._attach_child_logs(self._on_child_log)
        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=_progress_bar_total(self._max_passes, 0), progress=0)
        self.query_one("#activity-log", RichLog).write(
            Text("TUI started — logging is confined to this pane.", style="dim")
        )
        self.set_interval(1.0, self._tick_elapsed)
        self._run_pipeline()

    def on_unmount(self) -> None:
        if self._attach_child_logs is not None:
            self._attach_child_logs(None)
        self._uninstall_logging()

    def action_request_stop(self) -> None:
        if self._pipeline_finished:
            self.exit_with_action("quit")
            return
        if self._stop_requested:
            if self._force_stop_requested:
                self.query_one("#status-line", Static).update(
                    "[yellow]Force stop already requested — "
                    "waiting for the process to exit…[/yellow]"
                )
                return
            self._force_stop_requested = True
            request_force_stop()
            self.query_one("#status-line", Static).update(
                "[red]Force stop — killing current OCR process…[/red]"
            )
            self.query_one("#activity-log", RichLog).write(
                Text(
                    "WARN    tui: force stop requested (second q / Ctrl-C)",
                    style="yellow",
                )
            )
            return
        self._stop_requested = True
        request_interrupt()
        _request_nested_shutdown()
        self.query_one("#status-line", Static).update(
            "[yellow]Stop requested — finishing current step. "
            "Press q again to kill it.[/yellow]"
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
            DriverTuiResult(
                progress=self._progress
                if self._progress is not None
                else _empty_progress(self._meta),
                action=action,
                report_path=self._report_path,
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
        self.call_from_thread(self._append_log, line, levelno)

    def _on_child_log(self, text: str) -> None:
        for raw in text.splitlines():
            level = classify_child_log_level(raw)
            line = truncate_message(raw, max_len=child_log_display_max_len(level))
            if line:
                self.call_from_thread(self._append_log, line, level)
                if level >= logging.ERROR:
                    self.call_from_thread(self._show_live_error, line)

    def _append_log(self, line: str, levelno: int) -> None:
        style = _LEVEL_STYLE.get(levelno, "")
        content: Text | str = Text(line, style=style) if style else line
        try:
            self.query_one("#activity-log", RichLog).write(content)
        except Exception:
            pass

    def _show_live_error(self, line: str) -> None:
        if self._pipeline_finished or self._stop_requested:
            return
        try:
            self.query_one("#status-line", Static).update(
                f"[bold red]{truncate_message(line, 120)}[/bold red]"
            )
        except Exception:
            pass

    def _tick_elapsed(self) -> None:
        if self._pipeline_finished or self._started_at <= 0:
            return
        self._elapsed_s = time.monotonic() - self._started_at
        self._refresh_progress_widgets()

    # --- progress bridge --------------------------------------------------

    def apply_start(self, meta: DriverRunMeta) -> None:
        self._meta = meta
        self._max_passes = meta.max_passes
        self._refresh_progress_widgets()

    def apply_phase_start(
        self, pass_index: int, max_passes: int, phase: str
    ) -> None:
        self._pass_index = pass_index
        self._max_passes = max_passes
        self._phase = phase
        self._refresh_progress_widgets()
        if not self._stop_requested:
            label = _PHASE_LABEL.get(phase, phase)
            self.query_one("#status-line", Static).update(
                f"Running {label}…  q = stop after this step; second q kills it"
            )

    def apply_phase_done(self, line: str) -> None:
        self._write_result(line)
        self._refresh_progress_widgets()

    def apply_pass_done(
        self, rollup: str, trend: str | None, progress: DriverProgress
    ) -> None:
        self._phase = None
        self._progress = progress
        self._write_result("")
        self._write_result(rollup)
        if trend is not None:
            self._write_result(trend)
        self._sync_summary_from_progress(progress)
        self._refresh_progress_widgets()

    def apply_finish(self, progress: DriverProgress) -> None:
        self._progress = progress
        self._sync_summary_from_progress(progress)
        self._pass_index = progress.current_pass
        self._phase = None
        self._refresh_progress_widgets()

    def _write_result(self, line: str) -> None:
        results = self.query_one("#results-log", RichLog)
        if line.startswith("OCR      FAILED"):
            results.write(Text(line, style="bold red"))
        elif line.startswith("OCR      PARTIAL"):
            results.write(Text(line, style="yellow"))
        else:
            results.write(line)
        if line:
            self._result_count += 1
        if self._result_count > _MAX_RESULT_ROWS:
            self.query_one("#results-title", Static).update(
                f"Results (newest last; older rows trimmed at {_MAX_RESULT_ROWS})"
                "  ·  phase summaries"
            )

    def _sync_summary_from_progress(self, progress: DriverProgress) -> None:
        self._zeros = progress.consecutive_zero_novelty
        if not progress.passes:
            return
        latest = progress.passes[-1]
        self._novelty = latest.novelty
        self._valid_total = latest.valid_total
        self._valid_high = latest.valid_by_severity.get("high", 0)
        self._valid_medium = latest.valid_by_severity.get("medium", 0)
        self._valid_low = latest.valid_by_severity.get("low", 0)
        self._committed = latest.action_committed
        self._errors = infra_error_count(progress)

    def _refresh_progress_widgets(self) -> None:
        shown_pass = self._pass_index
        self.query_one("#header-meta", Static).update(
            format_header(self._meta, pass_index=shown_pass)
        )
        self.query_one("#progress-label", Static).update(
            format_progress_label(
                shown_pass,
                self._max_passes,
                self._phase,
                self._elapsed_s,
            )
        )
        bar = self.query_one("#progress-bar", ProgressBar)
        completed = self._progress.current_pass if self._progress is not None else 0
        if self._progress is None and self._phase is None:
            completed = 0
        elif self._progress is None:
            completed = max(shown_pass - 1, 0)
        bar.update(
            total=_progress_bar_total(self._max_passes, completed),
            progress=completed,
        )
        self.query_one("#summary-strip", Static).update(
            format_summary(
                novelty=self._novelty,
                zeros=self._zeros,
                k=self._meta.k,
                valid_total=self._valid_total,
                valid_high=self._valid_high,
                valid_medium=self._valid_medium,
                valid_low=self._valid_low,
                committed=self._committed,
                errors=self._errors,
            )
        )

    # --- worker -----------------------------------------------------------

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _run_pipeline(self) -> None:
        reporter = _TuiReporter(self)
        try:
            progress = self._pipeline(reporter)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "review-driver pipeline failed: %s", exc
            )
            self.call_from_thread(self._on_pipeline_failed, str(exc))
            return
        self.call_from_thread(self._on_pipeline_done, progress)
        if self._finalize is None:
            self.call_from_thread(self._on_finalize_done, None, None)
            return
        try:
            report_path = self._finalize(progress)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "review-driver finalize failed: %s", exc
            )
            self.call_from_thread(self._on_finalize_done, None, str(exc))
            return
        self.call_from_thread(self._on_finalize_done, report_path, None)

    def _on_pipeline_done(self, progress: DriverProgress) -> None:
        self._pipeline_finished = True
        self._progress = progress
        self._sync_summary_from_progress(progress)
        self._pass_index = progress.current_pass
        self._phase = None
        self._refresh_progress_widgets()
        if self._finalize is not None:
            self.query_one("#status-line", Static).update(
                "[bold]Finalizing — writing REPORT.md…[/bold]"
            )
        else:
            self.query_one("#status-line", Static).update(
                f"[bold]{self._done_status(progress, None)}[/bold]"
            )

    def _on_finalize_done(self, report_path: Path | None, error: str | None) -> None:
        self._report_path = report_path
        feedback = last_feedback_dir(self._progress)
        self._browse_available = is_browse_available(feedback)
        report_text = ""
        if report_path is not None:
            try:
                report_text = report_path.read_text(encoding="utf-8")
            except OSError:
                report_text = ""
        display_error = error
        if display_error is None and self._progress is not None:
            if self._progress.status == "failed" and self._progress.stop_detail:
                display_error = self._progress.stop_detail
        body = format_done_body(
            report_text,
            report_path=report_path,
            browse_available=self._browse_available,
            error=display_error,
        )
        status = self._done_status(self._progress, error)
        failed = display_error is not None or (
            self._progress is not None and self._progress.status == "failed"
        )
        try:
            markup = f"[bold red]{status}[/bold red]" if failed else f"[bold]{status}[/bold]"
            self.query_one("#status-line", Static).update(markup)
        except Exception:
            pass
        self.push_screen(
            DoneScreen(
                body,
                browse_available=self._browse_available,
                failed=failed,
            )
        )

    def _on_pipeline_failed(self, error: str) -> None:
        self._pipeline_finished = True
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
        self.push_screen(DoneScreen(body, browse_available=False, failed=True))

    def _done_status(
        self, progress: DriverProgress | None, error: str | None
    ) -> str:
        if error and (progress is None or progress.status != "failed"):
            return f"Finalize failed: {truncate_message(error, 80)}"
        if self._stop_requested:
            return "Interrupted."
        if progress is not None and progress.status == "failed":
            detail = progress.stop_detail or error
            if detail:
                return f"Stopped: failed — {truncate_message(detail, 120)}"
            return "Stopped: failed."
        if progress is None:
            return "Done."
        if progress.status == "converged":
            return f"Converged (K={progress.k})."
        if progress.status == "max_passes":
            return "Stopped: max-passes with novelty remaining."
        return f"Stopped: {progress.status}."


def _empty_progress(meta: DriverRunMeta) -> DriverProgress:
    return DriverProgress(
        status="failed",
        source=meta.source,
        target=meta.target,
        source_sha=meta.source_sha,
        target_sha=meta.target_sha,
        max_passes=meta.max_passes,
        k=meta.k,
        output_dir=str(meta.output_dir),
    )


def run_review_driver_tui(
    meta: DriverRunMeta,
    pipeline: PipelineFn,
    *,
    log_level: int = logging.INFO,
    log_file: Path | None = None,
    finalize: FinalizeFn | None = None,
    attach_child_logs: ChildLogSink | None = None,
) -> DriverTuiResult:
    """Run the full-screen TUI and return progress plus the user's post-run action.

    *pipeline* is called on a worker thread as ``pipeline(reporter)`` and must
    return a :class:`DriverProgress`.

    *finalize* (optional) runs on the same worker after the pipeline so
    ``REPORT.md`` is written before the done screen is shown.

    *attach_child_logs* (optional) is ``fn(sink)`` called on mount with a
    callback that accepts child log text, and again on unmount with ``None``.
    """
    app = ReviewDriverApp(
        meta,
        pipeline,
        log_level=log_level,
        log_file=log_file,
        finalize=finalize,
        attach_child_logs=attach_child_logs,
    )
    result = app.run()
    if result is None:
        return DriverTuiResult(
            progress=_empty_progress(meta),
            action="quit",
        )
    return result
