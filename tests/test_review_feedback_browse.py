"""Textual TUI tests for review-feedback-browse mode switching."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import ListView

from deep_architect.action_report import ActionFindingRow, ActionReport
from deep_architect.feedback_report import FeedbackFinding, FeedbackReport
from deep_architect.review_feedback_browse import (
    ActionDetailScreen,
    ActionSummaryScreen,
    FeedbackBrowseApp,
    SummaryScreen,
)


def _feedback_report(directory: Path) -> FeedbackReport:
    finding = FeedbackFinding(
        path=directory / "abc-0.md",
        finding_id="abc-0",
        source_file="src/example.py",
        line_start=10,
        line_end=12,
        verdict="VALID",
        existing_code="old()",
        suggested_code="new()",
        review_comment="fix it",
        analysis="Confirmed real issue.",
        raw_markdown="",
        severity="medium",
    )
    return FeedbackReport(
        directory=directory,
        summary_text="Coding agent: opencode",
        findings=[finding],
        counts={"VALID": 1},
    )


def _action_report(directory: Path) -> ActionReport:
    row = ActionFindingRow(
        finding_id="abc-0",
        path=directory / "abc-0.md",
        source_file="src/example.py",
        status="skipped",
        outcome_label="Skipped",
        summary="Already addressed",
        commit_sha=None,
        error_message=None,
        timestamp="t",
        severity="medium",
    )
    return ActionReport(
        directory=directory,
        latest_run=None,
        prior_runs=[],
        rows=[row],
        counts_by_outcome={"Skipped": 1},
    )


def _browse_app(tmp_path: Path, *, initial_mode: str = "action") -> FeedbackBrowseApp:
    return FeedbackBrowseApp(
        feedback_report=_feedback_report(tmp_path),
        action_report=_action_report(tmp_path),
        initial_mode=initial_mode,
        repo_root=None,
    )


class TestModeSwitch:
    async def test_v_then_a_swaps_mode_roots(self, tmp_path: Path) -> None:
        app = _browse_app(tmp_path, initial_mode="action")
        async with app.run_test() as pilot:
            assert isinstance(app.screen, ActionSummaryScreen)
            await pilot.press("v")
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen)
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, ActionSummaryScreen)

    async def test_switch_from_action_detail_lands_on_verdict(
        self, tmp_path: Path
    ) -> None:
        """v is bound on the action summary; overlays still unwind via the helper."""
        app = _browse_app(tmp_path, initial_mode="action")
        async with app.run_test() as pilot:
            list_view = app.screen.query_one("#action-list", ListView)
            list_view.index = 0
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ActionDetailScreen)
            app.switch_to_verdict()
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen)

    async def test_a_from_verdict_initial_mode(self, tmp_path: Path) -> None:
        app = _browse_app(tmp_path, initial_mode="verdict")
        async with app.run_test() as pilot:
            assert isinstance(app.screen, SummaryScreen)
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, ActionSummaryScreen)
