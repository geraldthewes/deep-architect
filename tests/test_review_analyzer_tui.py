"""Unit tests for deep_architect.review_analyzer_tui."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from deep_architect.review_analyzer import (
    AnalysisResult,
    ProgressEvent,
    RunMeta,
    Verdict,
)
from deep_architect.review_analyzer_tui import (
    AnalyzerTuiResult,
    LoggingCapture,
    ReviewAnalyzerApp,
    TuiLogHandler,
    format_done_body,
    format_duration,
    format_header,
    format_log_line,
    format_progress_label,
    format_result_line,
    format_summary,
    is_browse_available,
    run_review_analyzer_tui,
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
            {"valid": 1, "rejected": 2, "backlog": 3, "timeout": 1},
            total=10,
            completed=7,
        )
        assert "VALID 1" in text
        assert "REJECTED 2" in text
        assert "BACKLOG 3" in text
        assert "TIMEOUT 1" in text
        assert "pending 3" in text

    def test_summary_severity_line(self) -> None:
        text = format_summary(
            {"valid": 2, "rejected": 0, "backlog": 0, "timeout": 0},
            total=2,
            completed=2,
            severity_counts={"high": 1, "low": 1},
        )
        assert "severity:" in text
        assert "high 1" in text
        assert "low 1" in text

    def test_result_line_includes_severity(self) -> None:
        event = ProgressEvent(
            completed=1,
            total=1,
            finding={
                "type": "comment",
                "path": "src/foo.py",
                "start_line": 1,
                "end_line": 2,
                "severity": "high",
            },
            analysis=AnalysisResult(
                Verdict.VALID, "real issue", "", retry_count=0, duration_s=3.0
            ),
            elapsed_s=3.0,
        )
        line = format_result_line(event)
        assert "VALID" in line
        assert "high" in line
        assert "src/foo.py" in line

    def test_result_line_missing_severity(self) -> None:
        event = ProgressEvent(
            completed=1,
            total=1,
            finding={
                "type": "comment",
                "path": "src/foo.py",
                "start_line": 1,
                "end_line": 1,
            },
            analysis=AnalysisResult(Verdict.BACKLOG, "later", "", duration_s=1.0),
            elapsed_s=1.0,
        )
        line = format_result_line(event)
        assert "—" in line

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
                Verdict.VALID,
                "Real bug in naming",
                "",
                retry_count=0,
                duration_s=12.4,
            ),
            elapsed_s=5.0,
        )
        line = format_result_line(event)
        assert "VALID" in line
        assert "src/foo.py" in line
        assert "Real bug" in line
        assert " 0r" in line
        assert "  12s" in line

    def test_result_line_with_retry_and_duration(self) -> None:
        event = ProgressEvent(
            completed=2,
            total=2,
            finding={
                "type": "comment",
                "path": "src/bar.py",
                "start_line": 1,
                "end_line": 3,
                "index": 1,
                "content": "slow",
            },
            analysis=AnalysisResult(
                Verdict.TIMEOUT,
                "opencode execution timed out after 2 attempts",
                "",
                retry_count=1,
                duration_s=301.2,
            ),
            elapsed_s=400.0,
        )
        line = format_result_line(event)
        assert "TIMEOUT" in line
        assert " 1r" in line
        assert " 301s" in line
        assert "src/bar.py" in line


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


class TestFormatDoneBody:
    def test_includes_summary_and_paths(self, tmp_path: Path) -> None:
        summary = tmp_path / "SUMMARY.md"
        index = tmp_path / "INDEX.md"
        body = format_done_body(
            "# Review Analysis Summary\n\nVALID: 1\n",
            summary_path=summary,
            index_path=index,
            browse_available=True,
        )
        assert "# Review Analysis Summary" in body
        assert "VALID: 1" in body
        assert f"Summary written to {summary}" in body
        assert f"Index written to {index}" in body
        assert "q quit · b browse findings" in body

    def test_omits_browse_when_unavailable(self) -> None:
        body = format_done_body("hello", browse_available=False)
        assert "hello" in body
        assert "b browse" not in body
        assert body.strip().endswith("q quit")

    def test_includes_error(self) -> None:
        body = format_done_body("", error="boom", browse_available=False)
        assert "Pipeline failed: boom" in body
        assert "q quit" in body


class TestIsBrowseAvailable:
    def test_false_when_summary_only(self, tmp_path: Path) -> None:
        (tmp_path / "SUMMARY.md").write_text("x", encoding="utf-8")
        assert (
            is_browse_available(
                summary_only=True,
                output_dir=tmp_path,
                summary_path=tmp_path / "SUMMARY.md",
            )
            is False
        )

    def test_true_when_summary_exists(self, tmp_path: Path) -> None:
        summary = tmp_path / "SUMMARY.md"
        summary.write_text("# Review Analysis Summary\n", encoding="utf-8")
        assert (
            is_browse_available(
                summary_only=False,
                output_dir=tmp_path,
                summary_path=summary,
            )
            is True
        )

    def test_true_when_finding_md_exists(self, tmp_path: Path) -> None:
        (tmp_path / "abcd-0.md").write_text("# finding\n", encoding="utf-8")
        assert (
            is_browse_available(
                summary_only=False,
                output_dir=tmp_path,
                summary_path=None,
            )
            is True
        )

    def test_false_when_dir_missing(self, tmp_path: Path) -> None:
        assert (
            is_browse_available(
                summary_only=False,
                output_dir=tmp_path / "nope",
                summary_path=None,
            )
            is False
        )


class TestRunReviewAnalyzerTuiFallback:
    def test_none_result_is_quit_interrupted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(self: ReviewAnalyzerApp) -> None:
            return None

        monkeypatch.setattr(ReviewAnalyzerApp, "run", fake_run)
        meta = RunMeta(
            ocr_file=Path("ocr.json"),
            model="standard/coder",
            concurrency=1,
            output_dir=Path("feedback"),
            summary_only=False,
            total_findings=3,
            raw_findings=3,
        )
        result = run_review_analyzer_tui(meta, lambda _on_result: {})
        assert isinstance(result, AnalyzerTuiResult)
        assert result.action == "quit"
        assert result.counts["interrupted"] == 1
        assert result.counts["total_findings"] == 3
        assert result.summary_path is None
