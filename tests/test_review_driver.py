"""Unit tests for the review-driver loop, progress I/O, and formatters."""

from __future__ import annotations

import io
import json
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import git
import pytest

from deep_architect.review_driver import (
    DEFAULT_OUTPUT_DIR,
    DriverPassRecord,
    DriverPreflightError,
    DriverProgress,
    format_ocr_summary,
    format_stop_line,
    format_trend,
    load_driver_progress,
    main,
    parse_args,
    preflight_driver,
    run_action_main,
    run_analyzer_main,
    run_driver,
    run_ocr_subprocess,
    save_driver_progress,
    should_use_tui,
    write_driver_report,
)
from deep_architect.review_novelty import OcrRunStats


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
    resume: bool = False,
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
        result = _run(tmp_path, runners, resume=True)
        ocr_names = [p.name for p in runners.ocr_outputs]
        assert "code-review-r1.json" not in ocr_names
        assert ocr_names[0] == "code-review-r2.json"
        assert runners.analyzer_priors[0] == [tmp_path / "feedback-r1"]
        assert result.novelty_history[0] == 3
        assert result.current_pass >= 2

    def test_resume_missing_progress_fails_fast(self, tmp_path: Path) -> None:
        runners = ScriptedRunners(novelties=[0])
        with pytest.raises(FileNotFoundError, match="Omit --resume"):
            _run(tmp_path, runners, resume=True)
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
        self.kill = MagicMock()

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
        assert argv[argv.index("--exclude") + 1] == "**/generated/*"
        assert output_json.read_text(encoding="utf-8") == '{"comments":[]}\n'

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

    def test_ocr_timeout_returns_1(self, tmp_path: Path) -> None:
        output_json = tmp_path / "code-review-r1.json"
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
