"""Unit tests for deep_architect.review_action_tui."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from deep_architect.review_action_harness import ProgressEvent, RunMeta
from deep_architect.review_action_tui import format_duration, render_dashboard


class TestFormatDuration:

    def test_seconds(self) -> None:
        assert format_duration(5) == "5s"

    def test_minutes(self) -> None:
        assert format_duration(65) == "1m05s"

    def test_hours(self) -> None:
        assert format_duration(3661) == "1h01m01s"


class TestRenderDashboard:

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

    def test_renders_key_fields(self) -> None:
        results = [
            ProgressEvent(
                completed=1,
                total=2,
                finding_id="abc12345-0",
                file_path="src/foo.py",
                outcome="completed",
                summary="Fix applied and committed",
                commit_sha="deadbeef",
                elapsed_s=5.0,
                stats={"committed": 1, "skipped": 0, "errors": 0, "restored": 0},
            ),
            ProgressEvent(
                completed=2,
                total=2,
                finding_id="def67890-0",
                file_path="src/bar.py",
                outcome="error",
                summary="Quality checks failed",
                commit_sha=None,
                elapsed_s=12.5,
                stats={"committed": 1, "skipped": 0, "errors": 1, "restored": 0},
            ),
        ]
        stats = {"committed": 1, "skipped": 0, "errors": 1, "restored": 0}
        renderable = render_dashboard(
            self._meta(),
            stats,
            completed=2,
            total=2,
            elapsed_s=12.5,
            results=results,
            console_height=40,
            console_width=100,
        )
        console = Console(record=True, force_terminal=True, width=100, height=40)
        console.print(renderable)
        text = console.export_text()
        assert "Review Action" in text
        assert "feedback" in text
        assert "opencode (sonnet)" in text
        assert "force" in text
        assert "Fixed" in text or "committed" in text.lower() or "1" in text
        assert "abc12345-0" in text or "src/foo.py" in text
        assert "completed" in text or "error" in text

    def test_empty_results_waiting(self) -> None:
        renderable = render_dashboard(
            self._meta(),
            {"committed": 0, "skipped": 0, "errors": 0, "restored": 0},
            completed=0,
            total=2,
            elapsed_s=0.0,
            results=[],
            current_phase="applying",
            current_finding="abc-0",
            console_height=40,
            console_width=80,
        )
        console = Console(record=True, force_terminal=True, width=80, height=40)
        console.print(renderable)
        text = console.export_text()
        # Table may wrap "waiting for results…" across lines.
        assert "waiting for" in text
        assert "results" in text
        assert "applying" in text
        assert "abc-0" in text
