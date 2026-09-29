"""Unit tests for review intent resolution and the OCR length gate."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from deep_architect.review_intent import (
    BACKGROUND_OPEN_TAG,
    INTENT_FILENAME,
    INTENT_INSTRUCTION,
    INTENT_SOURCE_FILENAME,
    INTENT_SUMMARY_FILENAME,
    OCR_BACKGROUND_HARD_LIMIT,
    IntentDeclined,
    IntentError,
    PreparedIntent,
    extract_opencode_text,
    measured_length,
    prepare_review_intent,
    render_intent,
    sanitize_markdown,
    summary_budget,
)


def _refuse_summary(_prompt: str) -> str:
    raise AssertionError("summarize should not be called")


def _prepare(
    tmp_path: Path,
    *,
    source: str = "feat",
    knowledge_dir: Path | None = None,
    background: str | None = None,
    background_file: Path | None = None,
    branch_name: str | None = None,
    reuse_existing: bool = False,
    interactive: bool = False,
    ask_summarize: Callable[[int], bool] | None = None,
    ask_accept: Callable[[str], bool] | None = None,
    summarize: Callable[[str], str] | None = None,
) -> PreparedIntent:
    return prepare_review_intent(
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        source=source,
        knowledge_dir=knowledge_dir if knowledge_dir is not None else tmp_path / "knowledge",
        background=background,
        background_file=background_file,
        branch_name=branch_name,
        reuse_existing=reuse_existing,
        interactive=interactive,
        ask_summarize=ask_summarize,
        ask_accept=ask_accept,
        summarize=summarize if summarize is not None else _refuse_summary,
    )


def _ticket(tmp_path: Path, ticket_id: str, body: str) -> Path:
    directory = tmp_path / "knowledge" / "tickets"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticket_id}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_sanitize_drops_controls_and_collapses_blank_lines() -> None:
    raw = "keep\r\n\n\n\nthis\x00\u200b"
    assert sanitize_markdown(raw) == "keep\n\nthis"


def test_under_limit_passes_through_without_summarizing(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path, background="Add rate limiting to login.")
    assert prepared.label == "flag"
    assert prepared.path is not None
    text = prepared.path.read_text(encoding="utf-8")
    assert text.startswith(INTENT_INSTRUCTION)
    assert "Add rate limiting to login." in text
    assert measured_length(text) <= OCR_BACKGROUND_HARD_LIMIT
    assert not (tmp_path / "run" / INTENT_SOURCE_FILENAME).exists()


def test_ticket_from_source_branch_name(tmp_path: Path) -> None:
    _ticket(tmp_path, "PROJ-0013", "# Title\n\nShip rate limiting.\n")
    prepared = _prepare(tmp_path, source="PROJ-0013")
    assert prepared.label == "ticket PROJ-0013"
    assert prepared.path is not None
    text = prepared.path.read_text(encoding="utf-8")
    assert "Ship rate limiting." in text


def test_ticket_from_current_branch_when_source_has_no_id(tmp_path: Path) -> None:
    _ticket(tmp_path, "PROJ-7", "Needed for the audit.\n")
    prepared = _prepare(tmp_path, source="abc123", branch_name="feature/PROJ-7-audit")
    assert prepared.label == "ticket PROJ-7"


def test_explicit_file_suppresses_ticket_lookup(tmp_path: Path) -> None:
    _ticket(tmp_path, "PROJ-0013", "ticket body that must not be used\n")
    requirements = tmp_path / "req.md"
    requirements.write_text("Requirements say ship the limiter.\n", encoding="utf-8")
    prepared = _prepare(
        tmp_path,
        source="PROJ-0013",
        background="Focus on auth.",
        background_file=requirements,
    )
    assert prepared.label == "flag"
    assert prepared.path is not None
    text = prepared.path.read_text(encoding="utf-8")
    assert "Focus on auth." in text
    assert "Requirements say ship the limiter." in text
    assert "ticket body" not in text


def test_missing_background_file_fails(tmp_path: Path) -> None:
    with pytest.raises(IntentError, match="not found"):
        _prepare(tmp_path, background_file=tmp_path / "missing.md")


def test_missing_ticket_reviews_without_intent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING")
    prepared = _prepare(tmp_path, source="PROJ-404")
    assert prepared.label == "none"
    assert prepared.path is None
    assert "No ticket file" in caplog.text
    assert not (tmp_path / "run" / INTENT_FILENAME).exists()


def test_reserved_delimiter_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IntentError, match="ocr_user_background"):
        _prepare(tmp_path, background=f"before {BACKGROUND_OPEN_TAG} after")


def test_no_source_is_none(tmp_path: Path) -> None:
    prepared = _prepare(tmp_path)
    assert prepared.label == "none"
    assert prepared.path is None


def test_non_tty_summarizes_when_over_limit(tmp_path: Path) -> None:
    calls: list[str] = []

    def _summarize(prompt: str) -> str:
        calls.append(prompt)
        return "Why: rate limiting. Required: reject bursts over 10/s."

    body = "a" * (OCR_BACKGROUND_HARD_LIMIT + 50)
    prepared = _prepare(tmp_path, background=body, interactive=False, summarize=_summarize)
    assert prepared.label == "summarized from flag"
    assert len(calls) == 1
    assert "code reviewer" in calls[0]
    assert prepared.path is not None
    text = prepared.path.read_text(encoding="utf-8")
    assert "reject bursts" in text
    assert measured_length(text) <= OCR_BACKGROUND_HARD_LIMIT
    original = (tmp_path / "run" / INTENT_SOURCE_FILENAME).read_text(encoding="utf-8")
    assert original.startswith("a" * 20)


def test_tty_decline_does_not_write_intent(tmp_path: Path) -> None:
    def _summarize(_prompt: str) -> str:
        raise AssertionError("summarize should not run after a decline")

    with pytest.raises(IntentDeclined, match="Not summarizing"):
        _prepare(
            tmp_path,
            background="b" * (OCR_BACKGROUND_HARD_LIMIT + 10),
            interactive=True,
            ask_summarize=lambda _count: False,
            summarize=_summarize,
        )
    assert not (tmp_path / "run" / INTENT_FILENAME).exists()


def test_tty_reject_summary_leaves_summary_file(tmp_path: Path) -> None:
    def _summarize(_prompt: str) -> str:
        return "Condensed intent for the reviewer."

    def _reject(summary: str) -> bool:
        assert summary == "Condensed intent for the reviewer."
        return False

    with pytest.raises(IntentDeclined, match="Summary rejected"):
        _prepare(
            tmp_path,
            background="c" * (OCR_BACKGROUND_HARD_LIMIT + 10),
            interactive=True,
            ask_summarize=lambda _count: True,
            ask_accept=_reject,
            summarize=_summarize,
        )
    summary_path = tmp_path / "run" / INTENT_SUMMARY_FILENAME
    assert summary_path.read_text(encoding="utf-8").startswith("Condensed intent")
    assert not (tmp_path / "run" / INTENT_FILENAME).exists()


def test_tty_accepts_summary(tmp_path: Path) -> None:
    seen: list[str] = []

    def _accept(summary: str) -> bool:
        seen.append(summary)
        return True

    prepared = _prepare(
        tmp_path,
        background="d" * (OCR_BACKGROUND_HARD_LIMIT + 10),
        interactive=True,
        ask_summarize=lambda _count: True,
        ask_accept=_accept,
        summarize=lambda _prompt: "Accepted summary.",
    )
    assert seen == ["Accepted summary."]
    assert prepared.path is not None
    assert "Accepted summary." in prepared.path.read_text(encoding="utf-8")


def test_second_over_limit_summary_fails(tmp_path: Path) -> None:
    def _summarize(prompt: str) -> str:
        assert "previous summary" in prompt or "code reviewer" in prompt
        return "e" * (summary_budget() + 100)

    with pytest.raises(IntentError, match="after one retry"):
        _prepare(
            tmp_path,
            background="f" * (OCR_BACKGROUND_HARD_LIMIT + 10),
            interactive=False,
            summarize=_summarize,
        )
    assert not (tmp_path / "run" / INTENT_FILENAME).exists()


def test_over_limit_summary_retries_once_then_fits(tmp_path: Path) -> None:
    calls = {"n": 0}

    def _summarize(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "g" * (summary_budget() + 50)
        assert "previous summary" in prompt
        return "Fits on the second try."

    prepared = _prepare(
        tmp_path,
        background="h" * (OCR_BACKGROUND_HARD_LIMIT + 10),
        interactive=False,
        summarize=_summarize,
    )
    assert calls["n"] == 2
    assert prepared.path is not None
    assert "Fits on the second try." in prepared.path.read_text(encoding="utf-8")


def test_resume_reuses_saved_intent_without_summarizing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / INTENT_FILENAME).write_text(render_intent("saved reason"), encoding="utf-8")
    (run / "intent-meta.json").write_text(
        json.dumps({"label": "ticket PROJ-0013"}) + "\n",
        encoding="utf-8",
    )
    prepared = _prepare(
        tmp_path,
        reuse_existing=True,
        background="new text that must be ignored",
        summarize=_refuse_summary,
    )
    assert prepared.label == "ticket PROJ-0013"
    assert prepared.path == (run / INTENT_FILENAME).resolve()


def test_resume_without_saved_intent_stays_empty(tmp_path: Path) -> None:
    _ticket(tmp_path, "PROJ-0013", "should not load on resume\n")
    prepared = _prepare(tmp_path, source="PROJ-0013", reuse_existing=True)
    assert prepared.label == "none"
    assert prepared.path is None


def test_extract_opencode_text_reads_stream_parts() -> None:
    raw = "\n".join(
        [
            json.dumps({"type": "text", "part": {"type": "text", "text": "Why: "}}),
            json.dumps({"content": "limit bursts."}),
        ]
    )
    assert extract_opencode_text(raw) == "Why: limit bursts."
