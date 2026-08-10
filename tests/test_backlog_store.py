"""Unit tests for deep_architect.backlog_store."""
from __future__ import annotations

from pathlib import Path

from deep_architect.backlog_store import (
    BacklogAction,
    CatalogEntry,
    Occurrence,
    PromotionCounts,
    create_backlog_entry,
    default_knowledge_dir,
    deterministic_title_from_finding,
    format_catalog_heads,
    load_backlog_catalog,
    load_entry_body,
    load_full_catalog,
    load_ticket_catalog,
    rank_catalog_for_finding,
    slugify_title,
    stamp_feedback_disposition,
    update_backlog_entry,
)


class TestSlugifyTitle:
    def test_basic(self) -> None:
        assert slugify_title("Hello World") == "hello-world"

    def test_punctuation(self) -> None:
        assert slugify_title("Foo, Bar & Baz!") == "foo-bar-baz"

    def test_collapse_hyphens(self) -> None:
        assert slugify_title("a---b  c") == "a-b-c"

    def test_truncate_at_word(self) -> None:
        long = "one two three four five six seven eight nine ten eleven twelve"
        slug = slugify_title(long, max_len=30)
        assert len(slug) <= 30
        assert " " not in slug

    def test_empty(self) -> None:
        assert slugify_title("   ") == "untitled"
        assert slugify_title("!!!") == "untitled"


class TestCatalog:
    def test_empty_dirs(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        assert load_backlog_catalog(knowledge) == []
        assert load_ticket_catalog(knowledge) == []
        assert load_full_catalog(knowledge) == []

    def test_loads_backlog_and_tickets(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "backlog").mkdir(parents=True)
        (knowledge / "tickets").mkdir(parents=True)
        (knowledge / "backlog" / "foo.md").write_text(
            "---\ntitle: Foo Issue\ncreated_at: 2026-01-01T00:00:00Z\n---\n\n# Foo\n",
            encoding="utf-8",
        )
        (knowledge / "tickets" / "PROJ-0001.md").write_text(
            "---\nid: PROJ-0001\ntitle: Real Ticket\nstatus: backlog\n---\n\n# Real\n",
            encoding="utf-8",
        )
        catalog = load_full_catalog(knowledge)
        assert len(catalog) == 2
        titles = {e.title for e in catalog}
        assert "Foo Issue" in titles
        assert "Real Ticket" in titles
        kinds = {e.kind for e in catalog}
        assert kinds == {"backlog", "ticket"}

    def test_occurrence_files_from_multi_file_backlog(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "backlog").mkdir(parents=True)
        (knowledge / "backlog" / "type-hints.md").write_text(
            "---\n"
            "title: Public API type hints\n"
            "created_at: 2026-01-01T00:00:00Z\n"
            "occurrences:\n"
            "  - feedback: feedback/a.md\n"
            "    file: src/api.py\n"
            "    lines: 10-20\n"
            "    date: 2026-08-01\n"
            "  - feedback: feedback/b.md\n"
            "    file: src/helpers.py\n"
            "    date: 2026-08-02\n"
            "  - feedback: feedback/c.md\n"
            "    file: src/api.py\n"
            "    date: 2026-08-03\n"
            "---\n\n"
            "# Public API type hints\n\n"
            "## Problem\n\n"
            "Missing type annotations on public functions.\n",
            encoding="utf-8",
        )
        catalog = load_backlog_catalog(knowledge)
        assert len(catalog) == 1
        entry = catalog[0]
        assert entry.title == "Public API type hints"
        assert entry.occurrence_files == ("src/api.py", "src/helpers.py")

    def test_tickets_have_empty_occurrence_files(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "tickets").mkdir(parents=True)
        (knowledge / "tickets" / "PROJ-0001.md").write_text(
            "---\nid: PROJ-0001\ntitle: Real Ticket\nstatus: backlog\n---\n\n# Real\n",
            encoding="utf-8",
        )
        catalog = load_ticket_catalog(knowledge)
        assert len(catalog) == 1
        assert catalog[0].occurrence_files == ()

    def test_malformed_occurrences_yield_empty(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "backlog").mkdir(parents=True)
        (knowledge / "backlog" / "broken.md").write_text(
            "---\ntitle: Broken\noccurrences:\n  not-a-list\n---\n\n# Broken\n",
            encoding="utf-8",
        )
        catalog = load_backlog_catalog(knowledge)
        assert len(catalog) == 1
        assert catalog[0].occurrence_files == ()


class TestRankAndFormat:
    def test_rank_boosts_same_file_keeps_all(self) -> None:
        catalog = [
            CatalogEntry(
                path="knowledge/backlog/a.md",
                title="Unrelated theme",
                kind="backlog",
                occurrence_files=("other/file.py",),
            ),
            CatalogEntry(
                path="knowledge/backlog/b.md",
                title="Same-file theme",
                kind="backlog",
                occurrence_files=("src/api.py", "src/other.py"),
            ),
            CatalogEntry(
                path="knowledge/tickets/PROJ-0001.md",
                title="Ticket no files",
                kind="ticket",
                ticket_id="PROJ-0001",
                status="backlog",
            ),
        ]
        ranked = rank_catalog_for_finding(catalog, "src/api.py")
        assert len(ranked) == 3
        assert ranked[0].title == "Same-file theme"
        titles = [e.title for e in ranked]
        assert "Unrelated theme" in titles
        assert "Ticket no files" in titles

    def test_rank_empty_catalog(self) -> None:
        assert rank_catalog_for_finding([], "src/x.py") == []

    def test_format_catalog_heads_titles_not_bodies(self) -> None:
        catalog = [
            CatalogEntry(
                path="knowledge/backlog/type-hints.md",
                title="Public API type hints",
                kind="backlog",
                occurrence_files=("src/api.py",),
            ),
            CatalogEntry(
                path="knowledge/tickets/PROJ-0001.md",
                title="Real Ticket",
                kind="ticket",
                ticket_id="PROJ-0001",
                status="backlog",
            ),
        ]
        heads = format_catalog_heads(catalog)
        assert "Public API type hints" in heads
        assert "Real Ticket" in heads
        assert "knowledge/backlog/type-hints.md" in heads
        assert "src/api.py" in heads
        assert "id=PROJ-0001" in heads
        # Must not include full Problem bodies (we only pass compact fields)
        assert "## Problem" not in heads
        assert "Missing type annotations" not in heads

    def test_format_catalog_heads_empty(self) -> None:
        assert format_catalog_heads([]) == ""

    def test_load_entry_body(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "backlog").mkdir(parents=True)
        body = (
            "---\ntitle: Foo\n---\n\n# Foo\n\n## Problem\n\nFull body text here.\n"
        )
        (knowledge / "backlog" / "foo.md").write_text(body, encoding="utf-8")
        loaded = load_entry_body(knowledge, "knowledge/backlog/foo.md")
        assert loaded is not None
        assert "Full body text here." in loaded
        assert "## Problem" in loaded

    def test_load_entry_body_missing(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        knowledge.mkdir()
        assert load_entry_body(knowledge, "knowledge/backlog/missing.md") is None


class TestCreateUpdate:
    def _occ(self, feedback: str = "feedback/a-0.md") -> Occurrence:
        return Occurrence(
            feedback_rel=feedback,
            file_path="src/x.py",
            lines="1-5",
            date="2026-08-09",
            analysis_preview="needs later work",
        )

    def test_create_entry(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        path = create_backlog_entry(
            knowledge,
            title="Missing Metrics",
            problem="No metrics exported.",
            recommendation="Add Prometheus counters.",
            source_report="code-review.json",
            occurrence=self._occ(),
        )
        assert path.is_file()
        assert path.name == "missing-metrics.md"
        text = path.read_text(encoding="utf-8")
        assert "title: Missing Metrics" in text
        assert "source: review-analyzer" in text
        assert "## Problem" in text
        assert "No metrics exported." in text
        assert "## Occurrences" in text
        assert "feedback/a-0.md" in text

    def test_update_appends_occurrence(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        path = create_backlog_entry(
            knowledge,
            title="Observability Gaps",
            problem="Limited observability.",
            recommendation="Add tracing.",
            source_report="ocr.json",
            occurrence=self._occ("feedback/first.md"),
        )
        first = path.read_text(encoding="utf-8")
        update_backlog_entry(
            path,
            occurrence=self._occ("feedback/second.md"),
        )
        second = path.read_text(encoding="utf-8")
        assert "feedback/first.md" in second
        assert "feedback/second.md" in second
        assert second.count("## Occurrences") == 1
        # updated_at should still be present
        assert "updated_at:" in second
        assert len(second) > len(first)

    def test_slug_collision_gets_suffix(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        p1 = create_backlog_entry(
            knowledge,
            title="Same Title",
            problem="p1",
            recommendation="r1",
            source_report="a.json",
            occurrence=self._occ("feedback/1.md"),
        )
        # Force collision: create with same title would use unique path
        # create_backlog_entry uses unique_backlog_path
        p2 = create_backlog_entry(
            knowledge,
            title="Same Title",
            problem="p2",
            recommendation="r2",
            source_report="a.json",
            occurrence=self._occ("feedback/2.md"),
        )
        assert p1.name == "same-title.md"
        assert p2.name == "same-title-2.md"


class TestStampDisposition:
    def test_stamp_creates_section(self, tmp_path: Path) -> None:
        fb = tmp_path / "finding.md"
        fb.write_text("# OCR\n\n**Verdict**: BACKLOG\n", encoding="utf-8")
        stamp_feedback_disposition(
            fb,
            action=BacklogAction.CREATED,
            target="knowledge/backlog/foo.md",
            rationale="new item",
        )
        text = fb.read_text(encoding="utf-8")
        assert "## Backlog disposition" in text
        assert "created" in text
        assert "knowledge/backlog/foo.md" in text

    def test_stamp_replaces_prior(self, tmp_path: Path) -> None:
        fb = tmp_path / "finding.md"
        fb.write_text(
            "# OCR\n\n## Backlog disposition\n\n- **Action**: created\n",
            encoding="utf-8",
        )
        stamp_feedback_disposition(
            fb,
            action=BacklogAction.UPDATED,
            target="knowledge/backlog/foo.md",
            rationale="again",
        )
        text = fb.read_text(encoding="utf-8")
        assert text.count("## Backlog disposition") == 1
        assert "updated" in text


class TestHelpers:
    def test_default_knowledge_dir(self, tmp_path: Path) -> None:
        assert default_knowledge_dir(tmp_path) == tmp_path / "knowledge"

    def test_deterministic_title_comment(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "Missing error handling on network call\nmore",
        }
        title = deterministic_title_from_finding(finding, "analysis")
        assert "Missing error handling" in title

    def test_promotion_counts(self) -> None:
        c = PromotionCounts()
        from deep_architect.backlog_store import PromotionResult

        c.record(PromotionResult(action=BacklogAction.CREATED))
        c.record(PromotionResult(action=BacklogAction.UPDATED))
        c.record(PromotionResult(action=BacklogAction.LINKED_TO_TICKET))
        d = c.as_dict()
        assert d["created"] == 1
        assert d["updated"] == 1
        assert d["linked_to_ticket"] == 1
