"""Unit tests for deep_architect.backlog_dedup (mocked LLM)."""
from __future__ import annotations

import json
from pathlib import Path

from deep_architect.backlog_dedup import (
    apply_dedup_decision,
    build_dedup_prompt,
    parse_dedup_response,
    promote_backlog_findings,
)
from deep_architect.backlog_store import (
    BacklogAction,
    CatalogEntry,
    create_backlog_entry,
    load_backlog_catalog,
)
from deep_architect.review_analyzer import (
    AnalysisResult,
    Verdict,
    generate_markdown_content,
    generate_output_filename,
)


def _comment_finding(**overrides: object) -> dict:
    base: dict = {
        "type": "comment",
        "path": "src/app.py",
        "start_line": 10,
        "end_line": 20,
        "index": 0,
        "content": "Missing timeout on HTTP client",
        "existing_code": "requests.get(url)",
        "suggestion_code": "requests.get(url, timeout=30)",
    }
    base.update(overrides)
    return base


def _analysis(
    verdict: Verdict = Verdict.BACKLOG,
    text: str = "Defer timeout fix",
) -> AnalysisResult:
    return AnalysisResult(verdict=verdict, analysis=text, raw_response="")


class TestParseDedupResponse:
    def test_valid_create(self) -> None:
        raw = json.dumps(
            {
                "action": "create",
                "match_path": None,
                "title": "HTTP client timeout",
                "problem": "No timeout.",
                "recommendation": "Add timeout=30.",
                "rationale": "New issue",
            }
        )
        d = parse_dedup_response(
            raw,
            finding=_comment_finding(),
            analysis=_analysis(),
            catalog=[],
        )
        assert d.action == "create"
        assert d.title == "HTTP client timeout"
        assert d.match_path is None

    def test_invalid_json_falls_back_to_create(self) -> None:
        d = parse_dedup_response(
            "not json at all",
            finding=_comment_finding(),
            analysis=_analysis("analysis text here"),
            catalog=[],
        )
        assert d.action == "create"
        assert d.title  # non-empty

    def test_invalid_match_path_falls_back_to_create(self) -> None:
        raw = json.dumps(
            {
                "action": "update_backlog",
                "match_path": "knowledge/backlog/does-not-exist.md",
                "title": "X",
                "problem": "p",
                "recommendation": "r",
                "rationale": "guess",
            }
        )
        d = parse_dedup_response(
            raw,
            finding=_comment_finding(),
            analysis=_analysis(),
            catalog=[],
        )
        assert d.action == "create"
        assert d.match_path is None

    def test_valid_update_path(self) -> None:
        catalog = [
            CatalogEntry(
                path="knowledge/backlog/http-timeout.md",
                title="HTTP timeout",
                kind="backlog",
            )
        ]
        raw = json.dumps(
            {
                "action": "update_backlog",
                "match_path": "knowledge/backlog/http-timeout.md",
                "title": "HTTP timeout",
                "problem": "p",
                "recommendation": "r",
                "rationale": "same theme",
            }
        )
        d = parse_dedup_response(
            raw,
            finding=_comment_finding(),
            analysis=_analysis(),
            catalog=catalog,
        )
        assert d.action == "update_backlog"
        assert d.match_path == "knowledge/backlog/http-timeout.md"

    def test_fenced_json(self) -> None:
        raw = (
            '```json\n{"action":"skip","match_path":null,"title":"T",'
            '"problem":"p","recommendation":"r","rationale":"noise"}\n```'
        )
        d = parse_dedup_response(
            raw,
            finding=_comment_finding(),
            analysis=_analysis(),
            catalog=[],
        )
        assert d.action == "skip"


class TestApplyAndPromote:
    def test_create_writes_backlog_and_stamps_feedback(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        output = tmp_path / "feedback"
        output.mkdir()
        finding = _comment_finding()
        analysis = _analysis()
        fb = output / generate_output_filename(finding)
        fb.write_text(generate_markdown_content(finding, analysis), encoding="utf-8")

        from deep_architect.backlog_dedup import DedupDecision

        decision = DedupDecision(
            action="create",
            match_path=None,
            title="HTTP client timeout",
            problem="No timeout on requests.",
            recommendation="Add timeout parameter.",
            rationale="new",
        )
        result = apply_dedup_decision(
            decision,
            finding=finding,
            analysis=analysis,
            knowledge_dir=knowledge,
            ocr_file=tmp_path / "ocr.json",
            output_dir=output,
            feedback_path=fb,
        )
        assert result.action == BacklogAction.CREATED
        assert result.target is not None
        assert (knowledge / "backlog").is_dir()
        assert list((knowledge / "backlog").glob("*.md"))
        stamped = fb.read_text(encoding="utf-8")
        assert "## Backlog disposition" in stamped
        assert "created" in stamped

    def test_update_existing_backlog(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        from deep_architect.backlog_store import Occurrence

        existing = create_backlog_entry(
            knowledge,
            title="HTTP client timeout",
            problem="old",
            recommendation="old rec",
            source_report="old.json",
            occurrence=Occurrence(
                feedback_rel="feedback/old.md",
                file_path="src/app.py",
                lines="1-2",
                date="2026-01-01",
                analysis_preview="old",
            ),
        )
        output = tmp_path / "feedback"
        output.mkdir()
        finding = _comment_finding(index=1)
        analysis = _analysis(text="again")
        fb = output / generate_output_filename(finding)
        fb.write_text(generate_markdown_content(finding, analysis), encoding="utf-8")

        from deep_architect.backlog_dedup import DedupDecision

        # relative path as catalog would list
        rel = f"knowledge/backlog/{existing.name}"
        decision = DedupDecision(
            action="update_backlog",
            match_path=rel,
            title="HTTP client timeout",
            problem="p",
            recommendation="r",
            rationale="match",
        )
        result = apply_dedup_decision(
            decision,
            finding=finding,
            analysis=analysis,
            knowledge_dir=knowledge,
            ocr_file=tmp_path / "ocr.json",
            output_dir=output,
            feedback_path=fb,
        )
        assert result.action == BacklogAction.UPDATED
        text = existing.read_text(encoding="utf-8")
        assert "feedback/old.md" in text or "old.md" in text
        assert generate_output_filename(finding) in text or fb.name in text

    def test_link_ticket_does_not_create_backlog(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        tickets = knowledge / "tickets"
        tickets.mkdir(parents=True)
        ticket = tickets / "PROJ-0042.md"
        ticket.write_text(
            "---\nid: PROJ-0042\ntitle: HTTP timeouts\nstatus: spec\n---\n\n# T\n",
            encoding="utf-8",
        )
        output = tmp_path / "feedback"
        output.mkdir()
        finding = _comment_finding()
        analysis = _analysis()
        fb = output / generate_output_filename(finding)
        fb.write_text(generate_markdown_content(finding, analysis), encoding="utf-8")

        from deep_architect.backlog_dedup import DedupDecision

        decision = DedupDecision(
            action="link_ticket",
            match_path="knowledge/tickets/PROJ-0042.md",
            title="HTTP timeouts",
            problem="p",
            recommendation="r",
            rationale="already ticketed",
        )
        result = apply_dedup_decision(
            decision,
            finding=finding,
            analysis=analysis,
            knowledge_dir=knowledge,
            ocr_file=tmp_path / "ocr.json",
            output_dir=output,
            feedback_path=fb,
        )
        assert result.action == BacklogAction.LINKED_TO_TICKET
        assert not (knowledge / "backlog").exists() or not list(
            (knowledge / "backlog").glob("*.md")
        )
        # Ticket unchanged
        assert "Occurrences" not in ticket.read_text(encoding="utf-8")
        assert "linked_to_ticket" in fb.read_text(encoding="utf-8")

    def test_promote_skips_non_backlog(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        output = tmp_path / "feedback"
        output.mkdir()
        finding = _comment_finding()
        analysis = _analysis(Verdict.VALID, "fix now")
        called: list[str] = []

        def runner(prompt: str, model: str) -> str:
            called.append(prompt)
            return "{}"

        counts = promote_backlog_findings(
            [(finding, analysis)],
            knowledge_dir=knowledge,
            ocr_file=tmp_path / "ocr.json",
            output_dir=output,
            model="test/model",
            timeout=30,
            runner=runner,
        )
        assert called == []
        assert counts.created == 0

    def test_promote_with_mocked_runner_create(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        output = tmp_path / "feedback"
        output.mkdir()
        finding = _comment_finding()
        analysis = _analysis()
        fb = output / generate_output_filename(finding)
        fb.write_text(generate_markdown_content(finding, analysis), encoding="utf-8")

        def runner(prompt: str, model: str) -> str:
            assert "EXISTING_BACKLOG" in prompt
            return json.dumps(
                {
                    "action": "create",
                    "match_path": None,
                    "title": "Missing HTTP timeout",
                    "problem": "No timeout",
                    "recommendation": "Add one",
                    "rationale": "new",
                }
            )

        counts = promote_backlog_findings(
            [(finding, analysis)],
            knowledge_dir=knowledge,
            ocr_file=tmp_path / "ocr.json",
            output_dir=output,
            model="test/model",
            timeout=30,
            runner=runner,
        )
        assert counts.created == 1
        catalog = load_backlog_catalog(knowledge)
        assert len(catalog) == 1
        assert "timeout" in catalog[0].title.lower() or "Timeout" in catalog[0].title

    def test_create_when_slug_exists_updates(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        from deep_architect.backlog_store import Occurrence

        create_backlog_entry(
            knowledge,
            title="Duplicate Slug Case",
            problem="first",
            recommendation="r",
            source_report="a.json",
            occurrence=Occurrence(
                feedback_rel="feedback/old.md",
                file_path="a.py",
                lines=None,
                date="2026-01-01",
                analysis_preview="first",
            ),
        )
        output = tmp_path / "feedback"
        output.mkdir()
        finding = _comment_finding()
        analysis = _analysis()
        fb = output / generate_output_filename(finding)
        fb.write_text("x", encoding="utf-8")

        from deep_architect.backlog_dedup import DedupDecision

        result = apply_dedup_decision(
            DedupDecision(
                action="create",
                match_path=None,
                title="Duplicate slug case",  # same slug
                problem="second",
                recommendation="r2",
                rationale="create but exists",
            ),
            finding=finding,
            analysis=analysis,
            knowledge_dir=knowledge,
            ocr_file=tmp_path / "o.json",
            output_dir=output,
            feedback_path=fb,
        )
        assert result.action == BacklogAction.UPDATED
        assert len(list((knowledge / "backlog").glob("*.md"))) == 1


class TestBuildPrompt:
    def test_includes_catalog(self) -> None:
        catalog = [
            CatalogEntry(
                path="knowledge/backlog/a.md",
                title="A",
                kind="backlog",
            ),
            CatalogEntry(
                path="knowledge/tickets/PROJ-0001.md",
                title="B",
                kind="ticket",
                ticket_id="PROJ-0001",
                status="spec",
            ),
        ]
        prompt = build_dedup_prompt(_comment_finding(), _analysis(), catalog)
        assert "knowledge/backlog/a.md" in prompt
        assert "PROJ-0001" in prompt
        assert "Missing timeout" in prompt
