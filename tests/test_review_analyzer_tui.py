"""Unit tests for deep_architect.review_analyzer_tui."""

from __future__ import annotations

import logging
from pathlib import Path

from deep_architect.review_analyzer import (
    AnalysisResult,
    ProgressEvent,
    RunMeta,
    Verdict,
)
from deep_architect.review_analyzer_tui import (
    LoggingCapture,
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
            name="deep_architect.review_analyzer",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed to analyze %s: returncode=%d",
            args=("foo.py", 0),
            exc_info=None,
        )
        line = format_log_line(record, max_len=200)
        assert line.startswith("ERROR")
        assert "review_analyzer:" in line
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
            ocr_file=Path("code-review.json"),
            model="standard/coder",
            concurrency=5,
            output_dir=Path("feedback"),
            summary_only=False,
            total_findings=2,
            raw_findings=4,
            ocr_status="success",
            ocr_summary={"files_reviewed": 3, "comments": 4},
        )

    def test_header_includes_ocr_and_model(self) -> None:
        text = format_header(self._meta())
        assert "code-review.json" in text
        assert "standard/coder" in text
        assert "concurrency" in text
        assert "feedback" in text
        assert "2" in text
        assert "4" in text
        assert "files_reviewed: 3" in text

    def test_summary_counts(self) -> None:
        text = format_summary(
            {"valid": 1, "rejected": 2, "backlog": 3},
            total=10,
            completed=6,
        )
        assert "VALID 1" in text
        assert "REJECTED 2" in text
        assert "BACKLOG 3" in text
        assert "pending 4" in text

    def test_progress_label(self) -> None:
        text = format_progress_label(1, 4, 12.0)
        assert "1/4" in text
        assert "Analyzing" in text

    def test_result_line(self) -> None:
        event = ProgressEvent(
            completed=1,
            total=2,
            finding={
                "type": "comment",
                "path": "src/foo.py",
                "start_line": 10,
                "end_line": 12,
                "index": 0,
                "content": "rename",
            },
            analysis=AnalysisResult(
                Verdict.VALID, "Real bug in naming", ""
            ),
            elapsed_s=5.0,
        )
        line = format_result_line(event)
        assert "VALID" in line
        assert "src/foo.py" in line
        assert "Real bug" in line


class TestTuiLogHandler:

    def test_forwards_truncated_line(self) -> None:
        seen: list[tuple[str, int]] = []
        handler = TuiLogHandler(
            lambda line, level: seen.append((line, level)), max_message_len=50
        )
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
        root = logging.getLogger("test_analyzer_tui_capture_root")
        root.handlers.clear()
        root.setLevel(logging.DEBUG)

        stream = logging.StreamHandler()
        stream.setLevel(logging.INFO)
        root.addHandler(stream)

        seen: list[str] = []
        tui_handler = TuiLogHandler(
            lambda line, _level: seen.append(line), level=logging.INFO
        )
        capture = LoggingCapture(handler=tui_handler)
        capture.install(root)
        assert stream not in root.handlers
        assert tui_handler in root.handlers

        root.error("hello from capture")
        assert any("hello from capture" in line for line in seen)

        capture.uninstall(root)
        assert stream in root.handlers
        assert tui_handler not in root.handlers
        root.handlers.clear()
