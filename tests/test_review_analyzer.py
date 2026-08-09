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
from deep_architect.review_analyzer import (
    AnalysisResult,
    CircuitBreaker,
    PlainReporter,
    ProgressEvent,
    RunMeta,
    Verdict,
    _finding_lines,
    _finding_path,
    _parse_opencode_json,
    call_opencode_analysis,
    construct_analysis_prompt,
    default_opencode_timeout,
    extract_findings,
    filter_findings_by_path,
    generate_index_report,
    generate_markdown_content,
    generate_output_filename,
    generate_summary_report,
    get_filepath_hash,
    is_timeout_report,
    load_ocr_json,
    process_findings_concurrently,
    request_shutdown,
    resolve_opencode_timeout,
    select_timeout_findings_for_retry,
    should_use_tui,
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

    def test_from_string(self) -> None:
        assert Verdict("valid") == Verdict.VALID
        assert Verdict("timeout") == Verdict.TIMEOUT

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            Verdict("not_a_verdict")


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

    def test_zero_total(self) -> None:
        report = generate_summary_report({}, 0, model="standard/coder")
        assert "Coding agent: opencode (standard/coder)" in report
        assert "Total findings processed: 0" in report

    def test_includes_model(self) -> None:
        report = generate_summary_report({}, 0, model="custom/model")
        assert "Coding agent: opencode (custom/model)" in report


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
