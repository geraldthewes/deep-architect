"""Unit tests for deep_architect.review_driver_tui."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from textual.widgets import Static

from deep_architect.review_driver import DriverPassRecord, DriverProgress, DriverRunMeta
from deep_architect.review_driver_tui import (
    DriverTuiResult,
    LoggingCapture,
    ReviewDriverApp,
    TuiLogHandler,
    classify_child_log_level,
    format_done_body,
    format_done_header,
    format_header,
    format_log_line,
    format_progress_label,
    format_summary,
    infra_error_count,
    is_browse_available,
    last_feedback_dir,
    run_review_driver_tui,
    truncate_message,
)


def _meta(**overrides: object) -> DriverRunMeta:
    values: dict[str, object] = {
        "source": "feat",
        "target": "main",
        "source_sha": "aaaaaaaaaaaa",
        "target_sha": "bbbbbbbbbbbb",
        "max_passes": 5,
        "k": 2,
        "output_dir": Path(".review-runs"),
        "resume": False,
    }
    values.update(overrides)
    return DriverRunMeta(**values)  # type: ignore[arg-type]


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
            name="deep_architect.review_driver",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="ocr failed on pass %s: rc=%d",
            args=(2, 1),
            exc_info=None,
        )
        line = format_log_line(record, max_len=200)
        assert line.startswith("ERROR")
        assert "review_driver:" in line
        assert "pass 2" in line
        assert "rc=1" in line

    def test_truncates_long_body(self) -> None:
        record = logging.LogRecord(
            name="pkg",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="x" * 80,
            args=(),
            exc_info=None,
        )
        line = format_log_line(record, max_len=40)
        assert line.endswith("…")


class TestFormatHelpers:
    def test_header_includes_source_target_and_k(self) -> None:
        text = format_header(_meta())
        assert "feat" in text
        assert "main" in text
        assert "aaaaaaaaaaaa" in text
        assert "K:" in text
        assert "5" in text
        assert ".review-runs" in text
        assert "resume" not in text

    def test_header_marks_resume(self) -> None:
        text = format_header(_meta(resume=True), pass_index=2)
        assert "resume" in text
        assert "2/5" in text

    def test_summary_counts(self) -> None:
        text = format_summary(
            novelty=1,
            zeros=0,
            k=2,
            valid_total=8,
            valid_high=3,
            valid_medium=5,
            valid_low=0,
            committed=4,
            errors=1,
        )
        assert "novelty 1" in text
        assert "zeros 0/2" in text
        assert "VALID 8" in text
        assert "H3" in text
        assert "M5" in text
        assert "committed 4" in text
        assert "errors 1" in text

    def test_summary_none_novelty(self) -> None:
        text = format_summary(
            novelty=None,
            zeros=0,
            k=2,
            valid_total=0,
            valid_high=0,
            valid_medium=0,
            valid_low=0,
            committed=0,
            errors=0,
        )
        assert "novelty —" in text

    def test_progress_label_phase(self) -> None:
        text = format_progress_label(2, 5, "ocr", 12.0)
        assert "2/5" in text
        assert "OCR" in text
        assert "Elapsed" in text
        assert "\n" not in text


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
        root = logging.getLogger("test_driver_tui_capture_root")
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


class TestClassifyChildLog:
    def test_error_lines(self) -> None:
        assert (
            classify_child_log_level(
                "Error: review failed: all 16 file review(s) failed"
            )
            == logging.ERROR
        )
        assert (
            classify_child_log_level(
                "[ocr] Subtask error for pkg/plant.py: context deadline exceeded"
            )
            == logging.ERROR
        )
        assert classify_child_log_level("ocr exited 1") == logging.ERROR
        assert classify_child_log_level("ocr timed out after 3600 seconds") == logging.ERROR
        assert (
            classify_child_log_level(
                "[ocr] failed frontend/src/api/plantTrackingAPI.ts: "
                "LLM completion error: context deadline exceeded"
            )
            == logging.ERROR
        )
        assert (
            classify_child_log_level(
                "[ocr] llm error src/a.py: context deadline exceeded"
            )
            == logging.ERROR
        )

    def test_info_lines(self) -> None:
        assert classify_child_log_level("TUI started — logging is confined") == logging.INFO
        assert classify_child_log_level("ocr: reviewing 3 files") == logging.INFO
        assert classify_child_log_level("[ocr] done src/a.py") == logging.INFO
        assert classify_child_log_level("[ocr] reviewing src/a.py (plan_task)") == logging.INFO


class TestInfraErrorCount:
    def test_counts_failed_and_partial_passes(self) -> None:
        progress = DriverProgress(
            status="failed",
            source="feat",
            target="main",
            source_sha="a",
            target_sha="b",
            max_passes=5,
            k=2,
            output_dir=".",
            passes=[
                DriverPassRecord(
                    pass_index=1,
                    ocr_json="r1.json",
                    feedback_dir="f1",
                    novelty=1,
                    valid_total=1,
                    action_errors=0,
                    action_committed=0,
                    status="complete",
                    ocr_status="partial",
                ),
                DriverPassRecord(
                    pass_index=2,
                    ocr_json="r2.json",
                    feedback_dir="f2",
                    novelty=0,
                    valid_total=0,
                    action_errors=0,
                    action_committed=0,
                    status="failed",
                    ocr_status="failed",
                ),
            ],
        )
        assert infra_error_count(progress) == 2


class TestFormatDoneHeader:
    def test_complete_vs_failed(self) -> None:
        assert "Review complete" in format_done_header(failed=False)
        assert "Review failed" in format_done_header(failed=True)
        assert "red" in format_done_header(failed=True)


class TestFormatDoneBody:
    def test_includes_report_and_path(self, tmp_path: Path) -> None:
        report = tmp_path / "REPORT.md"
        body = format_done_body(
            "# Review Driver Report\n\nConverged (K=2).\n",
            report_path=report,
            browse_available=True,
        )
        assert "# Review Driver Report" in body
        assert "Converged (K=2)." in body
        assert f"Report written to {report}" in body
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

    def test_stop_detail_as_error(self) -> None:
        body = format_done_body(
            "# Report\n",
            error="context deadline exceeded (25 LLM requests timed out at ~5m)",
            browse_available=False,
        )
        assert "Pipeline failed: context deadline exceeded" in body
        assert "# Report" in body


class TestIsBrowseAvailable:
    def test_true_when_dir_exists(self, tmp_path: Path) -> None:
        assert is_browse_available(tmp_path) is True

    def test_false_when_missing(self, tmp_path: Path) -> None:
        assert is_browse_available(tmp_path / "nope") is False

    def test_false_when_none(self) -> None:
        assert is_browse_available(None) is False

    def test_last_feedback_dir_picks_newest_existing(self, tmp_path: Path) -> None:
        first = tmp_path / "feedback-r1"
        first.mkdir()
        progress = DriverProgress(
            status="running",
            source="feat",
            target="main",
            source_sha="a",
            target_sha="b",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
            passes=[],
        )
        from deep_architect.review_driver import DriverPassRecord

        progress.passes.append(
            DriverPassRecord(
                pass_index=1,
                ocr_json=str(tmp_path / "code-review-r1.json"),
                feedback_dir=str(first),
                novelty=1,
                valid_total=1,
                action_errors=0,
                action_committed=0,
                status="complete",
            )
        )
        progress.passes.append(
            DriverPassRecord(
                pass_index=2,
                ocr_json=str(tmp_path / "code-review-r2.json"),
                feedback_dir=str(tmp_path / "feedback-r2"),
                novelty=0,
                valid_total=0,
                action_errors=0,
                action_committed=0,
                status="failed",
            )
        )
        assert last_feedback_dir(progress) == first


class TestRunReviewDriverTuiFallback:
    def test_none_result_is_quit_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(self: ReviewDriverApp) -> None:
            return None

        monkeypatch.setattr(ReviewDriverApp, "run", fake_run)
        result = run_review_driver_tui(_meta(), lambda _reporter: DriverProgress(
            status="converged",
            source="feat",
            target="main",
            source_sha="a",
            target_sha="b",
            max_passes=5,
            k=2,
            output_dir=".",
        ))
        assert isinstance(result, DriverTuiResult)
        assert result.action == "quit"
        assert result.progress.status == "failed"
        assert result.report_path is None


class TestReviewDriverAppLayout:
    async def test_compose_has_three_panels(self) -> None:
        release = threading.Event()

        def pipeline(_reporter: object) -> DriverProgress:
            release.wait(timeout=10)
            return DriverProgress(
                status="converged",
                source="feat",
                target="main",
                source_sha="a",
                target_sha="b",
                max_passes=5,
                k=2,
                output_dir=".",
            )

        app = ReviewDriverApp(_meta(), pipeline)
        try:
            async with app.run_test():
                assert app.query("#header-panel")
                assert app.query("#results-panel")
                assert app.query("#log-panel")
                assert app.query("#results-log")
                assert not app.query("#progress-panel")
                assert not app.query("#summary-panel")
                header = str(app.query_one("#header-meta", Static).content)
                assert "feat" in header
                assert "main" in header
                progress = str(app.query_one("#progress-label", Static).content)
                assert "Pass" in progress
                summary = str(app.query_one("#summary-strip", Static).content)
                assert "novelty" in summary
        finally:
            release.set()


def _blocking_progress() -> DriverProgress:
    return DriverProgress(
        status="converged",
        source="feat",
        target="main",
        source_sha="a",
        target_sha="b",
        max_passes=5,
        k=2,
        output_dir=".",
    )


class TestReviewDriverAppStop:
    async def test_first_q_is_graceful_second_q_force_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            "deep_architect.review_driver_tui.request_interrupt",
            lambda: calls.append("interrupt"),
        )
        monkeypatch.setattr(
            "deep_architect.review_driver_tui.request_force_stop",
            lambda: calls.append("force"),
        )
        monkeypatch.setattr(
            "deep_architect.review_driver_tui._request_nested_shutdown",
            lambda: calls.append("nested"),
        )
        release = threading.Event()

        def pipeline(_reporter: object) -> DriverProgress:
            release.wait(timeout=10)
            return _blocking_progress()

        app = ReviewDriverApp(_meta(), pipeline)
        try:
            async with app.run_test() as pilot:
                await pilot.press("q")
                status = str(app.query_one("#status-line", Static).content)
                assert "Press q again" in status
                assert calls == ["interrupt", "nested"]
                await pilot.press("q")
                status = str(app.query_one("#status-line", Static).content)
                assert "Force stop" in status
                assert "killing current OCR" in status
                assert calls == ["interrupt", "nested", "force"]
                await pilot.press("q")
                status = str(app.query_one("#status-line", Static).content)
                assert "already requested" in status
                assert calls == ["interrupt", "nested", "force"]
        finally:
            release.set()
