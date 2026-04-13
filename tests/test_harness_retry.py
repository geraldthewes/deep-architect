"""Tests for retry behaviour in the run_harness() round loop.

All expensive I/O and LLM calls are mocked so the tests are fast and
deterministic.  We focus on verifying that:
  - a failing generator is retried and the round succeeds on the second attempt
  - a failing critic is retried and the round succeeds on the second attempt
  - exhausting all retries marks the sprint (and run) as failed
  - run_generator() has no session_id parameter (sessions are per-turn only)
  - --resume fails fast when no checkpoint exists
  - --resume skips already-passed sprints
  - --resume mid-sprint starts from the correct round
  - soft-fail (strict=False) accepts best-effort result and continues when max rounds exhausted
  - strict=True halts the run on sprint failure
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import git
import pytest

from deep_architect.agents.generator import GeneratorRoundResult
from deep_architect.config import AgentConfig, HarnessConfig, ThresholdConfig
from deep_architect.harness import run_harness
from deep_architect.models.contract import SprintContract, SprintCriterion
from deep_architect.models.feedback import CriterionScore, CriticResult

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_config(*, max_round_retries: int = 1) -> HarnessConfig:
    """Minimal config: 1 round wins a sprint (fast exit), 7 sprints available."""
    return HarnessConfig(
        generator=AgentConfig(model="test-model", max_turns=1, max_agent_retries=0),
        critic=AgentConfig(model="test-model", max_turns=1, max_agent_retries=0),
        thresholds=ThresholdConfig(
            min_score=9.0,
            consecutive_passing_rounds=1,
            max_rounds_per_sprint=3,
            max_total_rounds=30,
            timeout_hours=1.0,
            max_round_retries=max_round_retries,
        ),
    )


def _make_contract(sprint_number: int = 1) -> SprintContract:
    return SprintContract(
        sprint_number=sprint_number,
        sprint_name="Test Sprint",
        files_to_produce=["test.md"],
        criteria=[
            SprintCriterion(name="a", description="A criterion"),
            SprintCriterion(name="b", description="B criterion"),
            SprintCriterion(name="c", description="C criterion"),
        ],
    )


def _passing_result() -> CriticResult:
    return CriticResult(
        scores={"a": 9.5, "b": 9.5, "c": 9.5},
        feedback=[
            CriterionScore(criterion="a", score=9.5, severity="Low", details="ok"),
            CriterionScore(criterion="b", score=9.5, severity="Low", details="ok"),
            CriterionScore(criterion="c", score=9.5, severity="Low", details="ok"),
        ],
        overall_summary="All good",
    )


def _failing_result() -> CriticResult:
    return CriticResult(
        scores={"a": 7.0, "b": 7.0, "c": 7.0},
        feedback=[
            CriterionScore(criterion="a", score=7.0, severity="Low", details="needs work"),
            CriterionScore(criterion="b", score=7.0, severity="Low", details="needs work"),
            CriterionScore(criterion="c", score=7.0, severity="Low", details="needs work"),
        ],
        overall_summary="Below threshold",
    )


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    """Minimal git repo in a temp directory."""
    repo = git.Repo.init(str(tmp_path))
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@test.com").release()
    (tmp_path / "README.md").write_text("test")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit")
    return tmp_path


def _make_mock_repo(output_dir: Path) -> MagicMock:
    """Mock git.Repo with working_tree_dir set so checkpoint_dir resolves correctly."""
    mock_repo = MagicMock()
    mock_repo.working_tree_dir = str(output_dir)
    return mock_repo


# Patches applied to every harness test so we don't touch the network or file system.
_INFRA_PATCHES = [
    patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
    patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
    patch("deep_architect.harness.get_modified_files", return_value=[]),
    patch("deep_architect.harness.git_commit"),
    patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_harness_retries_round_on_generator_failure(
    output_dir: Path,
) -> None:
    """When the generator fails on the first attempt, the round is retried and
    completes successfully on the second attempt."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    gen_call_count = 0

    async def _generator_fail_then_succeed(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal gen_call_count
        gen_call_count += 1
        if gen_call_count == 1:
            raise Exception("Generator CLI crashed")
        return GeneratorRoundResult(session_id=None, input_tokens=0)

    with (
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_architect.harness.run_generator",
            side_effect=_generator_fail_then_succeed,
        ),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config(max_round_retries=1),
        )

    # First sprint round 1 required 2 generator calls (fail + retry)
    assert gen_call_count >= 2


async def test_harness_retries_round_on_critic_failure(
    output_dir: Path,
) -> None:
    """When the critic fails on the first attempt, the round is retried."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    critic_call_count = 0

    async def _critic_fail_then_succeed(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal critic_call_count
        critic_call_count += 1
        if critic_call_count == 1:
            raise Exception("Critic CLI crashed")
        return _passing_result()

    with (
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_architect.harness.run_generator",
            new_callable=AsyncMock,
            return_value=GeneratorRoundResult(session_id=None, input_tokens=0),
        ),
        patch(
            "deep_architect.harness.run_critic",
            side_effect=_critic_fail_then_succeed,
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config(max_round_retries=1),
        )

    assert critic_call_count >= 2


async def test_harness_marks_sprint_failed_after_max_retries(
    output_dir: Path,
) -> None:
    """When all retry attempts for a round are exhausted, the sprint is marked
    failed and the harness returns without completing the run."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    gen_call_count = 0

    async def _always_fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal gen_call_count
        gen_call_count += 1
        raise Exception("Generator always fails")

    with (
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_architect.harness.run_generator",
            side_effect=_always_fail,
        ),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config(max_round_retries=1),
        )

    # 1 initial + 1 retry = 2 attempts before failing
    assert gen_call_count == 2
    # Progress file should reflect failure (now lives in .checkpoints/)
    progress_file = output_dir / ".checkpoints" / "progress.json"
    assert progress_file.exists()
    progress_json = progress_file.read_text()
    assert '"failed"' in progress_json


async def test_harness_generator_receives_no_session_id(
    output_dir: Path,
) -> None:
    """Each generator call starts a new session — session_id is not a parameter."""
    import inspect

    from deep_architect.agents.generator import run_generator
    sig = inspect.signature(run_generator)
    assert "session_id" not in sig.parameters, (
        "run_generator() must not have a session_id parameter — sessions are per-turn only"
    )


async def test_harness_runs_multiple_rounds_stateless(
    output_dir: Path,
) -> None:
    """Harness completes two rounds without session threading."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")
    round_count = 0

    async def _fake_generator(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal round_count
        round_count += 1
        return GeneratorRoundResult(session_id="sdk-session", input_tokens=0)

    with (
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch("deep_architect.harness.run_generator", side_effect=_fake_generator),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config(max_round_retries=0),
        )

    assert round_count >= 1


async def test_resume_fails_fast_without_checkpoint(output_dir: Path) -> None:
    """--resume with no .checkpoints/progress.json raises FileNotFoundError immediately."""
    prd = output_dir / "prd.md"
    prd.write_text("# PRD")

    with (
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        pytest.raises(FileNotFoundError, match="--resume passed but no checkpoint found"),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=True,
            config=_make_config(),
        )


async def test_resume_skips_completed_sprints(output_dir: Path) -> None:
    """--resume skips sprints already marked passed/failed in the checkpoint."""
    from deep_architect.io.files import save_progress
    from deep_architect.models.progress import HarnessProgress, SprintStatus
    from deep_architect.sprints import SPRINTS

    prd = output_dir / "prd.md"
    prd.write_text("# PRD")

    # Write a checkpoint with sprint 1 passed, sprint 2 pending
    checkpoint_dir = output_dir / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=len(SPRINTS),
        current_sprint=2,
        completed_sprints=1,
        sprint_statuses=[
            SprintStatus(
                sprint_number=s.number,
                sprint_name=s.name,
                status="passed" if s.number == 1 else "pending",
            )
            for s in SPRINTS
        ],
    )
    save_progress(checkpoint_dir, progress)

    negotiate_calls: list[int] = []

    async def _record_negotiate(*args: object, **kwargs: object) -> object:
        # args: generator_config, critic_config, sprint, prd_content, output_dir, ...
        sprint_arg = args[2]  # SprintDefinition
        negotiate_calls.append(sprint_arg.number)
        return _make_contract(sprint_number=sprint_arg.number)

    with (
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.negotiate_contract", side_effect=_record_negotiate),
        patch(
            "deep_architect.harness.run_generator",
            new_callable=AsyncMock,
            return_value=GeneratorRoundResult(session_id=None, input_tokens=0),
        ),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
        patch(
            "deep_architect.harness.check_ping_pong",
            new_callable=AsyncMock,
            return_value=MagicMock(similarity_score=0.0),
        ),
    ):
        await run_harness(prd=prd, output_dir=output_dir, resume=True, config=_make_config())

    assert 1 not in negotiate_calls, "Sprint 1 should have been skipped"
    assert 2 in negotiate_calls, "Sprint 2 should have been executed"


async def test_resume_mid_sprint_starts_from_correct_round(output_dir: Path) -> None:
    """--resume with rounds_completed=2 starts the generator at round 3
    and loads the contract from disk instead of re-negotiating."""
    from deep_architect.io.files import save_contract, save_feedback, save_progress
    from deep_architect.models.progress import HarnessProgress, SprintStatus
    from deep_architect.sprints import SPRINTS

    prd = output_dir / "prd.md"
    prd.write_text("# PRD")

    # Create required workspace directories
    for subdir in ("feedback", "contracts", "decisions", "logs"):
        (output_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Write the contract to disk so load_contract finds it
    save_contract(output_dir, _make_contract(sprint_number=1))

    # Write prior round-2 feedback so last_result can be restored
    save_feedback(output_dir, 1, 2, _passing_result())

    checkpoint_dir = output_dir / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=len(SPRINTS),
        current_sprint=1,
        sprint_statuses=[
            SprintStatus(
                sprint_number=s.number,
                sprint_name=s.name,
                status="building" if s.number == 1 else "pending",
                rounds_completed=2 if s.number == 1 else 0,
                consecutive_passes=1 if s.number == 1 else 0,
            )
            for s in SPRINTS
        ],
    )
    save_progress(checkpoint_dir, progress)

    generator_calls: list[tuple[int, int]] = []  # (sprint_number, round_num)
    negotiate_calls: list[int] = []  # sprint numbers that triggered negotiation

    async def _record_generator(*args: object, **kwargs: object) -> GeneratorRoundResult:
        # Positional signature: config, sprint, contract, prd, last_result, output_dir, round_num
        sprint_arg = args[1]
        round_num = args[6]
        generator_calls.append((sprint_arg.number, round_num))
        return GeneratorRoundResult(session_id=None, input_tokens=0)

    async def _record_negotiate(*args: object, **kwargs: object) -> SprintContract:
        sprint_arg = args[2]
        negotiate_calls.append(sprint_arg.number)
        return _make_contract(sprint_number=sprint_arg.number)

    with (
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.negotiate_contract", side_effect=_record_negotiate),
        patch("deep_architect.harness.run_generator", side_effect=_record_generator),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
        patch(
            "deep_architect.harness.check_ping_pong",
            new_callable=AsyncMock,
            return_value=MagicMock(similarity_score=0.0),
        ),
    ):
        await run_harness(prd=prd, output_dir=output_dir, resume=True, config=_make_config())

    # Sprint 1 should NOT have re-negotiated (loaded from disk instead)
    assert 1 not in negotiate_calls, (
        "Sprint 1 contract should have been loaded from disk, not re-negotiated"
    )
    # Subsequent sprints (fresh start) should still negotiate normally
    assert 2 in negotiate_calls, "Sprint 2 should have negotiated normally"

    sprint_1_rounds = [r for s, r in generator_calls if s == 1]
    assert sprint_1_rounds, "Generator should have been called for sprint 1"
    assert min(sprint_1_rounds) == 3, (
        f"Sprint 1 resume should start at round 3 (rounds_completed=2), "
        f"got rounds={sprint_1_rounds}"
    )
    assert 1 not in sprint_1_rounds and 2 not in sprint_1_rounds, (
        "Rounds 1 and 2 should have been skipped on resume for sprint 1"
    )


async def test_resume_resets_failed_status(output_dir: Path) -> None:
    """--resume with status='failed' resets progress to 'running' so the resumed
    run does not carry stale failure state and ends as 'complete' on success."""
    from deep_architect.io.files import load_progress, save_progress
    from deep_architect.models.progress import HarnessProgress, SprintStatus
    from deep_architect.sprints import SPRINTS

    prd = output_dir / "prd.md"
    prd.write_text("# PRD")

    checkpoint_dir = output_dir / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=len(SPRINTS),
        current_sprint=2,
        completed_sprints=1,
        status="failed",
        sprint_statuses=[
            SprintStatus(
                sprint_number=s.number,
                sprint_name=s.name,
                status="passed" if s.number == 1 else "pending",
            )
            for s in SPRINTS
        ],
    )
    save_progress(checkpoint_dir, progress)

    with (
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_architect.harness.run_generator",
            new_callable=AsyncMock,
            return_value=GeneratorRoundResult(session_id=None, input_tokens=0),
        ),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
        patch(
            "deep_architect.harness.check_ping_pong",
            new_callable=AsyncMock,
            return_value=MagicMock(similarity_score=0.0),
        ),
    ):
        await run_harness(prd=prd, output_dir=output_dir, resume=True, config=_make_config())

    reloaded = load_progress(checkpoint_dir)
    assert reloaded.status == "complete", (
        f"Expected status='complete' after successful resumed run, got '{reloaded.status}'"
    )


async def test_harness_creates_sprint_boundary_commit(output_dir: Path) -> None:
    """Sprint-boundary and final-completion commits appear in the git log
    when get_modified_files and git_commit run against a real repo (not mocked)."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    repo = git.Repo(str(output_dir))
    gen_call_count = 0

    async def _writing_generator(*args: object, **kwargs: object) -> GeneratorRoundResult:
        nonlocal gen_call_count
        gen_call_count += 1
        sprint_arg = args[1]  # SprintDefinition
        round_num = args[6]   # int
        # Write a file so get_modified_files detects a change for git_commit
        f = output_dir / f"sprint-{sprint_arg.number}-round-{round_num}.md"
        f.write_text(f"# Sprint {sprint_arg.number} Round {round_num}")
        return GeneratorRoundResult(session_id=None, input_tokens=0)

    with (
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch("deep_architect.harness.run_generator", side_effect=_writing_generator),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config(),
        )

    commit_messages = [c.message for c in repo.iter_commits()]
    assert any("Sprint 1 complete:" in m for m in commit_messages), (
        f"Expected sprint-boundary commit in log, got: {commit_messages}"
    )
    assert commit_messages[0].startswith("Architecture complete — all"), (
        f"Expected final completion commit as most recent, got: {commit_messages[0]!r}"
    )


async def test_resume_mid_sprint_falls_back_to_negotiate_on_missing_contract(
    output_dir: Path,
) -> None:
    """When --resume mid-sprint but the contract file is missing, fall back
    to re-negotiation instead of crashing."""
    from deep_architect.io.files import save_feedback, save_progress
    from deep_architect.models.progress import HarnessProgress, SprintStatus
    from deep_architect.sprints import SPRINTS

    prd = output_dir / "prd.md"
    prd.write_text("# PRD")

    # Create workspace dirs but do NOT write a contract file
    for subdir in ("feedback", "contracts", "decisions", "logs"):
        (output_dir / subdir).mkdir(parents=True, exist_ok=True)

    save_feedback(output_dir, 1, 2, _passing_result())

    checkpoint_dir = output_dir / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=len(SPRINTS),
        current_sprint=1,
        sprint_statuses=[
            SprintStatus(
                sprint_number=s.number,
                sprint_name=s.name,
                status="building" if s.number == 1 else "pending",
                rounds_completed=2 if s.number == 1 else 0,
                consecutive_passes=1 if s.number == 1 else 0,
            )
            for s in SPRINTS
        ],
    )
    save_progress(checkpoint_dir, progress)

    negotiate_calls: list[int] = []

    async def _record_negotiate(*args: object, **kwargs: object) -> SprintContract:
        sprint_arg = args[2]
        negotiate_calls.append(sprint_arg.number)
        return _make_contract(sprint_number=sprint_arg.number)

    with (
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.negotiate_contract", side_effect=_record_negotiate),
        patch(
            "deep_architect.harness.run_generator",
            new_callable=AsyncMock,
            return_value=GeneratorRoundResult(session_id=None, input_tokens=0),
        ),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
        patch(
            "deep_architect.harness.check_ping_pong",
            new_callable=AsyncMock,
            return_value=MagicMock(similarity_score=0.0),
        ),
    ):
        await run_harness(prd=prd, output_dir=output_dir, resume=True, config=_make_config())

    assert 1 in negotiate_calls, (
        "Sprint 1 should have fallen back to negotiate_contract when contract file is missing"
    )


def _make_config_one_round() -> HarnessConfig:
    """Config with max_rounds_per_sprint=1 so every sprint exhausts after one round."""
    return HarnessConfig(
        generator=AgentConfig(model="test-model", max_turns=1, max_agent_retries=0),
        critic=AgentConfig(model="test-model", max_turns=1, max_agent_retries=0),
        thresholds=ThresholdConfig(
            min_score=9.0,
            consecutive_passing_rounds=1,
            max_rounds_per_sprint=1,
            max_total_rounds=30,
            timeout_hours=1.0,
            max_round_retries=0,
        ),
    )


async def test_soft_fail_accepts_best_result_and_continues(output_dir: Path) -> None:
    """With strict=False (default), a sprint that exhausts max rounds without meeting
    exit criteria is accepted using best_result and the run continues to completion."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    mock_repo = _make_mock_repo(output_dir)
    mock_repo.head.commit.hexsha = "abc123deadbeef"

    with (
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.validate_git_repo", return_value=mock_repo),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.git_commit_staged"),
        patch("deep_architect.harness.restore_arch_files_from_commit", return_value=[]),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_architect.harness.run_generator",
            new_callable=AsyncMock,
            return_value=GeneratorRoundResult(session_id=None, input_tokens=0),
        ),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_failing_result(),
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config_one_round(),
            strict=False,
        )

    from deep_architect.io.files import load_progress

    progress_file = output_dir / ".checkpoints" / "progress.json"
    assert progress_file.exists()
    progress_json = progress_file.read_text()
    assert '"accepted"' in progress_json, "Sprint should be accepted when max rounds exhausted"
    assert '"complete"' in progress_json, "Run should complete all sprints in soft-fail mode"

    reloaded = load_progress(output_dir / ".checkpoints")
    sprint_1 = reloaded.sprint_statuses[0]
    assert sprint_1.best_round == 1, f"best_round should be 1, got {sprint_1.best_round}"
    assert sprint_1.best_scores is not None, "best_scores should be populated"
    assert all(v == 7.0 for v in sprint_1.best_scores.values()), (
        f"All best_scores should be 7.0, got {sprint_1.best_scores}"
    )


async def test_strict_mode_halts_on_sprint_failure(output_dir: Path) -> None:
    """With strict=True, a sprint that exhausts max rounds causes the run to stop
    and the progress file records a failed status."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    mock_repo = _make_mock_repo(output_dir)
    mock_repo.head.commit.hexsha = "abc123deadbeef"

    with (
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.validate_git_repo", return_value=mock_repo),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_architect.harness.run_generator",
            new_callable=AsyncMock,
            return_value=GeneratorRoundResult(session_id=None, input_tokens=0),
        ),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_failing_result(),
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config_one_round(),
            strict=True,
        )

    progress_file = output_dir / ".checkpoints" / "progress.json"
    assert progress_file.exists()
    progress_json = progress_file.read_text()
    assert '"failed"' in progress_json, "Run should be marked failed in strict mode"
    assert '"complete"' not in progress_json, "Run must not complete in strict mode"

