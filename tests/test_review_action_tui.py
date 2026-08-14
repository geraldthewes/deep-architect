"""Unit tests for deep_architect.review_action_tui."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from textual.widgets import Static

from deep_architect.review_action_harness import ProgressEvent, RunMeta
from deep_architect.review_action_tui import (
    LoggingCapture,
    ReviewActionApp,
    TuiLogHandler,
    format_duration,
    format_header,
    format_log_line,
    format_progress_label,
    format_result_line,
    format_summary,
    truncate_message,
)


class TestFormatDuration:

    def test_seconds(self) -> None:
        assert format_duration(5) == "5s"

    def test_minutes(self) -> None:
        assert format_duration(65) == "1m05s"

    def test_hours(self) -> None:
        assert format_duration(3661) == "1h01m01s"


class TestTruncateMessage:

    def test_short_unchanged(self) -> None:
        assert truncate_message("hello", max_len=10) == "hello"

    def test_collapses_newlines(self) -> None:
        assert truncate_message("a\nb\nc", max_len=20) == "a b c"

    def test_truncates(self) -> None:
        assert truncate_message("abcdefghij", max_len=5) == "abcd…"


class TestFormatLogLine:

    def test_includes_level_and_short_name(self) -> None:
        record = logging.LogRecord(
            name="deep_architect.coding_agents.opencode",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed to apply fix for %s: returncode=%d",
            args=("foo.py", 0),
            exc_info=None,
        )
        line = format_log_line(record, max_len=200)
        assert line.startswith("ERROR")
        assert "opencode:" in line
        assert "foo.py" in line
        assert "returncode=0" in line

    def test_truncates_long_body(self) -> None:
        record = logging.LogRecord(
            name="pkg",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="x" * 1000,
            args=(),
            exc_info=None,
        )
        line = format_log_line(record, max_len=40)
        assert len(line) < 80
        assert line.endswith("…")


class TestFormatHelpers:

    def _meta(self) -> RunMeta:
        return RunMeta(
            output_dir=Path("feedback"),
            provider="opencode",
            model="sonnet",
            dry_run=False,
            force=True,
            skip_errors=False,
            total_findings=2,
            coding_agent="opencode (sonnet)",
        )

    def test_header_includes_agent_and_flags(self) -> None:
        text = format_header(self._meta())
        assert "feedback" in text
        assert "opencode (sonnet)" in text
        assert "force" in text
        assert "2" in text

    def test_summary_counts(self) -> None:
        text = format_summary(
            {"committed": 1, "skipped": 2, "errors": 3, "restored": 0},
            total=10,
            completed=6,
        )
        assert "Fixed 1" in text
        assert "Skipped 2" in text
        assert "Errors 3" in text
        assert "pending 4" in text

    def test_progress_label_phase(self) -> None:
        text = format_progress_label(
            1,
            4,
            12.0,
            current_finding="abc-0",
            current_phase="applying",
        )
        assert "1/4" in text
        assert "abc-0" in text
        assert "applying" in text
        assert "Elapsed" in text
        assert "ETA" in text
        assert "\n" not in text

    def test_result_line(self) -> None:
        event = ProgressEvent(
            completed=1,
            total=2,
            finding_id="abc12345-0",
            file_path="src/foo.py",
            outcome="error",
            summary="Fix failed after 6 attempts",
            commit_sha=None,
            elapsed_s=5.0,
            duration_s=5.0,
        )
        line = format_result_line(event)
        assert "error" in line
        assert "abc12345-0" in line
        assert "src/foo.py" in line
        assert "Fix failed" in line
        assert "—" in line
        assert "   5s" in line

    def test_result_line_includes_severity(self) -> None:
        event = ProgressEvent(
            completed=1,
            total=1,
            finding_id="abc12345-0",
            file_path="src/foo.py",
            outcome="completed",
            summary="Fix applied",
            commit_sha="deadbeef",
            elapsed_s=3.0,
            severity="high",
            duration_s=3.0,
        )
        line = format_result_line(event)
        assert "completed" in line
        assert "high" in line
        assert "src/foo.py" in line

    def test_result_line_with_duration(self) -> None:
        event = ProgressEvent(
            completed=2,
            total=2,
            finding_id="abc12345-1",
            file_path="src/bar.py",
            outcome="completed",
            summary="Fix applied",
            commit_sha=None,
            elapsed_s=400.0,
            severity="medium",
            duration_s=312.4,
        )
        line = format_result_line(event)
        assert "completed" in line
        assert "medium" in line
        assert " 312s" in line
        assert "src/bar.py" in line


class TestTuiLogHandler:

    def test_forwards_truncated_line(self) -> None:
        seen: list[tuple[str, int]] = []
        handler = TuiLogHandler(lambda line, level: seen.append((line, level)), max_message_len=50)
        handler.setLevel(logging.DEBUG)
        record = logging.LogRecord(
            name="deep_architect.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="boom " + ("y" * 200),
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        assert len(seen) == 1
        line, level = seen[0]
        assert level == logging.ERROR
        assert "ERROR" in line
        assert line.endswith("…")
        assert "y" * 200 not in line


class TestLoggingCapture:

    def test_strips_console_handlers_and_restores(self) -> None:
        root = logging.getLogger("test_tui_capture_root")
        root.handlers.clear()
        root.setLevel(logging.DEBUG)

        stream = logging.StreamHandler()
        stream.setLevel(logging.INFO)
        root.addHandler(stream)

        seen: list[str] = []
        tui_handler = TuiLogHandler(lambda line, _level: seen.append(line), level=logging.INFO)
        capture = LoggingCapture(handler=tui_handler)
        # Operate on this logger as if it were root for isolation.
        capture.install(root)
        assert stream not in root.handlers
        assert tui_handler in root.handlers

        root.error("hello from capture")
        assert any("hello from capture" in line for line in seen)

        capture.uninstall(root)
        assert stream in root.handlers
        assert tui_handler not in root.handlers
        root.handlers.clear()


class TestReviewActionAppLayout:
    async def test_compose_has_three_panels(self) -> None:
        release = threading.Event()

        def pipeline(_on_result: object, _on_phase: object) -> dict[str, int]:
            release.wait(timeout=10)
            return {
                "processed": 0,
                "committed": 0,
                "skipped": 0,
                "errors": 0,
                "restored": 0,
                "total_findings": 2,
            }

        meta = RunMeta(
            output_dir=Path("feedback"),
            provider="opencode",
            model="sonnet",
            dry_run=False,
            force=False,
            skip_errors=False,
            total_findings=2,
            coding_agent="opencode (sonnet)",
        )
        app = ReviewActionApp(meta, pipeline)
        try:
            async with app.run_test():
                assert app.query("#header-panel")
                assert app.query("#results-panel")
                assert app.query("#log-panel")
                assert app.query("#results-log")
                assert not app.query("#progress-panel")
                assert not app.query("#summary-panel")
                header = str(app.query_one("#header-meta", Static).content)
                assert "feedback" in header
                progress = str(app.query_one("#progress-label", Static).content)
                assert "Processing" in progress
                summary = str(app.query_one("#summary-strip", Static).content)
                assert "Fixed" in summary
        finally:
            release.set()
