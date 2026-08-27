"""Unit tests for the review-driver loop, progress I/O, and formatters."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import git
import pytest

from deep_architect import review_driver as review_driver_mod
from deep_architect.config import HarnessConfig
from deep_architect.review_driver import (
    DEFAULT_OUTPUT_DIR,
    DriverPassRecord,
    DriverPreflightError,
    DriverProgress,
    ProductionRunners,
    branch_run_parent,
    default_ocr_report_excludes,
    default_output_excludes,
    format_ocr_session_event,
    format_ocr_summary,
    format_pass_fraction,
    format_stop_line,
    format_trend,
    last_ocr_invocation_log,
    load_driver_progress,
    main,
    ocr_process_timeout_reason,
    ocr_process_timeout_seconds,
    ocr_session_dir,
    parse_args,
    preflight_driver,
    request_force_stop,
    request_interrupt,
    resolve_driver_run_dir,
    resolve_ocr_concurrency,
    resolve_ocr_llm_timeout_seconds,
    resolve_ocr_timeout_minutes,
    run_action_main,
    run_analyzer_main,
    run_driver,
    run_ocr_subprocess,
    sanitize_run_slug,
    save_driver_progress,
    should_use_tui,
    write_driver_report,
)
from deep_architect.review_novelty import OcrRunStats


@pytest.fixture(autouse=True)
def _clear_driver_interrupt_flags() -> object:
    review_driver_mod._reset_interrupt_state()
    yield
    review_driver_mod._reset_interrupt_state()


def _finding_md(
    *,
    file_path: str = "src/example.py",
    verdict: str = "VALID",
    severity: str | None = "high",
) -> str:
    severity_line = f"- **Severity**: {severity}\n" if severity else ""
    return (
        "# OCR Review Analysis\n\n"
        "**Original OCR Finding**:\n\n"
        f"- **File**: {file_path}\n"
        f"- **Lines**: 1-2\n"
        f"{severity_line}"
        "- **Existing Code**:\n```\nold()\n```\n"
        "- **Suggested Code**:\n```\nnew()\n```\n"
        "- **Review Comment**: fix it\n\n"
        "## LLM Analysis\n\n"
        f"**Verdict**: {verdict}\n\n"
        "**Analysis**:\nConfirmed.\n"
    )


def _write_novelty_findings(feedback_dir: Path, novelty: int) -> None:
    feedback_dir.mkdir(parents=True, exist_ok=True)
    for i in range(novelty):
        (feedback_dir / f"finding-{i}.md").write_text(
            _finding_md(file_path=f"src/f{i}.py", verdict="VALID", severity="high"),
            encoding="utf-8",
        )


def _tiny_ocr_json(*, high: int = 0, medium: int = 0, low: int = 0) -> dict[str, object]:
    comments: list[dict[str, str]] = []
    comments.extend({"severity": "high", "content": "h"} for _ in range(high))
    comments.extend({"severity": "medium", "content": "m"} for _ in range(medium))
    comments.extend({"severity": "low", "content": "l"} for _ in range(low))
    return {"comments": comments}


def _pass_index_from_ocr(output_json: Path) -> int:
    # code-review-rN.json
    stem = output_json.stem
    return int(stem.rsplit("r", 1)[1])


@dataclass
class ScriptedRunners:
    novelties: list[int]
    ocr_rcs: list[int] = field(default_factory=list)
    action_rcs: list[int] = field(default_factory=list)
    ocr_high: list[int] | None = None
    calls: list[str] = field(default_factory=list)
    analyzer_priors: list[list[Path]] = field(default_factory=list)
    ocr_outputs: list[Path] = field(default_factory=list)

    def run_ocr(
        self, *, source: str, target: str, output_json: Path, exclude: list[str]
    ) -> int:
        del source, target, exclude
        idx = _pass_index_from_ocr(output_json) - 1
        self.calls.append("ocr")
        self.ocr_outputs.append(output_json)
        rc = self.ocr_rcs[idx] if idx < len(self.ocr_rcs) else 0
        if rc != 0:
            return rc
        high = self.ocr_high[idx] if self.ocr_high is not None and idx < len(self.ocr_high) else 0
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(_tiny_ocr_json(high=high)), encoding="utf-8")
        return 0

    def run_analyzer(
        self,
        *,
        ocr_json: Path,
        feedback_dir: Path,
        prior_feedback: list[Path],
        knowledge_dir: Path | None,
        exclude: list[str],
    ) -> int:
        del ocr_json, knowledge_dir, exclude
        idx = int(feedback_dir.name.rsplit("r", 1)[1]) - 1
        self.calls.append("analyzer")
        self.analyzer_priors.append(list(prior_feedback))
        novelty = self.novelties[idx] if idx < len(self.novelties) else 0
        _write_novelty_findings(feedback_dir, novelty)
        return 0

    def run_action(self, *, feedback_dir: Path) -> int:
        idx = int(feedback_dir.name.rsplit("r", 1)[1]) - 1
        self.calls.append("action")
        if idx < len(self.action_rcs):
            return self.action_rcs[idx]
        return 0


def _run(
    tmp_path: Path,
    runners: ScriptedRunners,
    *,
    max_passes: int = 5,
    k: int = 2,
    resume: bool = True,
    source: str = "feat",
    target: str = "main",
) -> DriverProgress:
    return run_driver(
        source=source,
        target=target,
        output_dir=tmp_path,
        runners=runners,
        max_passes=max_passes,
        k=k,
        resume=resume,
        source_sha="aaa",
        target_sha="bbb",
    )


class TestFormatters:
    def test_format_ocr_summary_includes_tokens_when_set(self) -> None:
        stats = OcrRunStats(
            comments=31,
            files_reviewed=12,
            total_tokens=412345,
            input_tokens=300000,
            output_tokens=112345,
        )
        text = format_ocr_summary(
            stats, {"high": 5, "medium": 13, "low": 13}, wall_seconds=241
        )
        assert "tokens 412345" in text
        assert "in 300000" in text
        assert "out 112345" in text

    def test_format_ocr_summary_omits_tokens_when_none(self) -> None:
        text = format_ocr_summary(OcrRunStats(comments=4), {"high": 1}, wall_seconds=3)
        assert "tokens" not in text

    def test_format_ocr_summary_partial_and_timeouts(self) -> None:
        stats = OcrRunStats(
            comments=1,
            files_reviewed=16,
            files_failed=13,
            timeout_failures=23,
            status="partial",
            total_tokens=95216,
        )
        text = format_ocr_summary(
            stats, {"high": 0, "medium": 1, "low": 0}, wall_seconds=1201
        )
        assert text.startswith("OCR      PARTIAL")
        assert "13 failed" in text
        assert "23 LLM timeouts" in text

    def test_format_ocr_summary_failed_includes_reason(self) -> None:
        stats = OcrRunStats(
            comments=0,
            files_reviewed=16,
            files_failed=16,
            timeout_failures=25,
            failed_requests=25,
            status="failed",
        )
        text = format_ocr_summary(stats, {}, wall_seconds=1201)
        assert text.startswith("OCR      FAILED")
        assert "16 failed" in text
        assert "context deadline exceeded" in text
        assert "API key" not in text

    def test_format_ocr_summary_failed_includes_process_timeout(self) -> None:
        stats = OcrRunStats(
            comments=0,
            status="failed",
            message="ocr process timeout after 1h00m00s",
        )
        text = format_ocr_summary(stats, {}, wall_seconds=3600.0)
        assert text.startswith("OCR      FAILED")
        assert "ocr process timeout after 1h00m00s" in text
        assert "deadline exceeded" not in text

    def test_format_pass_fraction_unlimited(self) -> None:
        assert format_pass_fraction(3, 5) == "3/5"
        assert format_pass_fraction(3, 0) == "3/∞"

    def test_format_stop_line_includes_detail(self) -> None:
        assert format_stop_line("failed", 2) == "Stopped: failed."
        assert (
            format_stop_line("failed", 2, "context deadline exceeded")
            == "Stopped: failed — context deadline exceeded"
        )

    def test_format_trend_novelty_and_high(self) -> None:
        previous = DriverPassRecord(
            pass_index=1,
            ocr_json="code-review-r1.json",
            feedback_dir="feedback-r1",
            novelty=3,
            valid_total=13,
            ocr_severity={"high": 7, "medium": 14, "low": 10},
            action_errors=0,
            action_committed=4,
            status="complete",
        )
        current = DriverPassRecord(
            pass_index=2,
            ocr_json="code-review-r2.json",
            feedback_dir="feedback-r2",
            novelty=1,
            valid_total=8,
            ocr_severity={"high": 5, "medium": 13, "low": 13},
            action_errors=0,
            action_committed=2,
            status="complete",
        )
        text = format_trend(previous, current)
        assert "novelty 3→1" in text
        assert "high 7→5" in text


class TestProgressIO:
    def test_round_trip_and_no_tmp_remnant(self, tmp_path: Path) -> None:
        progress = DriverProgress(
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
            novelty_history=[3],
            current_pass=1,
        )
        path = save_driver_progress(tmp_path, progress)
        assert path == tmp_path / "progress.json"
        assert not (tmp_path / "progress.tmp").exists()
        loaded = load_driver_progress(tmp_path)
        assert loaded.source == "feat"
        assert loaded.novelty_history == [3]
        assert loaded.current_pass == 1


class TestRunDriver:
    def test_call_order_ocr_analyzer_action(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[0, 0])
        result = _run(tmp_path, runners, max_passes=5, k=2)
        assert result.status == "converged"
        assert runners.calls == [
            "ocr",
            "analyzer",
            "action",
            "ocr",
            "analyzer",
            "action",
        ]

    def test_pass_2_analyzer_receives_prior_feedback(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[1, 0, 0])
        _run(tmp_path, runners, max_passes=5, k=2)
        assert runners.analyzer_priors[0] == []
        assert runners.analyzer_priors[1] == [tmp_path / "feedback-r1"]

    def test_scripted_3100_converges_after_four_ocr_calls(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[3, 1, 0, 0])
        result = _run(tmp_path, runners, max_passes=5, k=2)
        assert result.status == "converged"
        assert result.novelty_history == [3, 1, 0, 0]
        assert result.current_pass == 4
        assert runners.calls.count("ocr") == 4

    def test_scripted_111_hits_max_passes(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[1, 1, 1])
        result = _run(tmp_path, runners, max_passes=3, k=2)
        assert result.status == "max_passes"
        assert result.novelty_history == [1, 1, 1]
        assert result.current_pass == 3

    def test_max_passes_zero_runs_until_converged(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[1, 1, 0, 0])
        result = _run(tmp_path, runners, max_passes=0, k=2)
        assert result.status == "converged"
        assert result.novelty_history == [1, 1, 0, 0]
        assert result.current_pass == 4
        assert runners.calls.count("ocr") == 4

    def test_ocr_failure_on_pass_2(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[3, 1], ocr_rcs=[0, 1])
        result = _run(tmp_path, runners, max_passes=5, k=2)
        assert result.status == "failed"
        assert result.current_pass == 1
        assert runners.calls == ["ocr", "analyzer", "action", "ocr"]
        failed = [p for p in result.passes if p.pass_index == 2]
        assert not failed or failed[0].status == "failed"
        assert result.stop_detail == "ocr exited rc=1"
        assert failed and failed[0].failure_reason == "ocr exited rc=1"

    def test_interrupt_before_pass_skips_ocr(self, tmp_path: Path) -> None:
        review_driver_mod._reset_interrupt_state()
        request_interrupt()
        try:
            runners = ScriptedRunners(novelties=[1])
            result = _run(tmp_path, runners, max_passes=2, k=2)
        finally:
            review_driver_mod._reset_interrupt_state()
        assert result.status == "failed"
        assert runners.calls == []

    def test_interrupt_after_ocr_skips_analyzer(self, tmp_path: Path) -> None:
        review_driver_mod._reset_interrupt_state()

        class InterruptingRunners(ScriptedRunners):
            def run_ocr(
                self,
                *,
                source: str,
                target: str,
                output_json: Path,
                exclude: list[str],
            ) -> int:
                rc = super().run_ocr(
                    source=source,
                    target=target,
                    output_json=output_json,
                    exclude=exclude,
                )
                request_interrupt()
                return rc

        try:
            runners = InterruptingRunners(novelties=[1])
            result = _run(tmp_path, runners, max_passes=2, k=2)
        finally:
            review_driver_mod._reset_interrupt_state()
        assert result.status == "failed"
        assert result.stop_detail == "interrupted"
        assert runners.calls == ["ocr"]

    def test_report_includes_stop_detail(self, tmp_path: Path) -> None:
        progress = DriverProgress(
            status="failed",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
            stop_detail="context deadline exceeded (25 LLM requests timed out at ~5m)",
        )
        path = write_driver_report(tmp_path, progress)
        text = path.read_text(encoding="utf-8")
        assert "Stop reason: failed — context deadline exceeded" in text

    def test_action_errors_still_converge(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[0, 0], action_rcs=[1, 0])
        result = _run(tmp_path, runners, max_passes=5, k=2)
        assert result.status == "converged"
        assert result.passes[0].action_errors >= 1
        assert result.novelty_history == [0, 0]

    def test_resume_skips_completed_pass(self, tmp_path: Path) -> None:
        seed_feedback = tmp_path / "feedback-r1"
        _write_novelty_findings(seed_feedback, 3)
        (tmp_path / "code-review-r1.json").write_text(
            json.dumps(_tiny_ocr_json(high=3)), encoding="utf-8"
        )
        seed = DriverProgress(
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            current_pass=1,
            consecutive_zero_novelty=0,
            novelty_history=[3],
            output_dir=str(tmp_path),
            passes=[
                DriverPassRecord(
                    pass_index=1,
                    ocr_json=str(tmp_path / "code-review-r1.json"),
                    feedback_dir=str(seed_feedback),
                    novelty=3,
                    valid_total=3,
                    ocr_severity={"high": 3},
                    action_errors=0,
                    action_committed=0,
                    status="complete",
                )
            ],
        )
        save_driver_progress(tmp_path, seed)

        runners = ScriptedRunners(novelties=[3, 0, 0])
        result = _run(tmp_path, runners)
        ocr_names = [p.name for p in runners.ocr_outputs]
        assert "code-review-r1.json" not in ocr_names
        assert ocr_names[0] == "code-review-r2.json"
        assert runners.analyzer_priors[0] == [tmp_path / "feedback-r1"]
        assert result.novelty_history[0] == 3
        assert result.current_pass >= 2

    def test_resume_missing_progress_starts_fresh(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[0, 0])
        result = _run(tmp_path, runners, max_passes=2, k=2)
        ocr_names = [p.name for p in runners.ocr_outputs]
        assert ocr_names[0] == "code-review-r1.json"
        assert result.status == "converged"

    def test_no_resume_starts_at_pass_1(self, tmp_path: Path) -> None:
        seed = DriverProgress(
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            current_pass=1,
            consecutive_zero_novelty=0,
            novelty_history=[3],
            output_dir=str(tmp_path),
        )
        save_driver_progress(tmp_path, seed)

        runners = ScriptedRunners(novelties=[0, 0])
        result = _run(tmp_path, runners, resume=False, max_passes=2, k=2)
        ocr_names = [p.name for p in runners.ocr_outputs]
        assert ocr_names[0] == "code-review-r1.json"
        assert result.status == "converged"

    def test_resume_source_mismatch_fails(self, tmp_path: Path) -> None:
        seed = DriverProgress(
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
        )
        save_driver_progress(tmp_path, seed)
        runners = ScriptedRunners(novelties=[0])
        with pytest.raises(ValueError, match="--no-resume"):
            _run(tmp_path, runners, source="other")
        assert runners.calls == []

    def test_resume_terminal_status_does_not_rerun(self, tmp_path: Path) -> None:
        seed = DriverProgress(
            status="converged",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            current_pass=2,
            consecutive_zero_novelty=2,
            novelty_history=[0, 0],
            output_dir=str(tmp_path),
        )
        save_driver_progress(tmp_path, seed)
        runners = ScriptedRunners(novelties=[9, 9, 9])
        result = _run(tmp_path, runners)
        assert result.status == "converged"
        assert result.current_pass == 2
        assert runners.calls == []

    def test_loop_stdout_is_phase_summaries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runners = ScriptedRunners(novelties=[3, 1], ocr_high=[7, 5])
        _run(tmp_path, runners, max_passes=2, k=2)
        out = capsys.readouterr().out
        assert "Pass 1/" in out
        assert "OCR starting" in out
        assert "Analyzer starting" in out
        assert "Action starting" in out
        assert "novelty=" in out
        assert "Trend" in out
        assert "Processed " not in out
        assert "Applying fixes" not in out


class _FakePopen:
    """Minimal Popen stand-in for OCR runner tests."""

    def __init__(self, *, stdout: str, stderr: str) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = 0
        self.pid: int | None = None
        self.kill = MagicMock()
        self.terminate = MagicMock()

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode


# ---------------------------------------------------------------------------
# Preflight + production runners (Phase 3)
# ---------------------------------------------------------------------------


def _repo_main_and_feat(tmp_path: Path) -> git.Repo:
    repo = git.Repo.init(tmp_path)
    (tmp_path / "README.md").write_text("main\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init main")
    if "main" not in repo.heads:
        repo.create_head("main")
    repo.heads.main.checkout()
    repo.create_head("feat")
    repo.heads.feat.checkout()
    (tmp_path / "feat.txt").write_text("feat\n", encoding="utf-8")
    repo.index.add(["feat.txt"])
    repo.index.commit("feat commit")
    return repo


class TestPreflight:
    def test_wrong_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo_main_and_feat(tmp_path)
        repo.heads.main.checkout()
        monkeypatch.setattr(
            "deep_architect.review_driver.shutil.which", lambda _name: "/usr/bin/ocr"
        )
        with pytest.raises(DriverPreflightError, match="Check out --source"):
            preflight_driver(
                cwd=tmp_path,
                source="feat",
                target="main",
                output_dir=tmp_path / ".review-runs",
                ocr_bin="ocr",
            )

    def test_matching_branch_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _repo_main_and_feat(tmp_path)
        monkeypatch.setattr(
            "deep_architect.review_driver.shutil.which", lambda _name: "/usr/bin/ocr"
        )
        repo, source_sha, target_sha = preflight_driver(
            cwd=tmp_path,
            source="feat",
            target="main",
            output_dir=tmp_path / ".review-runs",
            ocr_bin="ocr",
        )
        assert source_sha == repo.head.commit.hexsha
        assert target_sha == repo.commit("main").hexsha

    def test_detached_head_at_source_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo_main_and_feat(tmp_path)
        feat_sha = repo.commit("feat").hexsha
        repo.git.checkout(feat_sha)
        monkeypatch.setattr(
            "deep_architect.review_driver.shutil.which", lambda _name: "/usr/bin/ocr"
        )
        _, source_sha, _ = preflight_driver(
            cwd=tmp_path,
            source="feat",
            target="main",
            output_dir=tmp_path / ".review-runs",
            ocr_bin="ocr",
        )
        assert source_sha == feat_sha

    def test_dirty_tracked_outside_output_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _repo_main_and_feat(tmp_path)
        (tmp_path / "README.md").write_text("dirty\n", encoding="utf-8")
        monkeypatch.setattr(
            "deep_architect.review_driver.shutil.which", lambda _name: "/usr/bin/ocr"
        )
        with pytest.raises(DriverPreflightError, match="Dirty tracked files"):
            preflight_driver(
                cwd=tmp_path,
                source="feat",
                target="main",
                output_dir=tmp_path / ".review-runs",
                ocr_bin="ocr",
            )

    def test_dirty_inside_output_dir_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo_main_and_feat(tmp_path)
        output = tmp_path / ".review-runs"
        output.mkdir()
        artifact = output / "progress.json"
        artifact.write_text("{}\n", encoding="utf-8")
        repo.index.add([".review-runs/progress.json"])
        repo.index.commit("seed artifacts")
        artifact.write_text('{"dirty": true}\n', encoding="utf-8")
        monkeypatch.setattr(
            "deep_architect.review_driver.shutil.which", lambda _name: "/usr/bin/ocr"
        )
        preflight_driver(
            cwd=tmp_path,
            source="feat",
            target="main",
            output_dir=output,
            ocr_bin="ocr",
        )

    def test_untracked_outside_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _repo_main_and_feat(tmp_path)
        (tmp_path / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        monkeypatch.setattr(
            "deep_architect.review_driver.shutil.which", lambda _name: "/usr/bin/ocr"
        )
        preflight_driver(
            cwd=tmp_path,
            source="feat",
            target="main",
            output_dir=tmp_path / ".review-runs",
            ocr_bin="ocr",
        )

    def test_missing_ocr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _repo_main_and_feat(tmp_path)
        monkeypatch.setattr("deep_architect.review_driver.shutil.which", lambda _name: None)
        with pytest.raises(DriverPreflightError, match="OpenCodeReview"):
            preflight_driver(
                cwd=tmp_path,
                source="feat",
                target="main",
                output_dir=tmp_path / ".review-runs",
                ocr_bin="ocr",
            )


class TestProductionRunners:
    def test_ocr_argv_from_target_to_source(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        fake = _FakePopen(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake) as mocked:
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=["**/generated/*"],
                cwd=tmp_path,
                ocr_bin="ocr",
            )
        assert rc == 0
        argv = mocked.call_args[0][0]
        assert argv[:2] == ["ocr", "review"]
        assert argv[argv.index("--from") + 1] == "main"
        assert argv[argv.index("--to") + 1] == "feat"
        assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
        assert "--audience" in argv and argv[argv.index("--audience") + 1] == "agent"
        assert argv[argv.index("--timeout") + 1] == "10"
        assert argv[argv.index("--concurrency") + 1] == "8"
        assert argv[argv.index("--exclude") + 1] == "**/generated/*"
        assert output_json.read_text(encoding="utf-8") == '{"comments":[]}\n'

    def test_ocr_argv_uses_timeout_and_concurrency(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        fake = _FakePopen(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake) as mocked:
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
                ocr_timeout_minutes=30,
                ocr_concurrency=3,
            )
        assert rc == 0
        argv = mocked.call_args[0][0]
        assert argv[argv.index("--timeout") + 1] == "30"
        assert argv[argv.index("--concurrency") + 1] == "3"

    def test_ocr_exports_llm_timeout_env(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        fake = _FakePopen(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake) as mocked:
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
                ocr_llm_timeout_seconds=1200,
            )
        assert rc == 0
        env = mocked.call_args.kwargs["env"]
        assert env["OCR_LLM_TIMEOUT"] == "1200"
        log_text = (tmp_path / "logs" / "r1-ocr.log").read_text(encoding="utf-8")
        assert "llm-http-timeout=1200s" in log_text
        assert "process-timeout=" in log_text

    def test_ocr_omits_llm_timeout_env_when_unset(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        fake = _FakePopen(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake) as mocked:
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
            )
        assert rc == 0
        assert "env" not in mocked.call_args.kwargs
        log_text = (tmp_path / "logs" / "r1-ocr.log").read_text(encoding="utf-8")
        assert "llm-http-timeout=ocr-default" in log_text

    def test_production_runners_merges_output_excludes(self, tmp_path: Path) -> None:
        (tmp_path / ".review-runs").mkdir()
        runners = ProductionRunners(
            cwd=tmp_path,
            output_dir=tmp_path / ".review-runs",
            ocr_llm_timeout_seconds=1200,
        )
        with patch(
            "deep_architect.review_driver.run_ocr_subprocess", return_value=0
        ) as mocked:
            rc = runners.run_ocr(
                source="feat",
                target="main",
                output_json=tmp_path / "code-review-r1.json",
                exclude=["vendor/**"],
            )
        assert rc == 0
        assert mocked.call_args.kwargs["exclude"] == [
            ".review-runs/**",
            "code-review*.json",
            "code-review-*.json",
            "vendor/**",
        ]
        assert mocked.call_args.kwargs["ocr_llm_timeout_seconds"] == 1200

    def test_ocr_streams_stderr_to_log_and_callback(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        seen: list[str] = []
        fake = _FakePopen(
            stdout='{"comments":[]}\n',
            stderr="ocr: reviewing 3 files\n",
        )
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake):
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
                on_child_log=seen.append,
            )
        assert rc == 0
        log_text = (tmp_path / "logs" / "r1-ocr.log").read_text(encoding="utf-8")
        assert "ocr: reviewing 3 files" in log_text
        assert any("ocr: reviewing 3 files" in chunk for chunk in seen)

    def test_ocr_writes_json_when_process_exits_1(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r2.json"
        payload = (
            '{"status":"failed","llm":{"model":"nemotron-3-super"},'
            '"summary":{"files_reviewed":16,"comments":0}}\n'
        )
        fake = _FakePopen(
            stdout=payload,
            stderr="Error: review failed: all 16 file review(s) failed\n",
        )
        fake.returncode = 1
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake):
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
            )
        assert rc == 1
        assert output_json.is_file()
        written = json.loads(output_json.read_text(encoding="utf-8"))
        assert written["status"] == "failed"
        assert written["summary"]["files_reviewed"] == 16

    def test_ocr_audience_human_when_requested(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        fake = _FakePopen(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake) as mocked:
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
                audience="human",
            )
        assert rc == 0
        argv = mocked.call_args[0][0]
        assert argv[argv.index("--audience") + 1] == "human"

    def test_ocr_writes_start_line_to_log_and_callback(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        seen: list[str] = []
        fake = _FakePopen(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake):
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
                on_child_log=seen.append,
            )
        assert rc == 0
        log_text = (tmp_path / "logs" / "r1-ocr.log").read_text(encoding="utf-8")
        assert "OCR starting:" in log_text
        assert "concurrency=8" in log_text
        assert any("OCR starting:" in chunk for chunk in seen)

    def test_ocr_tails_session_jsonl_into_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output_json = tmp_path / "code-review-r1.json"
        session_dir = tmp_path / "ocr-sessions"
        session_dir.mkdir()
        session_file = session_dir / "sess.jsonl"
        monkeypatch.setattr(
            "deep_architect.review_driver.OCR_SESSION_POLL_SECONDS", 0.05
        )
        events = [
            {
                "type": "session_start",
                "sessionId": "abc-123",
                "cwd": str(tmp_path),
                "diffFrom": "main",
                "diffTo": "feat",
            },
            {
                "type": "llm_request",
                "filePath": "src/a.py",
                "taskType": "plan_task",
            },
            {
                "type": "llm_request",
                "filePath": "src/a.py",
                "taskType": "main_task",
            },
            {"type": "tool_call", "filePath": "src/a.py", "tool_name": "file_read"},
            {
                "type": "review_item_done",
                "filePath": "src/a.py",
            },
            {
                "type": "llm_error",
                "filePath": "src/b.py",
                "error": "context deadline exceeded",
            },
            {
                "type": "review_item_failed",
                "filePath": "src/b.py",
                "error": "LLM completion error: context deadline exceeded",
            },
        ]
        payload = "".join(json.dumps(ev) + "\n" for ev in events)
        seen: list[str] = []

        class _WaitWritesJsonl(_FakePopen):
            def wait(self, timeout: float | None = None) -> int:
                del timeout
                session_file.write_text(payload, encoding="utf-8")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if any("[ocr] failed src/b.py" in chunk for chunk in seen):
                        break
                    time.sleep(0.05)
                return self.returncode

        fake = _WaitWritesJsonl(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake):
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
                on_child_log=seen.append,
                session_dir=session_dir,
            )
        assert rc == 0
        joined = "".join(seen)
        assert "[ocr] session abc-123 main..feat" in joined
        assert "[ocr] reviewing src/a.py (plan_task)" in joined
        assert joined.count("[ocr] reviewing src/a.py") == 1
        assert "[ocr] done src/a.py" in joined
        assert "[ocr] llm error src/b.py: context deadline exceeded" in joined
        assert "[ocr] failed src/b.py: LLM completion error: context deadline exceeded" in joined
        assert "file_read" not in joined
        log_text = (tmp_path / "logs" / "r1-ocr.log").read_text(encoding="utf-8")
        assert "[ocr] done src/a.py" in log_text

    def test_ocr_timeout_returns_1(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        stale = '{"status": "failed", "message": "context deadline exceeded"}'
        output_json.write_text(stale, encoding="utf-8")
        fake = _FakePopen(stdout="", stderr="")
        fake.wait = MagicMock(side_effect=subprocess.TimeoutExpired("ocr", 1))
        with (
            patch("deep_architect.review_driver.subprocess.Popen", return_value=fake),
            patch("deep_architect.review_driver._ocr_timeout_seconds", return_value=0.01),
        ):
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
            )
        assert rc == 1
        fake.kill.assert_called_once()
        log_text = (tmp_path / "logs" / "r1-ocr.log").read_text(encoding="utf-8")
        assert "ocr timed out after" in log_text
        assert output_json.read_text(encoding="utf-8") == stale

    def test_ocr_force_stop_kills_child_and_returns_130(self, tmp_path: Path) -> None:
        review_driver_mod._reset_interrupt_state()
        script = tmp_path / "fake-ocr"
        script.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
        script.chmod(0o755)
        output_json = tmp_path / "code-review-r1.json"
        started = threading.Event()
        real_popen = subprocess.Popen

        def _started_popen(
            *args: object, **kwargs: object
        ) -> subprocess.Popen[str]:
            proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            started.set()
            return proc

        def _kill_after_start() -> None:
            assert started.wait(timeout=5)
            time.sleep(0.1)
            request_force_stop()

        killer = threading.Thread(target=_kill_after_start)
        t0 = time.monotonic()
        try:
            with patch("deep_architect.review_driver.subprocess.Popen", _started_popen):
                killer.start()
                rc = run_ocr_subprocess(
                    source="feat",
                    target="main",
                    output_json=output_json,
                    exclude=[],
                    cwd=tmp_path,
                    ocr_bin=str(script),
                )
            killer.join(timeout=5)
        finally:
            review_driver_mod._reset_interrupt_state()
        elapsed = time.monotonic() - t0
        assert rc == 130
        assert elapsed < 10
        log_text = (tmp_path / "logs" / "r1-ocr.log").read_text(encoding="utf-8")
        assert "force stop" in log_text

    def test_request_force_stop_without_child_sets_flags(self) -> None:
        review_driver_mod._reset_interrupt_state()
        try:
            request_force_stop()
            assert review_driver_mod._interrupt_requested is True
            assert review_driver_mod._force_stop_requested is True
        finally:
            review_driver_mod._reset_interrupt_state()

    def test_second_sigint_requests_force_stop(self) -> None:
        review_driver_mod._reset_interrupt_state()
        try:
            review_driver_mod._sigint_handler(2, None)
            assert review_driver_mod._interrupt_requested is True
            assert review_driver_mod._force_stop_requested is False
            review_driver_mod._sigint_handler(2, None)
            assert review_driver_mod._force_stop_requested is True
        finally:
            review_driver_mod._reset_interrupt_state()

    def test_ocr_popen_starts_new_session(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
        fake = _FakePopen(stdout='{"comments":[]}\n', stderr="")
        with patch("deep_architect.review_driver.subprocess.Popen", return_value=fake) as mocked:
            rc = run_ocr_subprocess(
                source="feat",
                target="main",
                output_json=output_json,
                exclude=[],
                cwd=tmp_path,
                ocr_bin="ocr",
            )
        assert rc == 0
        assert mocked.call_args.kwargs.get("start_new_session") is True

    def test_analyzer_redirects_stdout_to_on_child_log(self, tmp_path: Path) -> None:
        ocr_json = tmp_path / "code-review-r1.json"
        feedback = tmp_path / "feedback-r1"
        seen: list[str] = []

        def _fake_main(argv: list[str]) -> int:
            del argv
            print("  Processed 5/29 findings...")
            return 0

        with patch("deep_architect.review_analyzer.main", _fake_main):
            rc = run_analyzer_main(
                ocr_json=ocr_json,
                feedback_dir=feedback,
                prior_feedback=[],
                knowledge_dir=None,
                exclude=[],
                output_dir=tmp_path,
                on_child_log=seen.append,
            )
        assert rc == 0
        assert any("Processed 5/29 findings..." in chunk for chunk in seen)
        log_text = (tmp_path / "logs" / "r1-analyzer.log").read_text(encoding="utf-8")
        assert "Processed 5/29 findings..." in log_text

    def test_analyzer_argv_has_no_tui_and_prior(self, tmp_path: Path) -> None:
        ocr_json = tmp_path / "code-review-r2.json"
        feedback = tmp_path / "feedback-r2"
        prior = tmp_path / "feedback-r1"
        prior.mkdir()
        with patch(
            "deep_architect.review_analyzer.main", return_value=0
        ) as mocked:
            rc = run_analyzer_main(
                ocr_json=ocr_json,
                feedback_dir=feedback,
                prior_feedback=[prior],
                knowledge_dir=tmp_path / "knowledge",
                exclude=["*.gen.ts"],
                output_dir=tmp_path,
            )
        assert rc == 0
        argv = mocked.call_args[0][0]
        assert "--no-tui" in argv
        assert str(ocr_json) in argv
        assert argv[argv.index("--output-dir") + 1] == str(feedback)
        assert argv[argv.index("--prior-feedback") + 1] == str(prior)
        assert argv[argv.index("--exclude") + 1] == "*.gen.ts"
        assert argv[argv.index("--knowledge-dir") + 1] == str(tmp_path / "knowledge")

    def test_action_argv_has_no_tui_and_min_severity(self, tmp_path: Path) -> None:
        feedback = tmp_path / "feedback-r1"
        feedback.mkdir()
        with patch(
            "deep_architect.review_action_harness.main", return_value=0
        ) as mocked:
            rc = run_action_main(feedback_dir=feedback, output_dir=tmp_path)
        assert rc == 0
        argv = mocked.call_args[0][0]
        assert "--no-tui" in argv
        assert "--tui" not in argv
        assert argv[argv.index("--min-severity") + 1] == "medium"
        assert str(feedback) in argv


class TestInstallSigintHandler:
    def test_worker_thread_does_not_raise(self) -> None:
        from deep_architect.review_driver import _install_sigint_handler

        errors: list[BaseException] = []

        def _run() -> None:
            try:
                _install_sigint_handler()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()
        assert errors == []

    def test_run_analyzer_main_from_worker_thread(self, tmp_path: Path) -> None:
        ocr_json = tmp_path / "code-review-r1.json"
        ocr_json.write_text(
            json.dumps({"status": "success", "comments": [], "warnings": []}),
            encoding="utf-8",
        )
        errors: list[BaseException] = []
        codes: list[int] = []

        def _run() -> None:
            try:
                codes.append(
                    run_analyzer_main(
                        ocr_json=ocr_json,
                        feedback_dir=tmp_path / "feedback-r1",
                        prior_feedback=[],
                        knowledge_dir=tmp_path / "knowledge",
                        exclude=[],
                        output_dir=tmp_path,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()
        assert errors == []
        assert codes == [0]


class TestOcrSessionHelpers:
    def test_session_dir_slug(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        repo = tmp_path / "repos" / "app"
        repo.mkdir(parents=True)
        expected_slug = str(repo.resolve()).lstrip("/").replace("/", "-")
        got = ocr_session_dir(repo)
        assert got == tmp_path / "home" / ".opencodereview" / "sessions" / expected_slug

    def test_format_session_start(self) -> None:
        line = format_ocr_session_event(
            {
                "type": "session_start",
                "sessionId": "abc",
                "diffFrom": "main",
                "diffTo": "feat",
            }
        )
        assert line == "[ocr] session abc main..feat"

    def test_format_first_llm_request_only(self) -> None:
        event = {
            "type": "llm_request",
            "filePath": "src/a.py",
            "taskType": "plan_task",
            "messages": [{"role": "system", "content": "SECRET PROMPT"}],
        }
        first = format_ocr_session_event(event, first_request_for_file=True)
        again = format_ocr_session_event(event, first_request_for_file=False)
        assert first == "[ocr] reviewing src/a.py (plan_task)"
        assert again is None
        assert first is not None and "SECRET" not in first

    def test_format_done_failed_llm_error(self) -> None:
        assert (
            format_ocr_session_event({"type": "review_item_done", "filePath": "src/a.py"})
            == "[ocr] done src/a.py"
        )
        assert (
            format_ocr_session_event(
                {
                    "type": "review_item_failed",
                    "filePath": "src/b.py",
                    "error": "context deadline exceeded",
                }
            )
            == "[ocr] failed src/b.py: context deadline exceeded"
        )
        assert (
            format_ocr_session_event(
                {
                    "type": "llm_error",
                    "filePath": "src/b.py",
                    "error": "context deadline exceeded",
                }
            )
            == "[ocr] llm error src/b.py: context deadline exceeded"
        )

    def test_format_skips_noisy_types(self) -> None:
        assert format_ocr_session_event({"type": "tool_call", "filePath": "src/a.py"}) is None
        assert format_ocr_session_event({"type": "llm_response", "filePath": "src/a.py"}) is None


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


class TestParseArgs:
    def test_source_required_target_and_output_defaults(self) -> None:
        args = parse_args(["--source", "feat"])
        assert args.source == "feat"
        assert args.target == "main"
        assert args.output_dir == DEFAULT_OUTPUT_DIR
        assert args.resume is True

    def test_resume_flags(self) -> None:
        args = parse_args(["--source", "feat"])
        assert args.resume is True
        args = parse_args(["--source", "feat", "--resume"])
        assert args.resume is True
        args = parse_args(["--source", "feat", "--no-resume"])
        assert args.resume is False

    def test_missing_source_exits(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])

    def test_output_dir_override(self) -> None:
        args = parse_args(["--source", "feat", "--output-dir", "other/"])
        assert args.output_dir == Path("other/")

    def test_tui_flags(self) -> None:
        args = parse_args(["--source", "feat", "--tui"])
        assert args.tui is True
        assert args.no_tui is False
        args = parse_args(["--source", "feat", "--no-tui"])
        assert args.tui is False
        assert args.no_tui is True

    def test_tui_mutual_exclusion(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--source", "feat", "--tui", "--no-tui"])

    def test_max_passes_zero_is_accepted(self) -> None:
        args = parse_args(["--source", "feat", "--max-passes", "0"])
        assert args.max_passes == 0

    def test_ocr_timeout_and_concurrency_flags(self) -> None:
        args = parse_args(
            [
                "--source",
                "feat",
                "--ocr-timeout",
                "30",
                "--ocr-concurrency",
                "3",
                "--ocr-llm-timeout",
                "1200",
            ]
        )
        assert args.ocr_timeout == 30
        assert args.ocr_concurrency == 3
        assert args.ocr_llm_timeout == 1200


class TestResolveOcrLimits:
    def test_cli_wins(self) -> None:
        cfg = HarnessConfig()
        assert resolve_ocr_timeout_minutes(30, cfg) == 30
        assert resolve_ocr_concurrency(3, cfg) == 3
        assert resolve_ocr_llm_timeout_seconds(1200, cfg) == 1200

    def test_config_when_cli_unset(self) -> None:
        cfg = HarnessConfig()
        cfg.thresholds.review_driver_ocr_timeout_minutes = 30
        cfg.thresholds.review_driver_ocr_concurrency = 3
        cfg.thresholds.review_driver_ocr_llm_timeout_seconds = 1200
        assert resolve_ocr_timeout_minutes(None, cfg) == 30
        assert resolve_ocr_concurrency(None, cfg) == 3
        assert resolve_ocr_llm_timeout_seconds(None, cfg) == 1200

    def test_env_overrides_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIEW_DRIVER_OCR_FILE_TIMEOUT", "25")
        monkeypatch.setenv("REVIEW_DRIVER_OCR_CONCURRENCY", "4")
        monkeypatch.setenv("REVIEW_DRIVER_OCR_LLM_TIMEOUT", "900")
        cfg = HarnessConfig()
        cfg.thresholds.review_driver_ocr_timeout_minutes = 30
        cfg.thresholds.review_driver_ocr_concurrency = 3
        cfg.thresholds.review_driver_ocr_llm_timeout_seconds = 1200
        assert resolve_ocr_timeout_minutes(None, cfg) == 25
        assert resolve_ocr_concurrency(None, cfg) == 4
        assert resolve_ocr_llm_timeout_seconds(None, cfg) == 900

    def test_llm_timeout_zero_is_valid(self) -> None:
        cfg = HarnessConfig()
        assert resolve_ocr_llm_timeout_seconds(0, cfg) == 0
        assert resolve_ocr_llm_timeout_seconds(None, cfg) == 0


class TestOcrProcessTimeout:
    def test_derived_timeout_45m_concurrency_2(self) -> None:
        assert ocr_process_timeout_seconds(
            file_timeout_minutes=45, concurrency=2
        ) == 21720.0

    def test_derived_timeout_default_knobs_under_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REVIEW_DRIVER_OCR_TIMEOUT", raising=False)
        derived = ocr_process_timeout_seconds(
            file_timeout_minutes=10, concurrency=8
        )
        assert derived == 1320.0
        assert review_driver_mod._ocr_timeout_seconds(10, 8) == 3600.0

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIEW_DRIVER_OCR_TIMEOUT", "99")
        assert review_driver_mod._ocr_timeout_seconds(45, 2) == 99.0

    def test_invalid_env_falls_through_to_derived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_DRIVER_OCR_TIMEOUT", "nope")
        assert review_driver_mod._ocr_timeout_seconds(45, 2) == 21720.0


class TestOcrLogScoping:
    def test_last_invocation_slices_from_final_banner(self) -> None:
        text = (
            "OCR starting: old\n"
            "[ocr] llm error a.py: context deadline exceeded\n"
            "OCR starting: new\n"
            "ocr timed out after 3600 seconds\n"
        )
        block = last_ocr_invocation_log(text)
        assert block.startswith("OCR starting: new")
        assert "deadline exceeded" not in block
        assert "ocr timed out after 3600 seconds" in block

    def test_process_timeout_reason_from_log(self) -> None:
        log = "OCR starting: x\nocr timed out after 3600.0 seconds\n"
        assert ocr_process_timeout_reason(log) == "ocr process timeout after 1h00m00s"

    def test_default_ocr_report_excludes(self) -> None:
        assert default_ocr_report_excludes() == [
            "code-review*.json",
            "code-review-*.json",
        ]

    def test_driver_kill_ignores_stale_json_and_old_deadline(
        self, tmp_path: Path
    ) -> None:
        ocr_json = tmp_path / "code-review-r1.json"
        ocr_json.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "message": "context deadline exceeded",
                    "summary": {"comments": 0, "files_reviewed": 16},
                }
            ),
            encoding="utf-8",
        )
        log = tmp_path / "logs" / "r1-ocr.log"
        log.parent.mkdir()
        log.write_text(
            "OCR starting: first attempt\n"
            "Error: review failed: context deadline exceeded\n"
            "OCR starting: retry\n"
            "ocr timed out after 3600 seconds\n",
            encoding="utf-8",
        )
        runners = ScriptedRunners(novelties=[1], ocr_rcs=[1])
        result = _run(tmp_path, runners, max_passes=1, k=2)
        assert result.status == "failed"
        assert result.stop_detail is not None
        assert "process timeout" in result.stop_detail
        assert "deadline" not in result.stop_detail


def _stamp(hour: int = 14, minute: int = 30, second: int = 22) -> datetime:
    return datetime(2026, 8, 19, hour, minute, second, tzinfo=UTC)


def _seed_progress(
    run_dir: Path,
    *,
    status: str = "running",
    source: str = "feat",
    target: str = "main",
    source_sha: str = "aaa",
    target_sha: str = "bbb",
) -> DriverProgress:
    progress = DriverProgress(
        status=status,  # type: ignore[arg-type]
        source=source,
        target=target,
        source_sha=source_sha,
        target_sha=target_sha,
        max_passes=5,
        k=2,
        output_dir=str(run_dir),
    )
    save_driver_progress(run_dir, progress)
    return progress


class TestSanitizeRunSlug:
    def test_slashes_become_dashes(self) -> None:
        assert sanitize_run_slug("feature/foo") == "feature-foo"

    def test_punctuation_collapsed(self) -> None:
        assert sanitize_run_slug("feat: x") == "feat-x"

    def test_empty_falls_back_then_unnamed(self) -> None:
        assert sanitize_run_slug("") == "unnamed"
        assert sanitize_run_slug("...") == "unnamed"
        assert sanitize_run_slug("...", fallback="0123456789abcdef") == "0123456789ab"

    def test_branch_parent_omits_default_target(self, tmp_path: Path) -> None:
        assert branch_run_parent(tmp_path, "PROJ-0013", "main").name == "PROJ-0013"

    def test_branch_parent_includes_non_default_target(self, tmp_path: Path) -> None:
        parent = branch_run_parent(tmp_path, "feat", "develop")
        assert parent.name == "feat__develop"

    def test_branch_parent_uses_sha_fallback(self, tmp_path: Path) -> None:
        parent = branch_run_parent(
            tmp_path, "...", "main", source_sha="deadbeefcafebabe"
        )
        assert parent.name == "deadbeefcafe"


class TestDefaultOutputExcludes:
    def test_relative_root_under_cwd(self, tmp_path: Path) -> None:
        assert default_output_excludes(Path(".review-runs"), tmp_path) == [
            ".review-runs/**"
        ]

    def test_skips_when_root_is_cwd(self, tmp_path: Path) -> None:
        assert default_output_excludes(tmp_path, tmp_path) == []

    def test_skips_when_root_outside_cwd(self, tmp_path: Path) -> None:
        other = tmp_path / "outside"
        other.mkdir()
        cwd = tmp_path / "repo"
        cwd.mkdir()
        assert default_output_excludes(other, cwd) == []


class TestResolveDriverRunDir:
    def test_first_run_creates_nested_timestamp_dir(self, tmp_path: Path) -> None:
        now = _stamp()
        run_dir, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="PROJ-0013",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            resume=True,
            now=now,
        )
        assert is_resume is False
        assert run_dir == tmp_path / "PROJ-0013" / "20260819T143022Z"
        assert run_dir.is_dir()
        assert (tmp_path / "PROJ-0013" / "LATEST").read_text(
            encoding="utf-8"
        ) == "20260819T143022Z\n"

    def test_no_resume_creates_sibling_and_preserves_bytes(
        self, tmp_path: Path
    ) -> None:
        first, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        artifact = first / "code-review-r1.json"
        artifact.write_text("keep-me", encoding="utf-8")
        _seed_progress(first, status="failed")

        second, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=False,
            now=_stamp(hour=18),
        )
        assert is_resume is False
        assert second != first
        assert second == tmp_path / "feat" / "20260819T183022Z"
        assert artifact.read_text(encoding="utf-8") == "keep-me"

    def test_resume_failed_returns_same_path(self, tmp_path: Path) -> None:
        run_dir, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        _seed_progress(run_dir, status="failed", source_sha="aaa")
        again, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(hour=18),
        )
        assert is_resume is True
        assert again == run_dir

    def test_resume_running_returns_same_path(self, tmp_path: Path) -> None:
        run_dir, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        _seed_progress(run_dir, status="running")
        again, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(hour=18),
        )
        assert is_resume is True
        assert again == run_dir

    def test_terminal_same_sha_reuses_run(self, tmp_path: Path) -> None:
        run_dir, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        _seed_progress(run_dir, status="converged", source_sha="aaa")
        again, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(hour=18),
        )
        assert is_resume is True
        assert again == run_dir

    def test_terminal_different_source_sha_starts_new_run(
        self, tmp_path: Path
    ) -> None:
        first, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        artifact = first / "code-review-r1.json"
        artifact.write_text("original", encoding="utf-8")
        _seed_progress(first, status="converged", source_sha="aaa")

        second, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="ccc",
            resume=True,
            now=_stamp(hour=18),
        )
        assert is_resume is False
        assert second != first
        assert artifact.read_text(encoding="utf-8") == "original"

    def test_non_default_target_uses_combined_slug(self, tmp_path: Path) -> None:
        run_dir, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="develop",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        assert run_dir.parent.name == "feat__develop"

    def test_legacy_flat_progress_resumes_in_place(self, tmp_path: Path) -> None:
        _seed_progress(tmp_path, status="running")
        run_dir, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        assert run_dir == tmp_path
        assert is_resume is True

    def test_legacy_no_resume_creates_nested_sibling(self, tmp_path: Path) -> None:
        artifact = tmp_path / "code-review-r1.json"
        artifact.write_text("legacy", encoding="utf-8")
        _seed_progress(tmp_path, status="running")
        run_dir, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=False,
            now=_stamp(),
        )
        assert is_resume is False
        assert run_dir == tmp_path / "feat" / "20260819T143022Z"
        assert artifact.read_text(encoding="utf-8") == "legacy"
        assert not (run_dir / "code-review-r1.json").exists()

    def test_legacy_mismatch_returns_root(self, tmp_path: Path) -> None:
        _seed_progress(tmp_path, source="feat", status="running")
        run_dir, is_resume = resolve_driver_run_dir(
            tmp_path,
            source="other",
            target="main",
            source_sha="aaa",
            resume=True,
            now=_stamp(),
        )
        assert run_dir == tmp_path
        assert is_resume is True

    def test_timestamp_collision_suffixes(self, tmp_path: Path) -> None:
        first, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=False,
            now=_stamp(),
        )
        second, _ = resolve_driver_run_dir(
            tmp_path,
            source="feat",
            target="main",
            source_sha="aaa",
            resume=False,
            now=_stamp(),
        )
        assert first == tmp_path / "feat" / "20260819T143022Z"
        assert second == tmp_path / "feat" / "20260819T143022Z-2"


class TestMain:
    def test_resume_threaded_and_converged_exit_0(self, tmp_path: Path) -> None:
        progress = DriverProgress(
            status="converged",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
        )
        with (
            patch(
                "deep_architect.review_driver.preflight_driver",
                return_value=(None, "aaa", "bbb"),
            ),
            patch("deep_architect.review_driver.run_driver", return_value=progress) as mocked,
        ):
            rc = main(
                [
                    "--source",
                    "feat",
                    "--resume",
                    "--output-dir",
                    str(tmp_path),
                    "--no-tui",
                ]
            )
        assert rc == 0
        assert mocked.call_args.kwargs["resume"] is True

    def test_default_resume_threaded(self, tmp_path: Path) -> None:
        progress = DriverProgress(
            status="converged",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
        )
        with (
            patch(
                "deep_architect.review_driver.preflight_driver",
                return_value=(None, "aaa", "bbb"),
            ),
            patch("deep_architect.review_driver.run_driver", return_value=progress) as mocked,
        ):
            rc = main(["--source", "feat", "--output-dir", str(tmp_path), "--no-tui"])
        assert rc == 0
        assert mocked.call_args.kwargs["resume"] is True

    def test_no_resume_threaded(self, tmp_path: Path) -> None:
        progress = DriverProgress(
            status="converged",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
        )
        with (
            patch(
                "deep_architect.review_driver.preflight_driver",
                return_value=(None, "aaa", "bbb"),
            ),
            patch("deep_architect.review_driver.run_driver", return_value=progress) as mocked,
        ):
            rc = main(
                [
                    "--source",
                    "feat",
                    "--no-resume",
                    "--output-dir",
                    str(tmp_path),
                    "--no-tui",
                ]
            )
        assert rc == 0
        assert mocked.call_args.kwargs["resume"] is False

    def test_max_passes_negative_exits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        del tmp_path
        rc = main(["--source", "feat", "--max-passes", "-1", "--no-tui"])
        assert rc == 1
        assert "must be >= 0" in capsys.readouterr().err

    def test_max_passes_exit_1(self, tmp_path: Path) -> None:
        progress = DriverProgress(
            status="max_passes",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=3,
            k=2,
            novelty_history=[1, 1, 1],
            output_dir=str(tmp_path),
        )
        with (
            patch(
                "deep_architect.review_driver.preflight_driver",
                return_value=(None, "aaa", "bbb"),
            ),
            patch("deep_architect.review_driver.run_driver", return_value=progress),
        ):
            rc = main(
                ["--source", "feat", "--output-dir", str(tmp_path), "--no-tui"]
            )
        assert rc == 1

    def test_nests_run_dir_under_source_slug(self, tmp_path: Path) -> None:
        progress = DriverProgress(
            status="converged",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=str(tmp_path),
        )
        with (
            patch(
                "deep_architect.review_driver.preflight_driver",
                return_value=(None, "aaa", "bbb"),
            ),
            patch("deep_architect.review_driver.run_driver", return_value=progress) as mocked,
        ):
            rc = main(
                ["--source", "feat", "--output-dir", str(tmp_path), "--no-tui"]
            )
        assert rc == 0
        out = mocked.call_args.kwargs["output_dir"]
        assert out.parent.name == "feat"
        assert out.parent.parent == tmp_path
        assert (tmp_path / "feat" / "LATEST").is_file()

    def test_merges_default_output_exclude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        progress = DriverProgress(
            status="converged",
            source="feat",
            target="main",
            source_sha="aaa",
            target_sha="bbb",
            max_passes=5,
            k=2,
            output_dir=".review-runs",
        )
        with (
            patch(
                "deep_architect.review_driver.preflight_driver",
                return_value=(None, "aaa", "bbb"),
            ),
            patch("deep_architect.review_driver.run_driver", return_value=progress) as mocked,
        ):
            rc = main(
                [
                    "--source",
                    "feat",
                    "--output-dir",
                    ".review-runs",
                    "--exclude",
                    "vendor/**",
                    "--no-tui",
                ]
            )
        assert rc == 0
        assert mocked.call_args.kwargs["exclude"] == [
            ".review-runs/**",
            "code-review*.json",
            "code-review-*.json",
            "vendor/**",
        ]
