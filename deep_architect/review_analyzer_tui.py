"""Full-screen Textual TUI for review-analyzer interactive runs.

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
from typing import Any, Literal

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, ProgressBar, RichLog, Static

from deep_architect.feedback_report import NON_FINDING_FILES
from deep_architect.review_analyzer import (
    ProgressEvent,
    RunMeta,
    SummaryOutputs,
    Verdict,
    _finding_lines,
    _finding_path,
    _severity_display,
    _severity_stats_key,
    _sort_severity_counts,
    request_shutdown,
)

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a terminal)
# ---------------------------------------------------------------------------

_VERDICT_STYLE: dict[Verdict, str] = {
    Verdict.VALID: "bold green",
    Verdict.REJECTED: "bold red",
    Verdict.BACKLOG: "bold yellow",
    Verdict.TIMEOUT: "bold magenta",
}

_VERDICT_ICON: dict[Verdict, str] = {
    Verdict.VALID: "✓",
    Verdict.REJECTED: "✗",
    Verdict.BACKLOG: "◷",
    Verdict.TIMEOUT: "⌛",
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


def _location(finding: dict[str, Any]) -> str:
    path = _finding_path(finding)
    lines = _finding_lines(finding)
    if lines:
        bare = lines.strip("`")
        return f"{path}{bare}"
    return path


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


def format_header(meta: RunMeta) -> str:
    """Markup for the header panel."""
    lines = [
        f"[bold]OCR:[/bold] {meta.ocr_file}",
    ]
    if meta.ocr_status:
        lines[0] += f"   [bold]status:[/bold] {meta.ocr_status}"
    lines.append(
        f"[bold]Model:[/bold] {meta.model}   "
        f"[bold]concurrency:[/bold] {meta.concurrency}"
    )
    if meta.output_dir is not None and not meta.summary_only:
        lines[-1] += f"   [bold]output:[/bold] {meta.output_dir}"
    findings_line = f"[bold]Findings:[/bold] {meta.total_findings}"
    if meta.raw_findings != meta.total_findings:
        findings_line += f" [dim](of {meta.raw_findings} raw)[/dim]"
    lines.append(findings_line)
    ocr_bits = _ocr_summary_bits(meta.ocr_summary)
    if ocr_bits:
        lines.append(f"[dim]{ocr_bits}[/dim]")
    return "\n".join(lines)


def format_summary(
    counts: dict[str, int],
    total: int,
    completed: int,
    severity_counts: dict[str, int] | None = None,
) -> str:
    """Markup for the summary stats strip (verdicts + optional severity)."""
    valid = counts.get(Verdict.VALID.value, 0)
    rejected = counts.get(Verdict.REJECTED.value, 0)
    backlog = counts.get(Verdict.BACKLOG.value, 0)
    timeout = counts.get(Verdict.TIMEOUT.value, 0)
    pending = max(total - completed, 0)
    lines = [
        (
            f"[bold green]✓ VALID {valid}[/bold green]    "
            f"[bold red]✗ REJECTED {rejected}[/bold red]    "
            f"[bold yellow]◷ BACKLOG {backlog}[/bold yellow]    "
            f"[bold magenta]⌛ TIMEOUT {timeout}[/bold magenta]    "
            f"[dim]pending {pending}[/dim]"
        )
    ]
    if severity_counts:
        ordered = _sort_severity_counts(severity_counts)
        if ordered:
            bits = [f"{label} {n}" for label, n in ordered]
            lines.append("[dim]severity:[/dim] " + "  ·  ".join(bits))
    return "\n".join(lines)


def format_progress_label(
    completed: int,
    total: int,
    elapsed_s: float,
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

    return (
        f"[bold]Analyzing {completed}/{total}[/bold]  "
        f"[dim]({fraction * 100:.0f}%)[/dim]\n"
        f"[dim]Elapsed[/dim] {format_duration(elapsed_s)}  ·  "
        f"[dim]ETA[/dim] {eta_text}  ·  "
        f"{rate_text}"
        + (" findings/s" if rate_text != "—" else "")
    )


def format_done_body(
    summary_text: str,
    *,
    summary_path: Path | None = None,
    index_path: Path | None = None,
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
    path_lines: list[str] = []
    if summary_path is not None:
        path_lines.append(f"Summary written to {summary_path}")
    if index_path is not None:
        path_lines.append(f"Index written to {index_path}")
    if path_lines:
        if parts:
            parts.append("")
        parts.extend(path_lines)
    if parts:
        parts.append("")
    if browse_available:
        parts.append("q quit · b browse findings")
    else:
        parts.append("q quit")
    return "\n".join(parts) + "\n"


def is_browse_available(
    *,
    summary_only: bool,
    output_dir: Path | None,
    summary_path: Path | None,
) -> bool:
    """True when review-feedback-browse can open the run's output dir."""
    if summary_only or output_dir is None:
        return False
    if not output_dir.is_dir():
        return False
    if summary_path is not None and summary_path.is_file():
        return True
    try:
        return any(
            p.suffix == ".md" and p.name not in NON_FINDING_FILES
            for p in output_dir.iterdir()
        )
    except OSError:
        return False


def format_result_line(event: ProgressEvent) -> str:
    """One results-pane line for a finished finding.

    Columns: icon+verdict, severity, retry count, duration, location, preview.
    """
    icon = _VERDICT_ICON.get(event.analysis.verdict, "?")
    verdict = event.analysis.verdict.value.upper()
    sev = _severity_display(event.finding)
    sev_col = f"{sev:<8}" if sev != "—" else f"{'—':<8}"
    retries = max(0, int(event.analysis.retry_count))
    secs = max(0, int(round(event.analysis.duration_s)))
    loc = truncate_message(_location(event.finding), max_len=44)
    preview = truncate_message(event.analysis.analysis, max_len=72)
    return (
        f"{icon} {verdict:<9} {sev_col} {retries:>2}r {secs:>4}s  {loc}  {preview}"
    )


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

PipelineFn = Callable[[Callable[[ProgressEvent], None]], dict[str, int]]
FinalizeFn = Callable[[], SummaryOutputs]
TuiAction = Literal["quit", "browse"]


@dataclass(frozen=True)
class AnalyzerTuiResult:
    """Value returned when the analyzer TUI exits."""

    counts: dict[str, int]
    action: TuiAction
    summary_path: Path | None = None


class DoneScreen(Screen[None]):
    """Post-run summary: the same markdown as SUMMARY.md, then quit or browse."""

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
        if isinstance(app, ReviewAnalyzerApp):
            app.exit_with_action("quit")

    def action_launch_browse(self) -> None:
        app = self.app
        if isinstance(app, ReviewAnalyzerApp):
            app.launch_browse()


class ReviewAnalyzerApp(App[AnalyzerTuiResult]):
    """Full-screen review-analyzer dashboard with a dedicated Log pane."""

    TITLE = "review-analyzer"
    CSS = """
    Screen {
        layout: vertical;
    }
    #header-panel {
        height: auto;
        max-height: 6;
        border: solid cyan;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    #progress-panel {
        height: auto;
        border: solid blue;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    #progress-label {
        height: auto;
    }
    #progress-bar {
        height: 1;
        margin: 1 0;
    }
    #summary-panel {
        height: auto;
        max-height: 5;
        border: solid magenta;
        padding: 0 1;
        margin: 0 0 1 0;
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
        margin: 1 0 0 0;
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

        self._counts: dict[str, int] = {
            Verdict.VALID.value: 0,
            Verdict.REJECTED.value: 0,
            Verdict.BACKLOG.value: 0,
            Verdict.TIMEOUT.value: 0,
            "interrupted": 0,
            "total_findings": meta.total_findings,
        }
        self._severity_counts: dict[str, int] = {}
        self._completed = 0
        self._total = meta.total_findings
        self._elapsed_s = 0.0
        self._result_count = 0
        self._stop_requested = False
        self._pipeline_finished = False
        self._browse_available = False
        self._outputs: SummaryOutputs | None = None
        self._capture: LoggingCapture | None = None

    def compose(self) -> ComposeResult:
        yield Static(format_header(self._meta), id="header-panel")
        with Vertical(id="progress-panel"):
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
            format_summary(
                self._counts,
                self._total,
                self._completed,
                self._severity_counts,
            ),
            id="summary-panel",
        )
        with Vertical(id="results-panel"):
            yield Static(
                "Results (newest last)  ·  sev · r=retries  secs=duration",
                id="results-title",
            )
            yield RichLog(id="results-log", highlight=False, markup=False, max_lines=500)
        with Vertical(id="log-panel"):
            yield Static("Log (opencode / harness output)", id="log-title")
            yield RichLog(id="activity-log", highlight=False, markup=False, max_lines=2000)
        yield Static(
            "Running…  q = graceful stop after in-flight analyses finish",
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
                "[yellow]Stop already requested — finishing in-flight analyses…[/yellow]"
            )
            return
        self._stop_requested = True
        request_shutdown()
        self.query_one("#status-line", Static).update(
            "[yellow]Stop requested — finishing in-flight analyses before exit…[/yellow]"
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
            AnalyzerTuiResult(
                counts=self._final_counts(),
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

    def _apply_result(self, event: ProgressEvent) -> None:
        self._completed = event.completed
        self._total = event.total
        self._elapsed_s = event.elapsed_s
        verdict_key = event.analysis.verdict.value
        self._counts[verdict_key] = int(self._counts.get(verdict_key, 0)) + 1
        sev_key = _severity_stats_key(event.finding)
        self._severity_counts[sev_key] = int(self._severity_counts.get(sev_key, 0)) + 1
        self._result_count += 1
        style = _VERDICT_STYLE.get(event.analysis.verdict, "")
        line = format_result_line(event)
        row: Text | str = Text(line, style=style) if style else line
        results = self.query_one("#results-log", RichLog)
        results.write(row)
        if self._result_count > _MAX_RESULT_ROWS:
            title = (
                f"Results (newest last; older rows trimmed at {_MAX_RESULT_ROWS})"
                "  ·  sev · r=retries  secs=duration"
            )
        else:
            title = "Results (newest last)  ·  sev · r=retries  secs=duration"
        self.query_one("#results-title", Static).update(title)
        self._refresh_progress_widgets()

    def _refresh_progress_widgets(self) -> None:
        self.query_one("#progress-label", Static).update(
            format_progress_label(
                self._completed,
                self._total,
                self._elapsed_s,
            )
        )
        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=max(self._total, 1), progress=self._completed)
        self.query_one("#summary-panel", Static).update(
            format_summary(
                self._counts,
                self._total,
                self._completed,
                self._severity_counts,
            )
        )

    def _final_counts(self) -> dict[str, int]:
        out = {
            Verdict.VALID.value: int(self._counts.get(Verdict.VALID.value, 0)),
            Verdict.REJECTED.value: int(self._counts.get(Verdict.REJECTED.value, 0)),
            Verdict.BACKLOG.value: int(self._counts.get(Verdict.BACKLOG.value, 0)),
            Verdict.TIMEOUT.value: int(self._counts.get(Verdict.TIMEOUT.value, 0)),
            "total_findings": self._total,
            "interrupted": int(bool(self._counts.get("interrupted"))),
        }
        return out

    # --- worker -----------------------------------------------------------

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _run_pipeline(self) -> None:
        try:
            counts = self._pipeline(self._bridge_result)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "review-analyzer pipeline failed: %s", exc
            )
            self.call_from_thread(self._on_pipeline_failed, str(exc))
            return
        self.call_from_thread(self._on_pipeline_done, counts)
        if self._finalize is None:
            self.call_from_thread(self._on_finalize_done, None, None)
            return
        try:
            outputs = self._finalize()
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "review-analyzer finalize failed: %s", exc
            )
            self.call_from_thread(self._on_finalize_done, None, str(exc))
            return
        self.call_from_thread(self._on_finalize_done, outputs, None)

    def _on_pipeline_done(self, counts: dict[str, int]) -> None:
        self._pipeline_finished = True
        for key, value in counts.items():
            if key == "interrupted":
                self._counts["interrupted"] = int(bool(value))
            elif key == "total_findings":
                self._total = int(value)
                self._counts["total_findings"] = int(value)
            elif key in (
                Verdict.VALID.value,
                Verdict.REJECTED.value,
                Verdict.BACKLOG.value,
                Verdict.TIMEOUT.value,
            ):
                self._counts[key] = int(value)
        finished = (
            int(self._counts.get(Verdict.VALID.value, 0))
            + int(self._counts.get(Verdict.REJECTED.value, 0))
            + int(self._counts.get(Verdict.BACKLOG.value, 0))
            + int(self._counts.get(Verdict.TIMEOUT.value, 0))
        )
        if finished:
            self._completed = max(self._completed, finished)
        self._refresh_progress_widgets()
        if self._finalize is not None:
            self.query_one("#status-line", Static).update(
                "[bold]Finalizing — backlog promotion and SUMMARY.md…[/bold]"
            )
        else:
            interrupted = bool(counts.get("interrupted"))
            msg = "Interrupted." if interrupted else "Done."
            self.query_one("#status-line", Static).update(f"[bold]{msg}[/bold]")

    def _on_finalize_done(
        self,
        outputs: SummaryOutputs | None,
        error: str | None,
    ) -> None:
        self._outputs = outputs
        self._browse_available = is_browse_available(
            summary_only=self._meta.summary_only,
            output_dir=self._meta.output_dir,
            summary_path=outputs.summary_path if outputs else None,
        )
        body = format_done_body(
            outputs.text if outputs else "",
            summary_path=outputs.summary_path if outputs else None,
            index_path=outputs.index_path if outputs else None,
            browse_available=self._browse_available,
            error=error,
        )
        interrupted = bool(self._counts.get("interrupted"))
        if error:
            status = f"Finalize failed: {truncate_message(error, 80)}"
        elif interrupted:
            status = "Interrupted."
        else:
            status = "Done."
        try:
            self.query_one("#status-line", Static).update(f"[bold]{status}[/bold]")
        except Exception:
            pass
        self.push_screen(
            DoneScreen(body, browse_available=self._browse_available)
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
        self.push_screen(DoneScreen(body, browse_available=False))


def _empty_tui_counts(total_findings: int) -> dict[str, int]:
    return {
        Verdict.VALID.value: 0,
        Verdict.REJECTED.value: 0,
        Verdict.BACKLOG.value: 0,
        Verdict.TIMEOUT.value: 0,
        "total_findings": total_findings,
        "interrupted": 1,
    }


def run_review_analyzer_tui(
    meta: RunMeta,
    pipeline: PipelineFn,
    *,
    log_level: int = logging.INFO,
    log_file: Path | None = None,
    finalize: FinalizeFn | None = None,
) -> AnalyzerTuiResult:
    """Run the full-screen TUI and return counts plus the user's post-run action.

    *pipeline* is called on a worker thread as ``pipeline(on_result)`` and must
    return a counts dict (valid/rejected/backlog/timeout keys, plus optional
    ``interrupted`` / ``total_findings``).

    *finalize* (optional) runs on the same worker after the pipeline so
    promotion and SUMMARY.md happen before the done screen is shown.
    """
    app = ReviewAnalyzerApp(
        meta,
        pipeline,
        log_level=log_level,
        log_file=log_file,
        finalize=finalize,
    )
    result = app.run()
    if result is None:
        # User force-closed without a done-screen choice.
        return AnalyzerTuiResult(
            counts=_empty_tui_counts(meta.total_findings),
            action="quit",
        )
    return result
