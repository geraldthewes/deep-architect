"""Tests for retry behaviour in the run_harness() round loop.

All expensive I/O and LLM calls are mocked so the tests are fast and
deterministic.  We focus on verifying that:
  - a failing generator is retried and the round succeeds on the second attempt
  - a failing critic is retried and the round succeeds on the second attempt
  - exhausting all retries marks the sprint (and run) as failed
  - the generator session_id is reset to None between retry attempts
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import git
import pytest

from deep_researcher.config import AgentConfig, HarnessConfig, ThresholdConfig
from deep_researcher.harness import run_harness
from deep_researcher.models.contract import SprintContract, SprintCriterion
from deep_researcher.models.feedback import CriterionScore, CriticResult

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


# Patches applied to every harness test so we don't touch the network or file system.
_INFRA_PATCHES = [
    patch("deep_researcher.harness.run_preflight_check", new_callable=AsyncMock),
    patch("deep_researcher.harness.run_final_agreement", new_callable=AsyncMock),
    patch("deep_researcher.harness.validate_git_repo", return_value=MagicMock()),
    patch("deep_researcher.harness.get_modified_files", return_value=[]),
    patch("deep_researcher.harness.git_commit"),
    patch("deep_researcher.harness.setup_logging", return_value=Path("/tmp/test.log")),
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
        return None  # session_id

    with (
        patch("deep_researcher.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_researcher.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_researcher.harness.validate_git_repo", return_value=MagicMock()),
        patch("deep_researcher.harness.get_modified_files", return_value=[]),
        patch("deep_researcher.harness.git_commit"),
        patch("deep_researcher.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_researcher.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_researcher.harness.run_generator",
            side_effect=_generator_fail_then_succeed,
        ),
        patch(
            "deep_researcher.harness.run_critic",
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
        patch("deep_researcher.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_researcher.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_researcher.harness.validate_git_repo", return_value=MagicMock()),
        patch("deep_researcher.harness.get_modified_files", return_value=[]),
        patch("deep_researcher.harness.git_commit"),
        patch("deep_researcher.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_researcher.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_researcher.harness.run_generator",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "deep_researcher.harness.run_critic",
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
        patch("deep_researcher.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_researcher.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_researcher.harness.validate_git_repo", return_value=MagicMock()),
        patch("deep_researcher.harness.get_modified_files", return_value=[]),
        patch("deep_researcher.harness.git_commit"),
        patch("deep_researcher.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_researcher.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_researcher.harness.run_generator",
            side_effect=_always_fail,
        ),
        patch(
            "deep_researcher.harness.run_critic",
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
    # Progress file should reflect failure
    progress_file = output_dir / "progress.json"
    assert progress_file.exists()
    progress_json = progress_file.read_text()
    assert '"failed"' in progress_json


async def test_harness_resets_generator_session_on_retry(
    output_dir: Path,
) -> None:
    """After a generator failure, the next attempt must receive session_id=None
    so the corrupted session is discarded."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    captured_session_ids: list[str | None] = []

    async def _record_and_fail_then_succeed(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured_session_ids.append(kwargs.get("session_id"))
        if len(captured_session_ids) == 1:
            raise Exception("Generator CLI crashed")
        return "new-session-id"

    with (
        patch("deep_researcher.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_researcher.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_researcher.harness.validate_git_repo", return_value=MagicMock()),
        patch("deep_researcher.harness.get_modified_files", return_value=[]),
        patch("deep_researcher.harness.git_commit"),
        patch("deep_researcher.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_researcher.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch(
            "deep_researcher.harness.run_generator",
            side_effect=_record_and_fail_then_succeed,
        ),
        patch(
            "deep_researcher.harness.run_critic",
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

    # First call: session_id is None (first round, no prior session)
    assert captured_session_ids[0] is None
    # Retry call: session_id must be None (reset after crash)
    assert captured_session_ids[1] is None
