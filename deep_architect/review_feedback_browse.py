"""Textual TUI browser for review-analyzer and review-action feedback directories.

Two modes:
  - **Action** (post-run): list outcomes from review-action, git log / diffs
  - **Verdict** (pre-run): browse analyzer VALID/REJECTED/BACKLOG findings

View-only — never mutates the feedback dir or git state.
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

from deep_architect.action_report import (
    OUTCOME_ORDER,
    ActionFindingRow,
    ActionReport,
    ActionRunBlock,
    has_action_results,
    load_action_report,
    outcome_filter_key,
    rows_for_outcome_filter,
    unescape_action_field,
)
from deep_architect.feedback_report import (
    DEFAULT_FEEDBACK_DIR,
    VERDICT_ORDER,
    FeedbackFinding,
    FeedbackReport,
    analysis_preview,
    findings_for_verdict,
    line_range_label,
    load_feedback_dir,
    parse_markdown_finding,
)
from deep_architect.git_view import (
    discover_repo_root,
    git_commit_diff,
    git_commit_log,
    git_commit_stat,
)
from deep_architect.logger import get_logger

logger = get_logger(__name__)

_VERDICT_STYLE: dict[str, str] = {
    "VALID": "bold green",
    "REJECTED": "bold red",
    "BACKLOG": "bold yellow",
    "UNKNOWN": "bold dim",
}

_OUTCOME_STYLE: dict[str, str] = {
    "Fixed": "bold green",
    "Error": "bold red",
    "Skipped": "bold yellow",
    "Rejected": "bold yellow",
    "Interrupted": "bold magenta",
    "Dry run": "bold cyan",
    "Not processed": "dim",
}

_OUTCOME_ICON: dict[str, str] = {
    "Fixed": "✓",
    "Error": "✗",
    "Skipped": "◷",
    "Rejected": "—",
    "Interrupted": "!",
    "Dry run": "·",
    "Not processed": "·",
}

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a terminal)
# ---------------------------------------------------------------------------


def format_action_stats(run: ActionRunBlock | None, row_count: int) -> str:
    """Header stats lines for the action summary screen."""
    if run is None:
        return (
            f"[bold]Findings:[/bold] {row_count}  "
            f"[dim](no review-action_summary.md run block)[/dim]"
        )
    progress = run.progress or f"{run.processed} processed"
    lines = [
        f"[bold]Run:[/bold] {run.started_at}  [bold]Agent:[/bold] {run.coding_agent}",
        (
            f"[bold green]Committed:[/bold green] {run.committed}  "
            f"[bold yellow]Skipped:[/bold yellow] {run.skipped}  "
            f"[bold red]Errors:[/bold red] {run.errors}  "
            f"Restored: {run.restored}  Processed: {run.processed}"
        ),
        f"Progress: {progress}  Interrupted: {'yes' if run.interrupted else 'no'}",
    ]
    if run.cost_line:
        lines.append(run.cost_line)
    return "\n".join(lines)


def format_action_row(row: ActionFindingRow, index: int) -> str:
    """One list line for an action finding."""
    bucket = outcome_filter_key(row.outcome_label)
    icon = _OUTCOME_ICON.get(bucket, "?")
    style = _OUTCOME_STYLE.get(bucket, "bold")
    sha = row.commit_sha or "—"
    detail = analysis_preview(row.summary or "—", max_len=72)
    file_path = analysis_preview(row.source_file, max_len=40)
    return (
        f"{index:3d}  {icon} [{style}]{row.outcome_label:<16}[/{style}]  "
        f"{row.finding_id:<16}  {file_path}  {sha}  {detail}"
    )


def format_action_detail(row: ActionFindingRow) -> str:
    """Scrollable body for the action detail screen."""
    parts: list[str] = [
        "[bold underline]Action Taken[/bold underline]",
        f"Status: {row.status or '(none)'}",
        f"Outcome: {row.outcome_label}",
        f"Timestamp: {row.timestamp or '—'}",
        f"Commit: {row.commit_sha or '—'}",
        "",
        "[bold underline]Summary[/bold underline]",
        unescape_action_field(row.summary) if row.summary else "—",
    ]
    if row.error_message:
        parts.extend(
            [
                "",
                "[bold underline]Error[/bold underline]",
                unescape_action_field(row.error_message),
            ]
        )

    parsed = parse_markdown_finding(row.path)
    if parsed is not None:
        parts.extend(
            [
                "",
                "[bold underline]Review Comment[/bold underline]",
                parsed.review_comment or "—",
                "",
                "[bold underline]Existing Code[/bold underline]",
                f"```\n{parsed.existing_code}\n```" if parsed.existing_code else "—",
                "",
                "[bold underline]Suggested Code[/bold underline]",
                f"```\n{parsed.suggested_code}\n```" if parsed.suggested_code else "—",
            ]
        )
    return "\n".join(parts)


def resolve_browse_mode(
    mode: str,
    feedback_dir: Path,
) -> str:
    """Return ``action`` or ``verdict`` given CLI mode and directory contents."""
    if mode == "action":
        return "action"
    if mode == "verdict":
        return "verdict"
    # auto
    if has_action_results(feedback_dir):
        return "action"
    return "verdict"


# ---------------------------------------------------------------------------
# Verdict mode screens (pre-action)
# ---------------------------------------------------------------------------


def _verdicts_present(report: FeedbackReport) -> list[str]:
    """Return verdicts that have findings, in preferred order then extras."""
    present = set(report.counts)
    ordered = [v for v in VERDICT_ORDER if v in present and report.counts.get(v, 0) > 0]
    extras = sorted(
        v for v in present if v not in VERDICT_ORDER and report.counts.get(v, 0) > 0
    )
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


class SummaryScreen(Screen[None]):
    """Top-level summary: counts and verdict drill-down."""

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "switch_action", "Action"),
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
            yield Label(
                "Select a verdict (Enter).  a = action results  ·  q quit",
                id="summary-hint",
            )
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

    def action_switch_action(self) -> None:
        app = self.app
        if isinstance(app, FeedbackBrowseApp):
            app.switch_to_action()


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
                f"[dim]n next · p prev · Esc back · "
                f"{self.index + 1}/{len(self.findings)}[/dim]",
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
                "[bold underline]Raw markdown[/bold underline]\n\n" + f.raw_markdown
            )

        self.query_one("#detail-content", Static).update(body)
        self.query_one("#detail-footer-hint", Static).update(
            f"[dim]n next · p prev · Esc back · "
            f"{self.index + 1}/{len(self.findings)}[/dim]"
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
# Action mode screens (post review-action)
# ---------------------------------------------------------------------------


class ActionSummaryScreen(Screen[None]):
    """Stats + filterable list of action outcomes (summary table)."""

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("v", "switch_verdict", "Verdict"),
        Binding("0", "filter_all", "All", show=False),
        Binding("1", "filter_fixed", "Fixed", show=False),
        Binding("2", "filter_error", "Error", show=False),
        Binding("3", "filter_skipped", "Skipped", show=False),
        Binding("4", "filter_rejected", "Rejected", show=False),
        Binding("enter", "open_row", "Open", show=False),
    ]

    def __init__(self, report: ActionReport) -> None:
        super().__init__()
        self.report = report
        self._filter: str | None = None
        self._rows: list[ActionFindingRow] = list(report.rows)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="summary-body"):
            yield Static(id="action-stats")
            yield Static(id="action-filter-hint")
            yield ListView(id="action-list")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_list()
        self.query_one("#action-list", ListView).focus()

    def _refresh_list(self) -> None:
        self._rows = rows_for_outcome_filter(self.report, self._filter)
        stats = format_action_stats(self.report.latest_run, len(self.report.rows))
        count_bits: list[str] = []
        for key in OUTCOME_ORDER:
            n = self.report.counts_by_outcome.get(key, 0)
            if n:
                style = _OUTCOME_STYLE.get(key, "bold")
                count_bits.append(f"[{style}]{key}[/{style}]: {n}")
        if count_bits:
            stats = stats + "\n" + "  ·  ".join(count_bits)

        self.query_one("#action-stats", Static).update(
            f"[bold]Feedback:[/bold] {self.report.directory}\n{stats}"
        )
        filt = self._filter or "All"
        self.query_one("#action-filter-hint", Static).update(
            f"[dim]Filter: {filt}  (0 all · 1 Fixed · 2 Error · 3 Skipped · 4 Rejected)  "
            f"v = verdict mode · Enter open · q quit[/dim]"
        )

        list_view = self.query_one("#action-list", ListView)
        list_view.clear()
        if not self._rows:
            list_view.append(ListItem(Label("(no findings in this filter)")))
            return
        for i, row in enumerate(self._rows):
            list_view.append(
                ListItem(Label(format_action_row(row, i + 1)), id=f"arow-{i}")
            )

    def _set_filter(self, key: str | None) -> None:
        self._filter = key
        self._refresh_list()

    def action_filter_all(self) -> None:
        self._set_filter(None)

    def action_filter_fixed(self) -> None:
        self._set_filter("Fixed")

    def action_filter_error(self) -> None:
        self._set_filter("Error")

    def action_filter_skipped(self) -> None:
        self._set_filter("Skipped")

    def action_filter_rejected(self) -> None:
        self._set_filter("Rejected")

    def _open_index(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            app = self.app
            repo = app.repo_root if isinstance(app, FeedbackBrowseApp) else None
            self.app.push_screen(
                ActionDetailScreen(self.report, self._rows, index, repo)
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("arow-"):
            try:
                idx = int(item_id.removeprefix("arow-"))
            except ValueError:
                return
            self._open_index(idx)

    def action_open_row(self) -> None:
        list_view = self.query_one("#action-list", ListView)
        if list_view.index is None:
            return
        self._open_index(list_view.index)

    def action_switch_verdict(self) -> None:
        app = self.app
        if isinstance(app, FeedbackBrowseApp):
            app.switch_to_verdict()


class ActionDetailScreen(Screen[None]):
    """Detail for one action row; g/s/d open git views when a commit exists."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("n", "next_row", "Next"),
        Binding("p", "prev_row", "Prev"),
        Binding("g", "git_log", "Log"),
        Binding("s", "git_stat", "Stat"),
        Binding("d", "git_diff", "Diff"),
    ]

    def __init__(
        self,
        report: ActionReport,
        rows: list[ActionFindingRow],
        index: int,
        repo_root: Path | None,
    ) -> None:
        super().__init__()
        self.report = report
        self.rows = rows
        self.index = index
        self.repo_root = repo_root

    @property
    def row(self) -> ActionFindingRow:
        return self.rows[self.index]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="detail-body"):
            yield Static(id="detail-header")
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="detail-content")
            yield Static(id="detail-footer-hint")
        yield Footer()

    def on_mount(self) -> None:
        self._render_row()
        self.query_one("#detail-scroll", VerticalScroll).focus()

    def _render_row(self) -> None:
        row = self.row
        bucket = outcome_filter_key(row.outcome_label)
        style = _OUTCOME_STYLE.get(bucket, "bold")
        header = (
            f"[{style}]{row.outcome_label}[/{style}]  "
            f"[bold]{row.source_file}[/bold]\n"
            f"id: {row.finding_id}  ·  {self.index + 1}/{len(self.rows)}  "
            f"·  {row.path.name}"
        )
        self.query_one("#detail-header", Static).update(header)
        self.query_one("#detail-content", Static).update(format_action_detail(row))
        git_hint = (
            "g log · s --stat · d full diff"
            if row.commit_sha
            else "(no commit — g/s/d unavailable)"
        )
        self.query_one("#detail-footer-hint", Static).update(
            f"[dim]n next · p prev · Esc back · {git_hint} · "
            f"{self.index + 1}/{len(self.rows)}[/dim]"
        )
        self.sub_title = f"{row.finding_id} ({self.index + 1}/{len(self.rows)})"

    def action_next_row(self) -> None:
        if self.index + 1 < len(self.rows):
            self.index += 1
            self._render_row()
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    def action_prev_row(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._render_row()
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    def _show_git(self, title: str, body: str) -> None:
        self.app.push_screen(GitViewScreen(title, body))

    def _require_commit(self) -> str | None:
        sha = self.row.commit_sha
        if not sha:
            self._show_git("Git", "No commit for this finding.")
            return None
        if self.repo_root is None:
            self._show_git(
                "Git",
                "No git repository found.\n"
                "Run review-feedback-browse from the target repo root.",
            )
            return None
        return sha

    def action_git_log(self) -> None:
        sha = self._require_commit()
        if sha is None or self.repo_root is None:
            return
        text = git_commit_log(self.repo_root, sha)
        self._show_git(f"git log -1 {sha}", text)

    def action_git_stat(self) -> None:
        sha = self._require_commit()
        if sha is None or self.repo_root is None:
            return
        text = git_commit_stat(self.repo_root, sha)
        self._show_git(f"git show --stat {sha}", text)

    def action_git_diff(self) -> None:
        sha = self._require_commit()
        if sha is None or self.repo_root is None:
            return
        text = git_commit_diff(self.repo_root, sha)
        self._show_git(f"git show {sha}", text)


class GitViewScreen(Screen[None]):
    """Scrollable git command output."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.view_title = title
        self.body = body

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="detail-body"):
            yield Static(f"[bold]{self.view_title}[/bold]", id="detail-header")
            with VerticalScroll(id="detail-scroll"):
                # Escape markup-ish sequences so raw diffs render literally.
                yield Static(self.body.replace("[", "\\["), id="detail-content")
            yield Static("[dim]Esc back · q quit[/dim]", id="detail-footer-hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#detail-scroll", VerticalScroll).focus()
        self.sub_title = self.view_title


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class FeedbackBrowseApp(App[None]):
    """View-only browser for review-analyzer / review-action feedback output."""

    TITLE = "review-feedback-browse"
    CSS = """
    #summary-body, #list-body, #detail-body {
        padding: 1 2;
    }
    #summary-header, #list-header, #detail-header, #action-stats {
        margin-bottom: 1;
    }
    #summary-hint, #action-filter-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #verdict-list, #finding-list, #action-list {
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

    def __init__(
        self,
        *,
        feedback_report: FeedbackReport | None,
        action_report: ActionReport | None,
        initial_mode: str,
        repo_root: Path | None,
    ) -> None:
        super().__init__()
        self.feedback_report = feedback_report
        self.action_report = action_report
        self.initial_mode = initial_mode
        self.repo_root = repo_root

    def on_mount(self) -> None:
        if self.initial_mode == "action" and self.action_report is not None:
            self.push_screen(ActionSummaryScreen(self.action_report))
        elif self.feedback_report is not None:
            self.push_screen(SummaryScreen(self.feedback_report))
        elif self.action_report is not None:
            self.push_screen(ActionSummaryScreen(self.action_report))
        else:
            self.exit(message="No report data to display")

    def switch_to_action(self) -> None:
        """Switch to action-results mode if data is available."""
        if self.action_report is None:
            self.notify("No review-action results in this directory", severity="warning")
            return
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.switch_screen(ActionSummaryScreen(self.action_report))

    def switch_to_verdict(self) -> None:
        """Switch to analyzer verdict mode if data is available."""
        if self.feedback_report is None:
            self.notify("No analyzer findings to browse", severity="warning")
            return
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.switch_screen(SummaryScreen(self.feedback_report))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="review-feedback-browse",
        description=(
            "Browse review-analyzer feedback and review-action results "
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
    parser.add_argument(
        "--mode",
        choices=("auto", "action", "verdict"),
        default="auto",
        help=(
            "UI mode: action (post review-action), verdict (analyzer triage), "
            "or auto (action if summary/Action Taken present)"
        ),
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

    if not feedback_dir.exists():
        msg = f"Feedback directory does not exist: {feedback_dir}"
        logger.error("%s", msg)
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    if not feedback_dir.is_dir():
        msg = f"Not a directory: {feedback_dir}"
        logger.error("%s", msg)
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    mode = resolve_browse_mode(args.mode, feedback_dir)

    feedback_report: FeedbackReport | None = None
    action_report: ActionReport | None = None

    try:
        feedback_report = load_feedback_dir(feedback_dir)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        logger.error("Failed to load feedback dir: %s", exc)
        # Still try action mode if requested.
        if mode == "verdict":
            print(f"Error loading {feedback_dir}: {exc}", file=sys.stderr)
            return 1

    try:
        action_report = load_action_report(feedback_dir)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        logger.error("Failed to load action report: %s", exc)
        if mode == "action":
            print(f"Error loading action report: {exc}", file=sys.stderr)
            return 1

    if mode == "action" and (action_report is None or not action_report.rows):
        msg = f"No action findings found in {feedback_dir}"
        logger.error("%s", msg)
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    if mode == "verdict" and (feedback_report is None or not feedback_report.findings):
        msg = f"No finding markdown files found in {feedback_dir}"
        logger.error("%s", msg)
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    if (
        (feedback_report is None or not feedback_report.findings)
        and (action_report is None or not action_report.rows)
    ):
        msg = f"No findings found in {feedback_dir}"
        logger.error("%s", msg)
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    # If auto/action but no action data, fall back to verdict.
    if mode == "action" and action_report is not None and not action_report.rows:
        mode = "verdict"
    if mode == "auto":
        mode = resolve_browse_mode("auto", feedback_dir)
        if mode == "action" and (action_report is None or not action_report.rows):
            mode = "verdict"

    repo_root = discover_repo_root(feedback_dir=feedback_dir)
    if repo_root is not None:
        logger.info("Git repo: %s", repo_root)
    else:
        logger.warning("No git repository found; commit log/diff will be unavailable")

    if feedback_report is not None:
        logger.info(
            "Loaded %d analyzer findings from %s (%s)",
            len(feedback_report.findings),
            feedback_dir,
            ", ".join(f"{k}={v}" for k, v in feedback_report.counts.items()),
        )
    if action_report is not None:
        logger.info(
            "Loaded %d action rows from %s (%s)",
            len(action_report.rows),
            feedback_dir,
            ", ".join(f"{k}={v}" for k, v in action_report.counts_by_outcome.items()),
        )

    app = FeedbackBrowseApp(
        feedback_report=feedback_report,
        action_report=action_report,
        initial_mode=mode,
        repo_root=repo_root,
    )
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
