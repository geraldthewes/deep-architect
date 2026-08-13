"""Unit tests for deep_architect.review_analyzer."""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import deep_architect.review_analyzer as review_analyzer_mod
from deep_architect.backlog_store import CatalogEntry
from deep_architect.review_analyzer import (
    DEFAULT_DEDUP_SIMILARITY,
    AnalysisResult,
    CircuitBreaker,
    PlainReporter,
    ProgressEvent,
    RunMeta,
    Verdict,
    _emit_summary_outputs,
    _finding_lines,
    _finding_path,
    _finding_severity,
    _normalize_match_path,
    _parse_opencode_json,
    _promote_backlog_if_needed,
    _severity_stats_key,
    _tally_severity_counts,
    call_opencode_analysis,
    construct_analysis_prompt,
    content_similarity,
    default_opencode_timeout,
    expand_prior_feedback_dirs,
    extract_findings,
    filter_findings_by_path,
    finding_similarity_text,
    format_prior_feedback_index,
    generate_index_report,
    generate_index_report_from_output_dir,
    generate_markdown_content,
    generate_output_filename,
    generate_summary_report,
    get_filepath_hash,
    group_near_duplicate_findings,
    is_timeout_report,
    load_ocr_json,
    load_prior_feedback_index,
    parse_severity_from_markdown,
    process_findings_concurrently,
    request_shutdown,
    resolve_opencode_timeout,
    select_catalog_bodies_to_expand,
    select_timeout_findings_for_retry,
    should_use_tui,
    tally_output_dir_severities,
    tally_output_dir_verdicts,
)

# ---------------------------------------------------------------------------
# load_ocr_json
# ---------------------------------------------------------------------------


class TestLoadOcrJson:

    def test_valid_json(self, tmp_path: Path) -> None:
        data = {"status": "success", "comments": [], "warnings": []}
        f = tmp_path / "test.json"
        f.write_text(json.dumps(data))
        assert load_ocr_json(f) == data

    def test_file_not_found(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            load_ocr_json(Path("/tmp/__does_not_exist__.json"))
        assert exc_info.value.code == 1

    def test_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("{not valid json")
        with pytest.raises(SystemExit) as exc_info:
            load_ocr_json(f)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# extract_findings
# ---------------------------------------------------------------------------


class TestExtractFindings:

    def test_empty(self) -> None:
        assert extract_findings({}) == []
        assert extract_findings({"comments": [], "warnings": []}) == []

    def test_comments_only(self) -> None:
        data = {
            "comments": [
                {"path": "a.py", "content": "c1", "start_line": 1, "end_line": 1},
                {"path": "b.py", "content": "c2", "start_line": 5, "end_line": 5},
            ],
        }
        findings = extract_findings(data)
        assert len(findings) == 2
        assert findings[0]["type"] == "comment"
        assert findings[0]["index"] == 0
        assert findings[1]["path"] == "b.py"
        assert findings[1]["index"] == 1

    def test_warnings_only(self) -> None:
        data = {
            "warnings": [
                {"file": "w.py", "message": "w1", "warning_type": "timeout"},
            ],
        }
        findings = extract_findings(data)
        assert len(findings) == 1
        assert findings[0]["type"] == "warning"
        assert findings[0]["file"] == "w.py"

    def test_both(self) -> None:
        data = {
            "comments": [
                {"path": "a.py", "content": "x", "start_line": 1, "end_line": 1},
            ],
            "warnings": [
                {"file": "b.py", "message": "y", "warning_type": "err"},
            ],
        }
        findings = extract_findings(data)
        assert len(findings) == 2
        assert findings[0]["type"] == "comment"
        assert findings[1]["type"] == "warning"


# ---------------------------------------------------------------------------
# filter_findings_by_path
# ---------------------------------------------------------------------------


class TestFilterFindingsByPath:

    def _findings(self) -> list[Any]:
        return [
            {
                "type": "comment",
                "path": "src/main.py",
                "start_line": 1,
                "end_line": 1,
                "content": "x",
            },
            {
                "type": "comment",
                "path": "tests/test_main.py",
                "start_line": 1,
                "end_line": 1,
                "content": "x",
            },
            {
                "type": "comment",
                "path": "docs/readme.md",
                "start_line": 1,
                "end_line": 1,
                "content": "x",
            },
            {
                "type": "warning",
                "file": ".agents/config.toml",
                "message": "x",
                "warning_type": "warn",
            },
        ]

    def test_no_patterns_returns_all(self) -> None:
        assert len(filter_findings_by_path(self._findings())) == 4

    def test_include_pattern(self) -> None:
        result = filter_findings_by_path(
            self._findings(), include_patterns=["src/**"]
        )
        assert len(result) == 1
        assert result[0]["path"] == "src/main.py"

    def test_exclude_pattern(self) -> None:
        result = filter_findings_by_path(
            self._findings(), exclude_patterns=["tests/**", "docs/**"]
        )
        assert len(result) == 2

    def test_include_and_exclude(self) -> None:
        result = filter_findings_by_path(
            self._findings(),
            include_patterns=["**/*.py"],
            exclude_patterns=["tests/**"],
        )
        assert len(result) == 1
        assert result[0]["path"] == "src/main.py"

    def test_no_path_included(self) -> None:
        findings = [
            {"type": "comment", "content": "x", "start_line": 1, "end_line": 1}
        ]
        result = filter_findings_by_path(findings, include_patterns=["**"])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# get_filepath_hash
# ---------------------------------------------------------------------------


class TestGetFilepathHash:

    def test_consistent(self) -> None:
        h = get_filepath_hash("src/main.py")
        assert h == get_filepath_hash("src/main.py")

    def test_different(self) -> None:
        h1 = get_filepath_hash("a.py")
        h2 = get_filepath_hash("b.py")
        assert h1 != h2

    def test_format(self) -> None:
        h = get_filepath_hash("test/path.py")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:

    def test_initial_state(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_success_resets(self) -> None:
        cb = CircuitBreaker(failure_threshold=2)
        cb._on_failure()
        cb._on_failure()
        assert cb.state == "OPEN"
        cb._on_success()
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb._on_failure()
        assert cb.state == "OPEN"

    def test_call_raises_when_open(self) -> None:
        cb = CircuitBreaker(failure_threshold=1)
        cb._on_failure()
        with pytest.raises(RuntimeError, match="OPEN"):
            cb.call(lambda: 42)

    def test_call_succeeds_when_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.call(lambda: 42) == 42

    def test_recovery_after_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb._on_failure()
        assert cb.state == "OPEN"
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == "CLOSED"


# ---------------------------------------------------------------------------
# construct_analysis_prompt
# ---------------------------------------------------------------------------


class TestConstructAnalysisPrompt:

    def test_comment_prompt(self) -> None:
        finding = {
            "type": "comment",
            "path": "src/foo.py",
            "content": "Rename variable",
            "existing_code": "x = 1",
            "suggestion_code": "count = 1",
            "start_line": 10,
            "end_line": 10,
        }
        prompt = construct_analysis_prompt(finding)
        assert "src/foo.py" in prompt
        assert "**Lines**: 10-10" in prompt
        assert "x = 1" in prompt
        assert "count = 1" in prompt
        assert "Rename variable" in prompt
        # Empty catalog: no catalog section / multi-file rule block
        assert "Existing knowledge catalog" not in prompt
        assert "each file still needs a fix" not in prompt

    def test_warning_prompt(self) -> None:
        finding = {
            "type": "warning",
            "file": "src/bar.py",
            "message": "Context deadline exceeded",
            "warning_type": "timeout",
        }
        prompt = construct_analysis_prompt(finding)
        assert "src/bar.py" in prompt
        assert "Context deadline exceeded" in prompt

    def test_with_catalog_heads_includes_rules(self) -> None:
        finding = {
            "type": "comment",
            "path": "src/api.py",
            "content": "Missing type hints on public API",
            "existing_code": "def f(x):",
            "suggestion_code": "def f(x: int) -> None:",
            "start_line": 1,
            "end_line": 1,
        }
        heads = (
            "- path=knowledge/backlog/type-hints.md | title=Public API type hints "
            "| kind=backlog | files=[src/api.py]"
        )
        prompt = construct_analysis_prompt(finding, catalog_heads=heads)
        assert "Existing knowledge catalog" in prompt
        assert "Public API type hints" in prompt
        assert "knowledge/backlog/type-hints.md" in prompt
        assert "each file still needs a fix" in prompt
        assert "match_path" in prompt
        assert "VALID|REJECTED|BACKLOG" in prompt

    def test_empty_catalog_heads_omits_section(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "x",
            "start_line": 1,
            "end_line": 1,
        }
        prompt = construct_analysis_prompt(finding, catalog_heads="")
        assert "Existing knowledge catalog" not in prompt
        baseline = construct_analysis_prompt(finding)
        # Same structural shape (no catalog rules)
        assert "Classification rules" not in prompt
        assert "Classification rules" not in baseline

    def test_with_prior_feedback_section(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "x",
            "start_line": 1,
            "end_line": 1,
        }
        prompt = construct_analysis_prompt(
            finding,
            prior_feedback_index="- file=a.py verdict=BACKLOG preview=type hints",
        )
        assert "Prior feedback" in prompt
        assert "verdict=BACKLOG" in prompt


class TestPriorFeedbackIndex:

    def _write_finding(
        self,
        directory: Path,
        name: str,
        *,
        verdict: str,
        file_path: str = "src/api.py",
        comment: str = "Missing type hints on public helpers",
        disposition: str | None = None,
        analysis: str = "Deferred style campaign",
    ) -> Path:
        text = (
            "# OCR Review Analysis\n\n"
            f"- **File**: {file_path}\n"
            "- **Lines**: 1-5\n"
            "- **Type**: Comment\n"
            "- **Existing Code**:\n```\npass\n```\n"
            f"- **Review Comment**: {comment}\n\n"
            "## LLM Analysis\n\n"
            f"**Verdict**: {verdict}\n\n"
            "**Analysis**:\n\n"
            f"{analysis}\n"
        )
        if disposition:
            text += (
                "\n## Backlog disposition\n\n"
                f"- **Action**: {disposition}\n"
                "- **Target**: `knowledge/backlog/type-hints.md`\n"
            )
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_load_mixed_verdicts_excludes_timeout(
        self, tmp_path: Path
    ) -> None:
        fb = tmp_path / "feedback-r1"
        fb.mkdir()
        self._write_finding(fb, "a-0.md", verdict="BACKLOG", disposition="created")
        self._write_finding(
            fb,
            "b-1.md",
            verdict="REJECTED",
            comment="False positive on lint",
            analysis="Noise",
        )
        self._write_finding(
            fb,
            "c-2.md",
            verdict="VALID",
            comment="Null check missing",
            analysis="Real bug",
        )
        # TIMEOUT noise
        (fb / "d-3.md").write_text(
            "# OCR Review Analysis\n\n"
            "- **File**: src/x.py\n"
            "- **Review Comment**: timed out\n\n"
            "## LLM Analysis\n\n"
            "**Verdict**: TIMEOUT\n\n"
            "**Analysis**:\n\n"
            "opencode execution timed out (>300s)\n",
            encoding="utf-8",
        )
        # Non-finding files skipped
        (fb / "SUMMARY.md").write_text("# Summary\n", encoding="utf-8")

        items = load_prior_feedback_index([fb])
        verdicts = {i.verdict for i in items}
        assert "TIMEOUT" not in verdicts
        assert "BACKLOG" in verdicts
        assert "REJECTED" in verdicts
        assert "VALID" in verdicts
        assert len(items) == 3
        backlog = next(i for i in items if i.verdict == "BACKLOG")
        assert backlog.disposition == "created"
        assert "type hints" in backlog.comment_preview.lower()

    def test_format_is_compact(self, tmp_path: Path) -> None:
        fb = tmp_path / "fb"
        fb.mkdir()
        long_comment = "word " * 80
        self._write_finding(
            fb, "a-0.md", verdict="BACKLOG", comment=long_comment
        )
        items = load_prior_feedback_index([fb])
        formatted = format_prior_feedback_index(items)
        assert "verdict=BACKLOG" in formatted
        assert "prefer BACKLOG" in formatted
        # Preview truncated
        assert len(items[0].comment_preview) <= 120
        assert "…" in items[0].comment_preview or len(long_comment) <= 120

    def test_missing_dir_no_crash(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        items = load_prior_feedback_index([missing])
        assert items == []

    def test_prompt_includes_prior_only_when_non_empty(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "x",
            "start_line": 1,
            "end_line": 1,
        }
        empty = construct_analysis_prompt(finding, prior_feedback_index="")
        assert "Prior feedback" not in empty
        filled = construct_analysis_prompt(
            finding,
            prior_feedback_index=format_prior_feedback_index([]),  # empty → ""
        )
        assert "Prior feedback" not in filled
        # Non-empty index string
        index = (
            "Prior triage…\n\n"
            "- file=a.py verdict=BACKLOG preview=type hints src=fb/a-0.md"
        )
        with_prior = construct_analysis_prompt(
            finding, prior_feedback_index=index
        )
        assert "Prior feedback" in with_prior
        assert "verdict=BACKLOG" in with_prior


class TestSelectCatalogBodies:

    def test_selects_overlapping_titles(self) -> None:
        catalog = [
            CatalogEntry(
                path="knowledge/backlog/type-hints.md",
                title="Public API type hints campaign",
                kind="backlog",
                occurrence_files=("src/api.py",),
            ),
            CatalogEntry(
                path="knowledge/backlog/metrics.md",
                title="Missing Prometheus metrics",
                kind="backlog",
            ),
        ]
        finding = {
            "type": "comment",
            "path": "src/api.py",
            "content": "Add type hints to public API helpers",
            "index": 0,
        }
        selected = select_catalog_bodies_to_expand(
            catalog, finding, max_bodies=3, title_overlap_threshold=0.15
        )
        assert any(e.path.endswith("type-hints.md") for e in selected)

    def test_empty_catalog(self) -> None:
        finding = {"type": "comment", "path": "a.py", "content": "x", "index": 0}
        assert select_catalog_bodies_to_expand([], finding) == []


class TestNormalizeMatchPath:

    def test_valid_path(self) -> None:
        paths = {"knowledge/backlog/foo.md", "knowledge/tickets/PROJ-0001.md"}
        assert (
            _normalize_match_path("knowledge/backlog/foo.md", catalog_path_set=paths)
            == "knowledge/backlog/foo.md"
        )

    def test_invalid_path_dropped(self) -> None:
        paths = {"knowledge/backlog/foo.md"}
        assert (
            _normalize_match_path("knowledge/backlog/nope.md", catalog_path_set=paths)
            is None
        )

    def test_missing_field_ok(self) -> None:
        assert _normalize_match_path(None, catalog_path_set={"a.md"}) is None
        assert _normalize_match_path("null", catalog_path_set={"a.md"}) is None

    def test_no_catalog_drops_match(self) -> None:
        assert (
            _normalize_match_path("knowledge/backlog/foo.md", catalog_path_set=None)
            is None
        )


# ---------------------------------------------------------------------------
# _parse_opencode_json
# ---------------------------------------------------------------------------


class TestParseOpendencodeJson:

    def test_string_content_with_json(self) -> None:
        raw = json.dumps(
            {"content": '{"verdict": "valid", "analysis": "real issue"}'}
        )
        result = _parse_opencode_json(raw)
        assert result.verdict == Verdict.VALID
        assert "real issue" in result.analysis
        assert result.match_path is None

    def test_match_path_validated(self) -> None:
        payload = {
            "verdict": "backlog",
            "analysis": "deferred theme",
            "match_path": "knowledge/backlog/type-hints.md",
        }
        raw = json.dumps({"content": json.dumps(payload)})
        paths = {"knowledge/backlog/type-hints.md"}
        result = _parse_opencode_json(raw, catalog_path_set=paths)
        assert result.verdict == Verdict.BACKLOG
        assert result.match_path == "knowledge/backlog/type-hints.md"

    def test_invalid_match_path_dropped(self) -> None:
        payload = {
            "verdict": "backlog",
            "analysis": "x",
            "match_path": "knowledge/backlog/missing.md",
        }
        raw = json.dumps({"content": json.dumps(payload)})
        result = _parse_opencode_json(
            raw, catalog_path_set={"knowledge/backlog/other.md"}
        )
        assert result.match_path is None

    def test_list_content_blocks(self) -> None:
        verdict_json = '{"verdict":"rejected","analysis":"false positive"}'
        raw = json.dumps({"content": [{"type": "text", "text": verdict_json}]})
        result = _parse_opencode_json(raw)
        assert result.verdict == Verdict.REJECTED

    def test_no_content_field(self) -> None:
        raw = json.dumps({"other": "data"})
        result = _parse_opencode_json(raw)
        assert result.verdict == Verdict.BACKLOG

    def test_empty_input(self) -> None:
        result = _parse_opencode_json("")
        assert result.verdict == Verdict.BACKLOG

    def test_invalid_verdict_defaults_to_backlog(self) -> None:
        raw = json.dumps(
            {"content": '{"verdict": "unknown", "analysis": "test"}'}
        )
        result = _parse_opencode_json(raw)
        assert result.verdict == Verdict.BACKLOG

    def test_streaming_events(self) -> None:
        lines = [
            json.dumps({"content": '{"ve'}),
            json.dumps({"content": 'rdict": "valid", "analysis": "yes"}'}),
        ]
        result = _parse_opencode_json("\n".join(lines))
        assert result.verdict == Verdict.VALID


# ---------------------------------------------------------------------------
# call_opencode_analysis  (mocked)
# ---------------------------------------------------------------------------


class TestCallOpendencodeAnalysis:

    @patch("deep_architect.review_analyzer.subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"content": '{"verdict":"valid","analysis":"ok"}'}
            ),
            stderr="",
        )
        result = call_opencode_analysis("prompt", "model")
        assert result.verdict == Verdict.VALID
        assert result.retry_count == 0
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["timeout"] == 300

    @patch("deep_architect.review_analyzer.subprocess.run")
    def test_custom_timeout_passed(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"content": '{"verdict":"valid","analysis":"ok"}'}
            ),
            stderr="",
        )
        call_opencode_analysis("prompt", "model", timeout=45, timeout_retries=0)
        assert mock_run.call_args.kwargs["timeout"] == 45

    @patch("deep_architect.review_analyzer.subprocess.run")
    def test_failure_returncode(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="model not found"
        )
        result = call_opencode_analysis("prompt", "bad-model")
        assert result.verdict == Verdict.BACKLOG
        assert "model not found" in result.analysis

    @patch("deep_architect.review_analyzer.subprocess.run")
    def test_timeout_retries_once_then_timeout_verdict(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="opencode", timeout=30
        )
        result = call_opencode_analysis(
            "prompt", "model", timeout=30, timeout_retries=1
        )
        assert result.verdict == Verdict.TIMEOUT
        assert "timed out" in result.analysis
        assert "2 attempts" in result.analysis
        assert result.retry_count == 1
        assert mock_run.call_count == 2

    @patch("deep_architect.review_analyzer.subprocess.run")
    def test_timeout_no_retry(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="opencode", timeout=10
        )
        result = call_opencode_analysis(
            "prompt", "model", timeout=10, timeout_retries=0
        )
        assert result.verdict == Verdict.TIMEOUT
        assert ">10s" in result.analysis
        assert result.retry_count == 0
        assert mock_run.call_count == 1

    @patch("deep_architect.review_analyzer.subprocess.run")
    def test_timeout_then_success_on_retry(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="opencode", timeout=20),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"content": '{"verdict":"valid","analysis":"recovered"}'}
                ),
                stderr="",
            ),
        ]
        result = call_opencode_analysis(
            "prompt", "model", timeout=20, timeout_retries=1
        )
        assert result.verdict == Verdict.VALID
        assert "recovered" in result.analysis
        assert result.retry_count == 1
        assert mock_run.call_count == 2

    def test_binary_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import deep_architect.review_analyzer as ra

        monkeypatch.setattr(ra, "__OPENCODE_BIN", "/nonexistent/opencode")
        result = call_opencode_analysis("prompt", "model")
        assert result.verdict == Verdict.BACKLOG
        assert "not found" in result.analysis


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class TestVerdict:

    def test_values(self) -> None:
        assert Verdict.VALID.value == "valid"
        assert Verdict.REJECTED.value == "rejected"
        assert Verdict.BACKLOG.value == "backlog"
        assert Verdict.TIMEOUT.value == "timeout"
        assert Verdict.DUPLICATE.value == "duplicate"

    def test_from_string(self) -> None:
        assert Verdict("valid") == Verdict.VALID
        assert Verdict("timeout") == Verdict.TIMEOUT
        assert Verdict("duplicate") == Verdict.DUPLICATE

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            Verdict("not_a_verdict")


# ---------------------------------------------------------------------------
# Intra-OCR near-duplicate collapse
# ---------------------------------------------------------------------------


def _comment(
    path: str,
    content: str,
    index: int = 0,
    *,
    severity: str | None = None,
    start_line: int = 1,
    end_line: int = 1,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "type": "comment",
        "path": path,
        "content": content,
        "start_line": start_line,
        "end_line": end_line,
        "index": index,
    }
    if severity is not None:
        finding["severity"] = severity
    return finding


class TestContentSimilarity:

    def test_identical(self) -> None:
        assert content_similarity("missing type hints", "missing type hints") == 1.0

    def test_empty_both(self) -> None:
        assert content_similarity("", "") == 1.0

    def test_empty_one(self) -> None:
        assert content_similarity("hello world", "") == 0.0

    def test_partial_overlap(self) -> None:
        sim = content_similarity(
            "add type hints to public API functions",
            "add type hints on public helpers",
        )
        assert 0.0 < sim < 1.0

    def test_disjoint(self) -> None:
        assert content_similarity("alpha beta", "gamma delta") == 0.0


class TestGroupNearDuplicates:

    def test_same_path_near_identical_collapses(self) -> None:
        findings = [
            _comment("conftest.py", "Iterator consumed twice in fixture setup", 0),
            _comment(
                "conftest.py",
                "Iterator consumed twice during fixture setup",
                1,
            ),
        ]
        groups = group_near_duplicate_findings(findings, threshold=0.5)
        multi = [g for g in groups if g.duplicate_indices]
        assert len(multi) == 1
        assert multi[0].canonical_index == 0
        assert multi[0].duplicate_indices == (1,)

    def test_same_content_different_paths_no_collapse(self) -> None:
        text = "Missing null check on response object"
        findings = [
            _comment("a.py", text, 0),
            _comment("b.py", text, 1),
        ]
        groups = group_near_duplicate_findings(findings)
        assert len(groups) == 2
        assert all(g.duplicate_indices == () for g in groups)

    def test_empty_and_single(self) -> None:
        assert group_near_duplicate_findings([]) == []
        groups = group_near_duplicate_findings(
            [_comment("a.py", "only one", 0)]
        )
        assert len(groups) == 1
        assert groups[0].canonical_index == 0
        assert groups[0].duplicate_indices == ()

    def test_threshold_boundary(self) -> None:
        # Share one token of two → Jaccard 1/3 ≈ 0.333
        a = "unique_token_aaa shared"
        b = "unique_token_bbb shared"
        sim = content_similarity(a, b)
        assert abs(sim - 1 / 3) < 1e-9
        findings = [_comment("x.py", a, 0), _comment("x.py", b, 1)]
        below = group_near_duplicate_findings(findings, threshold=0.5)
        assert all(g.duplicate_indices == () for g in below)
        above = group_near_duplicate_findings(findings, threshold=0.3)
        multi = [g for g in above if g.duplicate_indices]
        assert len(multi) == 1

    def test_canonical_prefers_higher_severity(self) -> None:
        findings = [
            _comment("x.py", "same issue about missing validation", 0, severity="low"),
            _comment("x.py", "same issue about missing validation", 1, severity="high"),
        ]
        groups = group_near_duplicate_findings(findings, threshold=0.8)
        multi = [g for g in groups if g.duplicate_indices]
        assert len(multi) == 1
        assert multi[0].canonical_index == 1
        assert multi[0].duplicate_indices == (0,)

    def test_default_threshold_constant(self) -> None:
        assert DEFAULT_DEDUP_SIMILARITY == 0.85

    def test_finding_similarity_text_warning(self) -> None:
        f = {"type": "warning", "file": "a.py", "message": "warn msg", "index": 0}
        assert finding_similarity_text(f) == "warn msg"


class TestProcessFindingsDedup:

    def test_only_canonicals_invoke_analysis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        findings = [
            _comment("conftest.py", "one-time iterator exhausted early", 0),
            _comment("conftest.py", "one-time iterator exhausted early!", 1),
            _comment("other.py", "unrelated null check missing", 2),
        ]
        calls: list[int] = []

        def fake_analyze(
            finding: dict[str, Any],
            model: str,
            breaker: Any,
            **kwargs: Any,
        ) -> AnalysisResult:
            calls.append(finding["index"])
            return AnalysisResult(Verdict.VALID, f"ok-{finding['index']}", "")

        monkeypatch.setattr(
            "deep_architect.review_analyzer.analyze_finding", fake_analyze
        )

        results = process_findings_concurrently(
            findings,
            model="m",
            max_workers=2,
            output_dir=tmp_path,
            dedup_threshold=0.5,
        )
        assert len(results) == 3
        # Two canonicals only (one for conftest cluster + other.py)
        assert sorted(calls) == [0, 2]
        verdicts = {
            r[0]["index"]: r[1].verdict for r in results
        }
        assert verdicts[0] == Verdict.VALID
        assert verdicts[1] == Verdict.DUPLICATE
        assert verdicts[2] == Verdict.VALID
        # DUPLICATE report written
        dup_name = generate_output_filename(findings[1])
        dup_md = (tmp_path / dup_name).read_text(encoding="utf-8")
        assert "**Verdict**: DUPLICATE" in dup_md
        assert "Duplicate of" in dup_md


# ---------------------------------------------------------------------------
# default_opencode_timeout
# ---------------------------------------------------------------------------


class TestDefaultOpencodeTimeout:

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REVIEW_ANALYZER_TIMEOUT", raising=False)
        monkeypatch.setattr(
            "deep_architect.review_analyzer._timeout_from_config",
            lambda: None,
        )
        assert default_opencode_timeout() == 300

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIEW_ANALYZER_TIMEOUT", "90")
        assert default_opencode_timeout() == 90

    def test_invalid_env_falls_back_to_config_or_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_ANALYZER_TIMEOUT", "nope")
        monkeypatch.setattr(
            "deep_architect.review_analyzer._timeout_from_config",
            lambda: None,
        )
        assert default_opencode_timeout() == 300

    def test_config_used_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REVIEW_ANALYZER_TIMEOUT", raising=False)
        monkeypatch.setattr(
            "deep_architect.review_analyzer._timeout_from_config",
            lambda: 240,
        )
        assert default_opencode_timeout() == 240

    def test_cli_wins_over_env_and_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_ANALYZER_TIMEOUT", "90")
        monkeypatch.setattr(
            "deep_architect.review_analyzer._timeout_from_config",
            lambda: 240,
        )
        assert resolve_opencode_timeout(45) == 45


# ---------------------------------------------------------------------------
# generate_output_filename
# ---------------------------------------------------------------------------


class TestGenerateOutputFilename:

    def test_comment(self) -> None:
        f = {"type": "comment", "path": "src/main.py", "index": 0}
        name = generate_output_filename(f)
        assert name.endswith("-0.md")
        assert len(name.split("-")[0]) == 8

    def test_warning(self) -> None:
        f = {"type": "warning", "file": "src/main.py", "index": 3}
        name = generate_output_filename(f)
        assert name.endswith("-3.md")


# ---------------------------------------------------------------------------
# generate_markdown_content
# ---------------------------------------------------------------------------


class TestGenerateMarkdownContent:

    def test_comment_finding(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "fix this",
            "start_line": 1,
            "end_line": 1,
            "existing_code": "x = 1",
            "suggestion_code": "y = 2",
        }
        analysis = AnalysisResult(Verdict.VALID, "It's wrong", "")
        md = generate_markdown_content(finding, analysis)
        assert "# OCR Review Analysis" in md
        assert "a.py" in md
        assert "x = 1" in md
        assert "y = 2" in md
        assert "VALID" in md

    def test_warning_finding(self) -> None:
        finding = {
            "type": "warning",
            "file": "b.py",
            "message": "timeout",
            "warning_type": "error",
        }
        analysis = AnalysisResult(Verdict.REJECTED, "False positive", "")
        md = generate_markdown_content(finding, analysis)
        assert "b.py" in md
        assert "timeout" in md
        assert "REJECTED" in md

    def test_duplicate_verdict(self) -> None:
        finding = {
            "type": "comment",
            "path": "conftest.py",
            "content": "dup",
            "start_line": 1,
            "end_line": 1,
            "index": 1,
        }
        analysis = AnalysisResult(
            Verdict.DUPLICATE,
            "Near-duplicate of `abc-0.md`",
            "",
            duration_s=0.0,
        )
        md = generate_markdown_content(
            finding, analysis, duplicate_of="abc-0.md"
        )
        assert "**Verdict**: DUPLICATE" in md
        assert "**Duplicate of**: `abc-0.md`" in md

    def test_catalog_match_stamp(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "type hints",
            "start_line": 1,
            "end_line": 1,
            "index": 0,
        }
        analysis = AnalysisResult(
            Verdict.BACKLOG,
            "Deferred theme",
            "",
            match_path="knowledge/backlog/type-hints.md",
        )
        md = generate_markdown_content(finding, analysis)
        assert "**Catalog match**: `knowledge/backlog/type-hints.md`" in md

    def test_includes_severity_when_present(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "fix this",
            "start_line": 1,
            "end_line": 1,
            "severity": "high",
        }
        analysis = AnalysisResult(Verdict.VALID, "It's wrong", "")
        md = generate_markdown_content(finding, analysis)
        assert "**Severity**: high" in md

    def test_omits_severity_when_absent(self) -> None:
        finding = {
            "type": "comment",
            "path": "a.py",
            "content": "fix this",
            "start_line": 1,
            "end_line": 1,
        }
        analysis = AnalysisResult(Verdict.VALID, "It's wrong", "")
        md = generate_markdown_content(finding, analysis)
        assert "Severity" not in md


# ---------------------------------------------------------------------------
# severity helpers
# ---------------------------------------------------------------------------


class TestSeverityHelpers:

    def test_finding_severity_normalizes(self) -> None:
        assert _finding_severity({"severity": "HIGH"}) == "high"
        assert _finding_severity({"level": "medium"}) == "medium"
        assert _finding_severity({}) == ""

    def test_severity_stats_key_unknown(self) -> None:
        assert _severity_stats_key({}) == "unknown"
        assert _severity_stats_key({"severity": "low"}) == "low"

    def test_parse_severity_from_markdown(self) -> None:
        body = "- **File**: a.py\n- **Severity**: High\n- **Type**: Comment\n"
        assert parse_severity_from_markdown(body) == "high"
        assert parse_severity_from_markdown("# no severity\n") == ""

    def test_tally_severity_counts(self) -> None:
        results = [
            (
                {"type": "comment", "path": "a.py", "severity": "high", "index": 0},
                AnalysisResult(Verdict.VALID, "ok", ""),
            ),
            (
                {"type": "comment", "path": "b.py", "severity": "high", "index": 1},
                AnalysisResult(Verdict.REJECTED, "no", ""),
            ),
            (
                {"type": "comment", "path": "c.py", "index": 2},
                AnalysisResult(Verdict.BACKLOG, "later", ""),
            ),
        ]
        counts = _tally_severity_counts(results)
        assert counts == {"high": 2, "unknown": 1}


# ---------------------------------------------------------------------------
# generate_summary_report
# ---------------------------------------------------------------------------


class TestGenerateSummaryReport:

    def test_basic(self) -> None:
        counts = {"valid": 2, "rejected": 1, "backlog": 0, "timeout": 0}
        report = generate_summary_report(counts, 3, model="standard/coder")
        assert "Coding agent: opencode (standard/coder)" in report
        assert "Total findings processed: 3" in report
        assert "VALID: 2 (66.7%)" in report
        assert "REJECTED: 1 (33.3%)" in report
        assert "BACKLOG: 0 (0.0%)" in report
        assert "TIMEOUT: 0 (0.0%)" in report
        assert "DUPLICATE:" in report
        assert "TIMEOUT is infrastructure" in report
        assert "DUPLICATE is same-path" in report
        assert "Severity is from the OCR input" in report

    def test_zero_total(self) -> None:
        report = generate_summary_report({}, 0, model="standard/coder")
        assert "Coding agent: opencode (standard/coder)" in report
        assert "Total findings processed: 0" in report

    def test_includes_model(self) -> None:
        report = generate_summary_report({}, 0, model="custom/model")
        assert "Coding agent: opencode (custom/model)" in report

    def test_includes_promotion_block(self) -> None:
        report = generate_summary_report(
            {"valid": 0, "rejected": 0, "backlog": 1, "timeout": 0},
            1,
            model="standard/coder",
            promotion={
                "created": 1,
                "updated": 0,
                "linked_to_ticket": 0,
                "skipped": 0,
                "errors": 0,
            },
        )
        assert "Backlog promotion:" in report
        assert "created: 1" in report

    def test_severity_breakdown(self) -> None:
        report = generate_summary_report(
            {"valid": 2, "rejected": 1, "backlog": 0, "timeout": 0},
            3,
            model="standard/coder",
            severity_counts={"high": 1, "low": 2},
        )
        assert "Breakdown by severity:" in report
        assert "HIGH: 1 (33.3%)" in report
        assert "LOW: 2 (66.7%)" in report
        # Higher severity listed first
        high_pos = report.index("HIGH:")
        low_pos = report.index("LOW:")
        assert high_pos < low_pos

    def test_severity_section_omitted_when_empty(self) -> None:
        report = generate_summary_report(
            {"valid": 1}, 1, model="standard/coder", severity_counts={}
        )
        assert "Breakdown by severity:" not in report


# ---------------------------------------------------------------------------
# _emit_summary_outputs / promotion helpers
# ---------------------------------------------------------------------------


def _sample_finding_result() -> tuple[dict[str, Any], AnalysisResult]:
    finding = {
        "type": "comment",
        "path": "src/foo.py",
        "start_line": 1,
        "end_line": 2,
        "index": 0,
        "content": "rename this",
        "severity": "high",
    }
    analysis = AnalysisResult(Verdict.VALID, "real issue", "")
    return finding, analysis


class TestEmitSummaryOutputs:
    def test_echo_true_prints_and_writes(self, tmp_path: Path, capsys: Any) -> None:
        pair = _sample_finding_result()
        outputs = _emit_summary_outputs(
            [pair],
            {"valid": 1, "rejected": 0, "backlog": 0, "timeout": 0},
            model="standard/coder",
            output_dir=tmp_path,
            summary_only=False,
            total_findings=1,
            echo=True,
        )
        captured = capsys.readouterr()
        assert "# Review Analysis Summary" in captured.out
        assert "Summary written to" in captured.out
        assert "Index written to" in captured.out
        assert outputs.summary_path == tmp_path / "SUMMARY.md"
        assert outputs.index_path == tmp_path / "INDEX.md"
        assert (tmp_path / "SUMMARY.md").read_text(
            encoding="utf-8"
        ).startswith("# Review Analysis Summary")

    def test_echo_false_writes_without_print(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        pair = _sample_finding_result()
        outputs = _emit_summary_outputs(
            [pair],
            {"valid": 1, "rejected": 0, "backlog": 0, "timeout": 0},
            model="standard/coder",
            output_dir=tmp_path,
            summary_only=False,
            total_findings=1,
            echo=False,
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert outputs.text.startswith("# Review Analysis Summary")
        assert outputs.summary_path is not None
        assert outputs.summary_path.is_file()
        assert outputs.index_path is not None
        assert outputs.index_path.is_file()

    def test_summary_only_does_not_write_files(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        pair = _sample_finding_result()
        outputs = _emit_summary_outputs(
            [pair],
            {"valid": 1},
            model="standard/coder",
            output_dir=tmp_path,
            summary_only=True,
            total_findings=1,
            echo=False,
        )
        assert outputs.summary_path is None
        assert outputs.index_path is None
        assert not (tmp_path / "SUMMARY.md").exists()
        assert capsys.readouterr().out == ""


class TestPromoteBacklogIfNeeded:
    def test_disabled_returns_none(self, tmp_path: Path, capsys: Any) -> None:
        finding, analysis = _sample_finding_result()
        analysis.verdict = Verdict.BACKLOG
        result = _promote_backlog_if_needed(
            [(finding, analysis)],
            knowledge_dir=tmp_path,
            ocr_file=tmp_path / "ocr.json",
            output_dir=tmp_path,
            model="standard/coder",
            timeout=30,
            write_backlog=False,
            summary_only=False,
            echo=True,
        )
        assert result is None
        assert "Promoting BACKLOG" not in capsys.readouterr().out

    def test_no_backlog_skips(self, tmp_path: Path) -> None:
        result = _promote_backlog_if_needed(
            [_sample_finding_result()],
            knowledge_dir=tmp_path,
            ocr_file=tmp_path / "ocr.json",
            output_dir=tmp_path,
            model="standard/coder",
            timeout=30,
            write_backlog=True,
            summary_only=False,
            echo=False,
        )
        assert result is None


# ---------------------------------------------------------------------------
# parse_args (backlog flags)
# ---------------------------------------------------------------------------


class TestParseArgsBacklogFlags:

    def test_defaults_write_backlog_on(self) -> None:
        from deep_architect.review_analyzer import parse_args

        args = parse_args(["ocr.json", "--no-tui"])
        assert args.no_write_backlog is False
        assert args.prior_feedback == []

    def test_prior_feedback_repeatable(self) -> None:
        from deep_architect.review_analyzer import parse_args

        args = parse_args(
            [
                "ocr.json",
                "--prior-feedback",
                "feedback-r1",
                "--prior-feedback",
                "feedback-r2",
                "--no-tui",
            ]
        )
        assert args.prior_feedback == ["feedback-r1", "feedback-r2"]

    def test_prior_feedback_comma_separated_expand(self) -> None:
        dirs = expand_prior_feedback_dirs(["a,b", "c"])
        assert dirs == [Path("a"), Path("b"), Path("c")]

    def test_no_write_backlog(self) -> None:
        from deep_architect.review_analyzer import parse_args

        args = parse_args(["ocr.json", "--no-write-backlog"])
        assert args.no_write_backlog is True

    def test_knowledge_dir(self, tmp_path: Path) -> None:
        from deep_architect.review_analyzer import parse_args

        k = tmp_path / "knowledge"
        args = parse_args(["ocr.json", "--knowledge-dir", str(k)])
        assert args.knowledge_dir == k


# ---------------------------------------------------------------------------
# _finding_path
# ---------------------------------------------------------------------------


class TestFindingPath:

    def test_comment_path(self) -> None:
        finding: dict[str, Any] = {"type": "comment", "path": "src/foo.py"}
        assert _finding_path(finding) == "src/foo.py"

    def test_warning_file(self) -> None:
        finding: dict[str, Any] = {"type": "warning", "file": "src/bar.py"}
        assert _finding_path(finding) == "src/bar.py"

    def test_missing_path(self) -> None:
        finding: dict[str, Any] = {"type": "comment"}
        assert _finding_path(finding) == "(unknown)"


# ---------------------------------------------------------------------------
# _finding_lines
# ---------------------------------------------------------------------------


class TestFindingLines:

    def test_comment_with_lines(self) -> None:
        finding: dict[str, Any] = {
            "type": "comment",
            "start_line": 10,
            "end_line": 15,
        }
        assert _finding_lines(finding) == "`:10-15`"

    def test_comment_without_lines(self) -> None:
        finding: dict[str, Any] = {"type": "comment"}
        assert _finding_lines(finding) == ""

    def test_warning(self) -> None:
        finding: dict[str, Any] = {"type": "warning", "file": "x.py"}
        assert _finding_lines(finding) == ""


# ---------------------------------------------------------------------------
# generate_index_report
# ---------------------------------------------------------------------------


class TestGenerateIndexReport:

    def test_empty_results(self) -> None:
        report = generate_index_report([])
        assert "# Review Findings Index" in report

    def test_grouped_by_verdict(self) -> None:
        comment: dict[str, Any] = {
            "type": "comment",
            "path": "src/foo.py",
            "content": "fix this",
            "start_line": 1,
            "end_line": 5,
            "index": 0,
        }
        results = [
            (comment, AnalysisResult(Verdict.VALID, "Real issue found", "")),
            (comment, AnalysisResult(Verdict.REJECTED, "False positive", "")),
        ]
        report = generate_index_report(results)
        assert "## VALID (1)" in report
        assert "## REJECTED (1)" in report
        assert "src/foo.py" in report
        assert "Real issue found" in report
        assert "False positive" in report
        assert "| # | Severity | File |" in report

    def test_severity_column(self) -> None:
        high: dict[str, Any] = {
            "type": "comment",
            "path": "a.py",
            "content": "fix",
            "start_line": 1,
            "end_line": 1,
            "index": 0,
            "severity": "high",
        }
        low: dict[str, Any] = {
            "type": "comment",
            "path": "b.py",
            "content": "nit",
            "start_line": 2,
            "end_line": 2,
            "index": 1,
            "severity": "low",
        }
        none: dict[str, Any] = {
            "type": "comment",
            "path": "c.py",
            "content": "old",
            "start_line": 3,
            "end_line": 3,
            "index": 2,
        }
        results = [
            (low, AnalysisResult(Verdict.VALID, "low issue", "")),
            (high, AnalysisResult(Verdict.VALID, "high issue", "")),
            (none, AnalysisResult(Verdict.VALID, "no sev", "")),
        ]
        report = generate_index_report(results)
        assert "`high`" in report
        assert "`low`" in report
        assert "—" in report
        # High severity row comes before low within VALID section
        assert report.index("`high`") < report.index("`low`")

    def test_pipes_escaped_in_preview(self) -> None:
        comment: dict[str, Any] = {
            "type": "comment",
            "path": "src/foo.py",
            "content": "fix",
            "start_line": 1,
            "end_line": 1,
            "index": 0,
        }
        results = [
            (comment, AnalysisResult(Verdict.VALID, "Has | pipe | chars", "")),
        ]
        report = generate_index_report(results)
        assert "\\|" in report

    def test_preview_truncated(self) -> None:
        comment: dict[str, Any] = {
            "type": "comment",
            "path": "src/foo.py",
            "content": "fix",
            "start_line": 1,
            "end_line": 1,
            "index": 0,
        }
        long_text = "x" * 200
        results = [
            (comment, AnalysisResult(Verdict.VALID, long_text, "")),
        ]
        report = generate_index_report(results)
        assert "…" in report

    def test_disk_rebuild_recovers_severity(self, tmp_path: Path) -> None:
        md = (
            "# OCR Review Analysis\n\n"
            "**Original OCR Finding**:\n\n"
            "- **File**: src/foo.py\n"
            "- **Lines**: 1-2\n"
            "- **Severity**: medium\n"
            "- **Type**: Comment\n"
            "- **Existing Code**:\n```\nx\n```\n"
            "- **Review Comment**: fix it\n\n"
            "## LLM Analysis\n\n"
            "**Verdict**: VALID\n\n"
            "**Analysis**:\nReal issue\n\n"
            "---\n\n"
            "*Generated by review-analyzer.*\n"
        )
        name = "abcd1234-0.md"
        (tmp_path / name).write_text(md, encoding="utf-8")
        report = generate_index_report_from_output_dir(tmp_path)
        assert "`medium`" in report
        assert "Severity" in report

    def test_tally_output_dir_severities(self, tmp_path: Path) -> None:
        high_md = (
            "# OCR Review Analysis\n\n"
            "- **File**: a.py\n"
            "- **Severity**: high\n"
            "- **Existing Code**:\n```\nx\n```\n"
            "- **Review Comment**: a\n\n"
            "**Verdict**: VALID\n"
        )
        none_md = (
            "# OCR Review Analysis\n\n"
            "- **File**: b.py\n"
            "- **Existing Code**:\n```\ny\n```\n"
            "- **Review Comment**: b\n\n"
            "**Verdict**: BACKLOG\n"
        )
        (tmp_path / "a-0.md").write_text(high_md, encoding="utf-8")
        (tmp_path / "b-1.md").write_text(none_md, encoding="utf-8")
        (tmp_path / "SUMMARY.md").write_text("# Summary\n", encoding="utf-8")
        counts = tally_output_dir_severities(tmp_path)
        assert counts == {"high": 1, "unknown": 1}


# ---------------------------------------------------------------------------
# should_use_tui / PlainReporter / progress callbacks
# ---------------------------------------------------------------------------


class TestShouldUseTui:

    def test_force_true(self) -> None:
        assert should_use_tui(force_tui=True) is True

    def test_force_false(self) -> None:
        assert should_use_tui(force_tui=False) is False

    def test_auto_tty(self) -> None:
        stream = MagicMock()
        stream.isatty.return_value = True
        assert should_use_tui(force_tui=None, stream=stream) is True

    def test_auto_non_tty(self) -> None:
        stream = MagicMock()
        stream.isatty.return_value = False
        assert should_use_tui(force_tui=None, stream=stream) is False


class TestPlainReporter:

    def test_start_and_on_result_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        meta = RunMeta(
            ocr_file=Path("review.json"),
            model="standard/coder",
            concurrency=3,
            output_dir=Path("feedback"),
            summary_only=False,
            total_findings=10,
            raw_findings=12,
        )
        reporter = PlainReporter()
        reporter.start(meta)
        finding = {
            "type": "comment",
            "path": "a.py",
            "start_line": 1,
            "end_line": 1,
            "index": 0,
            "content": "x",
        }
        analysis = AnalysisResult(Verdict.VALID, "ok", "")
        reporter.on_result(
            ProgressEvent(
                completed=5,
                total=10,
                finding=finding,
                analysis=analysis,
                elapsed_s=1.0,
            )
        )
        reporter.finish({"valid": 1, "rejected": 0, "backlog": 0})
        out = capsys.readouterr().out
        assert "Analyzing 10 findings" in out
        assert "Writing reports to feedback/" in out
        assert "Processed 5/10" in out


class TestProcessFindingsCallback:

    def test_on_result_called_per_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        findings = [
            {
                "type": "comment",
                "path": f"f{i}.py",
                "start_line": 1,
                "end_line": 1,
                "index": i,
                "content": "c",
            }
            for i in range(3)
        ]
        events: list[ProgressEvent] = []

        def fake_analyze(
            finding: dict[str, Any],
            model: str,
            breaker: Any,
            **kwargs: Any,
        ) -> AnalysisResult:
            return AnalysisResult(Verdict.VALID, f"ok-{finding['index']}", "")

        monkeypatch.setattr(
            "deep_architect.review_analyzer.analyze_finding", fake_analyze
        )

        results = process_findings_concurrently(
            findings,
            model="m",
            max_workers=2,
            output_dir=tmp_path,
            on_result=events.append,
        )
        assert len(results) == 3
        assert len(events) == 3
        assert {e.completed for e in events} == {1, 2, 3}
        assert all(e.total == 3 for e in events)
        # Files written
        assert len(list(tmp_path.glob("*.md"))) == 3


# ---------------------------------------------------------------------------
# Graceful shutdown during concurrent processing
# ---------------------------------------------------------------------------


class TestProcessFindingsShutdown:

    def test_skips_pending_after_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After request_shutdown, remaining findings are not analyzed."""
        review_analyzer_mod._shutdown_requested = False
        findings = [
            {
                "type": "comment",
                "path": f"f{i}.py",
                "start_line": 1,
                "end_line": 1,
                "index": i,
                "content": "c",
            }
            for i in range(8)
        ]
        started = 0
        lock = threading.Lock()

        def fake_analyze(
            finding: dict[str, Any],
            model: str,
            breaker: Any,
            **kwargs: Any,
        ) -> AnalysisResult:
            nonlocal started
            with lock:
                started += 1
                n = started
            # First analysis requests stop; later submissions see the flag
            # in _analyze_one and never call analyze_finding.
            if n == 1:
                request_shutdown()
            return AnalysisResult(Verdict.VALID, f"ok-{finding['index']}", "")

        monkeypatch.setattr(
            "deep_architect.review_analyzer.analyze_finding", fake_analyze
        )

        results = process_findings_concurrently(
            findings,
            model="m",
            max_workers=1,  # serial: after first completes, rest skip
            output_dir=tmp_path,
        )
        assert len(results) == 1
        assert started == 1
        assert results[0][1].verdict == Verdict.VALID
        review_analyzer_mod._shutdown_requested = False


# ---------------------------------------------------------------------------
# Timeout report detection / retry selection
# ---------------------------------------------------------------------------


class TestTimeoutReportHelpers:

    def test_is_timeout_report_timeout_verdict(self, tmp_path: Path) -> None:
        md = tmp_path / "abc-0.md"
        md.write_text(
            "# OCR Review Analysis\n\n"
            "**Verdict**: TIMEOUT\n\n"
            "**Analysis**:\n\nopencode execution timed out (>120s)\n",
            encoding="utf-8",
        )
        assert is_timeout_report(md) is True

    def test_is_timeout_report_legacy_backlog(self, tmp_path: Path) -> None:
        md = tmp_path / "abc-1.md"
        md.write_text(
            "# OCR Review Analysis\n\n"
            "**Verdict**: BACKLOG\n\n"
            "**Analysis**:\n\nopencode execution timed out (>120s)\n",
            encoding="utf-8",
        )
        assert is_timeout_report(md) is True

    def test_is_timeout_report_normal_backlog(self, tmp_path: Path) -> None:
        md = tmp_path / "abc-2.md"
        md.write_text(
            "# OCR Review Analysis\n\n"
            "**Verdict**: BACKLOG\n\n"
            "**Analysis**:\n\nNice-to-have refactor later\n",
            encoding="utf-8",
        )
        assert is_timeout_report(md) is False

    def test_is_timeout_report_valid(self, tmp_path: Path) -> None:
        md = tmp_path / "abc-3.md"
        md.write_text(
            "# OCR Review Analysis\n\n"
            "**Verdict**: VALID\n\n"
            "**Analysis**:\n\nReal issue\n",
            encoding="utf-8",
        )
        assert is_timeout_report(md) is False

    def test_select_timeout_findings_for_retry(self, tmp_path: Path) -> None:
        findings = [
            {
                "type": "comment",
                "path": "a.py",
                "start_line": 1,
                "end_line": 1,
                "index": 0,
                "content": "c0",
            },
            {
                "type": "comment",
                "path": "b.py",
                "start_line": 1,
                "end_line": 1,
                "index": 1,
                "content": "c1",
            },
            {
                "type": "comment",
                "path": "c.py",
                "start_line": 1,
                "end_line": 1,
                "index": 2,
                "content": "c2",
            },
        ]
        # Write timeout report only for finding 0
        name0 = generate_output_filename(findings[0])
        (tmp_path / name0).write_text(
            "**Verdict**: TIMEOUT\n\n**Analysis**:\n\nopencode timed out\n",
            encoding="utf-8",
        )
        # Valid report for finding 1
        name1 = generate_output_filename(findings[1])
        (tmp_path / name1).write_text(
            "**Verdict**: VALID\n\n**Analysis**:\n\nok\n",
            encoding="utf-8",
        )
        selected = select_timeout_findings_for_retry(findings, tmp_path)
        assert len(selected) == 1
        assert selected[0]["path"] == "a.py"

    def test_tally_output_dir_verdicts(self, tmp_path: Path) -> None:
        (tmp_path / "a-0.md").write_text("**Verdict**: VALID\n", encoding="utf-8")
        (tmp_path / "b-1.md").write_text("**Verdict**: TIMEOUT\n", encoding="utf-8")
        (tmp_path / "c-2.md").write_text("**Verdict**: BACKLOG\n", encoding="utf-8")
        (tmp_path / "SUMMARY.md").write_text("# summary\n", encoding="utf-8")
        counts = tally_output_dir_verdicts(tmp_path)
        assert counts["valid"] == 1
        assert counts["timeout"] == 1
        assert counts["backlog"] == 1
        assert counts["rejected"] == 0


# ---------------------------------------------------------------------------
