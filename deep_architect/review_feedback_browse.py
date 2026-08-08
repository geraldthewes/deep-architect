"""Textual TUI browser for review-analyzer feedback directories.

View-only navigation: summary → verdict list → finding detail.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from deep_architect.feedback_report import (
    DEFAULT_FEEDBACK_DIR,
    VERDICT_ORDER,
    FeedbackFinding,
    FeedbackReport,
    analysis_preview,
    findings_for_verdict,
    line_range_label,
    load_feedback_dir,
)
from deep_architect.logger import get_logger

logger = get_logger(__name__)

_VERDICT_STYLE: dict[str, str] = {
    "VALID": "bold green",
    "REJECTED": "bold red",
    "BACKLOG": "bold yellow",
    "UNKNOWN": "bold dim",
}


def _verdicts_present(report: FeedbackReport) -> list[str]:
    """Return verdicts that have findings, in preferred order then extras."""
    present = set(report.counts)
    ordered = [v for v in VERDICT_ORDER if v in present and report.counts.get(v, 0) > 0]
    extras = sorted(v for v in present if v not in VERDICT_ORDER and report.counts.get(v, 0) > 0)
    return ordered + extras


def _summary_meta_line(summary_text: str | None) -> str:
    """Extract a short meta line from SUMMARY.md (e.g. coding agent)."""
    if not summary_text:
        return ""
    for line in summary_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("coding agent:"):
            return stripped
    return ""


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class SummaryScreen(Screen[None]):
    """Top-level summary: counts and verdict drill-down."""

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "select_verdict", "Open", show=False),
    ]

    def __init__(self, report: FeedbackReport) -> None:
        super().__init__()
        self.report = report
        self._verdicts = _verdicts_present(report)

    def compose(self) -> ComposeResult:
        yield Header()
        meta = _summary_meta_line(self.report.summary_text)
        total = len(self.report.findings)
        header_lines = [
            f"[bold]Feedback:[/bold] {self.report.directory}",
            f"[bold]Total findings:[/bold] {total}",
        ]
        if meta:
            header_lines.append(meta)

        count_bits: list[str] = []
        for verdict in self._verdicts:
            style = _VERDICT_STYLE.get(verdict, "bold")
            n = self.report.counts.get(verdict, 0)
            count_bits.append(f"[{style}]{verdict}[/{style}]: {n}")
        if count_bits:
            header_lines.append("  ·  ".join(count_bits))

        with Vertical(id="summary-body"):
            yield Static("\n".join(header_lines), id="summary-header")
            yield Label("Select a verdict (Enter to open):", id="summary-hint")
            items = [
                ListItem(
                    Label(
                        f"{verdict}  ({self.report.counts.get(verdict, 0)})",
                        classes=f"verdict-{verdict.lower()}",
                    ),
                    id=f"verdict-{verdict}",
                )
                for verdict in self._verdicts
            ]
            yield ListView(*items, id="verdict-list")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#verdict-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("verdict-"):
            verdict = item_id.removeprefix("verdict-")
            self.app.push_screen(FindingListScreen(self.report, verdict))

    def action_select_verdict(self) -> None:
        list_view = self.query_one("#verdict-list", ListView)
        if list_view.index is None or not self._verdicts:
            return
        idx = list_view.index
        if 0 <= idx < len(self._verdicts):
            self.app.push_screen(FindingListScreen(self.report, self._verdicts[idx]))


class FindingListScreen(Screen[None]):
    """List of findings for one verdict."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("enter", "open_finding", "Open", show=False),
    ]

    def __init__(self, report: FeedbackReport, verdict: str) -> None:
        super().__init__()
        self.report = report
        self.verdict = verdict
        self.findings = findings_for_verdict(report, verdict)

    def compose(self) -> ComposeResult:
        yield Header()
        style = _VERDICT_STYLE.get(self.verdict, "bold")
        title = (
            f"[{style}]{self.verdict}[/{style}]  "
            f"({len(self.findings)} findings)  —  Esc back"
        )
        with Vertical(id="list-body"):
            yield Static(title, id="list-header")
            items: list[ListItem] = []
            for i, finding in enumerate(self.findings, 1):
                lines = line_range_label(finding.line_start, finding.line_end)
                preview = analysis_preview(finding.analysis or finding.review_comment)
                label = (
                    f"{i:3d}  {finding.source_file}{lines}  "
                    f"[{finding.finding_id}]  {preview}"
                )
                items.append(ListItem(Label(label), id=f"finding-{i - 1}"))
            if not items:
                items.append(ListItem(Label("(no findings)")))
            yield ListView(*items, id="finding-list")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#finding-list", ListView).focus()

    def _open_index(self, index: int) -> None:
        if 0 <= index < len(self.findings):
            self.app.push_screen(
                FindingDetailScreen(self.report, self.verdict, self.findings, index)
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("finding-"):
            try:
                idx = int(item_id.removeprefix("finding-"))
            except ValueError:
                return
            self._open_index(idx)

    def action_open_finding(self) -> None:
        list_view = self.query_one("#finding-list", ListView)
        if list_view.index is None:
            return
        self._open_index(list_view.index)


class FindingDetailScreen(Screen[None]):
    """Full detail for one finding; n/p walk the current verdict set."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("n", "next_finding", "Next"),
        Binding("p", "prev_finding", "Prev"),
    ]

    def __init__(
        self,
        report: FeedbackReport,
        verdict: str,
        findings: list[FeedbackFinding],
        index: int,
    ) -> None:
        super().__init__()
        self.report = report
        self.verdict = verdict
        self.findings = findings
        self.index = index

    @property
    def finding(self) -> FeedbackFinding:
        return self.findings[self.index]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="detail-body"):
            yield Static(id="detail-header")
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="detail-content")
            yield Static(
                f"[dim]n next · p prev · Esc back · {self.index + 1}/{len(self.findings)}[/dim]",
                id="detail-footer-hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._render_finding()
        self.query_one("#detail-scroll", VerticalScroll).focus()

    def _render_finding(self) -> None:
        f = self.finding
        style = _VERDICT_STYLE.get(f.verdict, "bold")
        lines = line_range_label(f.line_start, f.line_end)
        header = (
            f"[{style}]{f.verdict}[/{style}]  "
            f"[bold]{f.source_file}{lines}[/bold]\n"
            f"id: {f.finding_id}  ·  {self.index + 1}/{len(self.findings)}  "
            f"·  {f.path.name}"
        )
        self.query_one("#detail-header", Static).update(header)

        if f.review_comment or f.existing_code or f.analysis:
            suggested = f.suggested_code if f.suggested_code else "*(none)*"
            body = "\n".join(
                [
                    "[bold underline]Review Comment[/bold underline]",
                    f.review_comment or "*(empty)*",
                    "",
                    "[bold underline]Existing Code[/bold underline]",
                    f"```\n{f.existing_code}\n```" if f.existing_code else "*(empty)*",
                    "",
                    "[bold underline]Suggested Code[/bold underline]",
                    f"```\n{f.suggested_code}\n```" if f.suggested_code else suggested,
                    "",
                    "[bold underline]LLM Analysis[/bold underline]",
                    f.analysis or "*(empty)*",
                ]
            )
        else:
            body = (
                "[bold underline]Raw markdown[/bold underline]\n\n"
                + f.raw_markdown
            )

        self.query_one("#detail-content", Static).update(body)
        self.query_one("#detail-footer-hint", Static).update(
            f"[dim]n next · p prev · Esc back · {self.index + 1}/{len(self.findings)}[/dim]"
        )
        self.sub_title = f"{f.finding_id} ({self.index + 1}/{len(self.findings)})"

    def action_next_finding(self) -> None:
        if self.index + 1 < len(self.findings):
            self.index += 1
            self._render_finding()
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    def action_prev_finding(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._render_finding()
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class FeedbackBrowseApp(App[None]):
    """View-only browser for review-analyzer feedback output."""

    TITLE = "review-feedback-browse"
    CSS = """
    #summary-body, #list-body, #detail-body {
        padding: 1 2;
    }
    #summary-header, #list-header, #detail-header {
        margin-bottom: 1;
    }
    #summary-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #verdict-list, #finding-list {
        height: 1fr;
        border: solid $primary;
    }
    #detail-scroll {
        height: 1fr;
        border: solid $primary;
        padding: 0 1;
    }
    #detail-footer-hint {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, report: FeedbackReport) -> None:
        super().__init__()
        self.report = report

    def on_mount(self) -> None:
        self.push_screen(SummaryScreen(self.report))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="review-feedback-browse",
        description=(
            "Browse review-analyzer feedback (SUMMARY / INDEX / per-finding .md) "
            "in a view-only Textual TUI."
        ),
    )
    parser.add_argument(
        "feedback_dir",
        type=Path,
        nargs="?",
        default=DEFAULT_FEEDBACK_DIR,
        help=f"Directory written by review-analyzer (default: {DEFAULT_FEEDBACK_DIR}/)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for review-feedback-browse."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    feedback_dir: Path = args.feedback_dir

    try:
        report = load_feedback_dir(feedback_dir)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except NotADirectoryError as exc:
        logger.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        logger.error("Failed to load feedback directory %s: %s", feedback_dir, exc)
        print(f"Error loading {feedback_dir}: {exc}", file=sys.stderr)
        return 1

    if not report.findings:
        msg = f"No finding markdown files found in {feedback_dir}"
        logger.error("%s", msg)
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    logger.info(
        "Loaded %d findings from %s (%s)",
        len(report.findings),
        feedback_dir,
        ", ".join(f"{k}={v}" for k, v in report.counts.items()),
    )

    app = FeedbackBrowseApp(report)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
