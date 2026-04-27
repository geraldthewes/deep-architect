from pathlib import Path

import pytest

from deep_architect.io.files import (
    append_critic_history,
    append_generator_history,
    clean_run_artifacts,
    init_workspace,
    load_contract,
    load_feedback,
    load_progress,
    reset_sprint_artifacts,
    save_contract,
    save_feedback,
    save_final_agreement,
    save_progress,
    save_round_log,
    write_index,
)
from deep_architect.models.contract import SprintContract, SprintCriterion
from deep_architect.models.feedback import CriterionScore, CriticResult
from deep_architect.models.progress import HarnessProgress, SprintStatus


def make_contract() -> SprintContract:
    return SprintContract(
        sprint_number=1,
        sprint_name="C1 Context",
        files_to_produce=["c1-context.md"],
        criteria=[
            SprintCriterion(name="c1", description="Mermaid diagram valid"),
            SprintCriterion(name="c2", description="C4 complete"),
            SprintCriterion(name="c3", description="Narrative quality"),
        ],
    )


def make_result() -> CriticResult:
    feedback = [
        CriterionScore(criterion="c1", score=9.5, severity="Low", details="ok"),
        CriterionScore(criterion="c2", score=9.0, severity="Medium", details="ok"),
        CriterionScore(criterion="c3", score=9.2, severity="Low", details="ok"),
    ]
    return CriticResult(
        scores={"c1": 9.5, "c2": 9.0, "c3": 9.2},
        feedback=feedback,
        overall_summary="good",
    )


def test_init_workspace(tmp_path: Path) -> None:
    init_workspace(tmp_path)
    assert (tmp_path / "contracts").is_dir()
    assert (tmp_path / "feedback").is_dir()
    assert (tmp_path / "decisions").is_dir()
    assert (tmp_path / "logs").is_dir()


def test_contract_round_trip(tmp_path: Path) -> None:
    init_workspace(tmp_path)
    contract = make_contract()
    save_contract(tmp_path, contract)
    loaded = load_contract(tmp_path, 1)
    assert loaded.sprint_number == contract.sprint_number
    assert loaded.sprint_name == contract.sprint_name
    assert len(loaded.criteria) == len(contract.criteria)
    assert loaded.criteria[0].name == contract.criteria[0].name


def test_feedback_round_trip(tmp_path: Path) -> None:
    init_workspace(tmp_path)
    result = make_result()
    save_feedback(tmp_path, 1, 1, result)
    loaded = load_feedback(tmp_path, 1, 1)
    assert abs(loaded.average_score - result.average_score) < 0.01
    assert loaded.passed == result.passed
    assert len(loaded.feedback) == len(result.feedback)


def test_progress_round_trip(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=7,
        sprint_statuses=[
            SprintStatus(sprint_number=i, sprint_name=f"Sprint {i}") for i in range(1, 8)
        ],
    )
    save_progress(checkpoint_dir, progress)
    loaded = load_progress(checkpoint_dir)
    assert loaded.total_sprints == 7
    assert loaded.current_sprint == 1
    assert len(loaded.sprint_statuses) == 7
    assert loaded.seed > 0


def test_save_progress_creates_checkpoint_dir(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / ".checkpoints"
    assert not checkpoint_dir.exists()
    progress = HarnessProgress(
        total_sprints=2,
        sprint_statuses=[
            SprintStatus(sprint_number=i, sprint_name=f"Sprint {i}") for i in range(1, 3)
        ],
    )
    save_progress(checkpoint_dir, progress)
    assert checkpoint_dir.exists()
    assert (checkpoint_dir / "progress.json").exists()


def test_save_progress_no_tmp_remnant(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=2,
        sprint_statuses=[
            SprintStatus(sprint_number=i, sprint_name=f"Sprint {i}") for i in range(1, 3)
        ],
    )
    save_progress(checkpoint_dir, progress)
    assert not (checkpoint_dir / "progress.tmp").exists()
    assert (checkpoint_dir / "progress.json").exists()


def test_progress_round_trip_with_consecutive_passes(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=2,
        sprint_statuses=[
            SprintStatus(
                sprint_number=1, sprint_name="Sprint 1",
                rounds_completed=3, consecutive_passes=1,
            ),
            SprintStatus(sprint_number=2, sprint_name="Sprint 2"),
        ],
    )
    save_progress(checkpoint_dir, progress)
    loaded = load_progress(checkpoint_dir)
    assert loaded.sprint_statuses[0].rounds_completed == 3
    assert loaded.sprint_statuses[0].consecutive_passes == 1
    assert loaded.sprint_statuses[1].consecutive_passes == 0


def test_round_log(tmp_path: Path) -> None:
    init_workspace(tmp_path)
    save_round_log(tmp_path, 1, 1, {"sprint": 1, "round": 1, "score": 9.0})
    log_path = tmp_path / "feedback" / "sprint-1-round-1-log.json"
    assert log_path.exists()
    import json
    data = json.loads(log_path.read_text())
    assert data["sprint"] == 1


def test_clean_run_artifacts_removes_checkpoint_contracts_feedback(tmp_path: Path) -> None:
    """clean_run_artifacts deletes checkpoint, contracts, and feedback files."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"

    # Populate checkpoint
    progress = HarnessProgress(
        total_sprints=2,
        sprint_statuses=[
            SprintStatus(sprint_number=i, sprint_name=f"Sprint {i}") for i in range(1, 3)
        ],
    )
    save_progress(checkpoint_dir, progress)

    # Populate contracts and feedback
    save_contract(tmp_path, make_contract())
    save_feedback(tmp_path, 1, 1, make_result())
    save_round_log(tmp_path, 1, 1, {"sprint": 1})

    # Add learnings file that should be deleted
    learnings_file = tmp_path / "generator-learnings.md"
    learnings_file.write_text("## Round 1\n- Decision: used C4Context")

    # Add files that should be preserved
    log_file = tmp_path / "logs" / "run.log"
    log_file.write_text("log content")
    decision_file = tmp_path / "decisions" / "adr-001.md"
    decision_file.write_text("# ADR")

    deleted = clean_run_artifacts(tmp_path, checkpoint_dir)

    assert not (checkpoint_dir / "progress.json").exists()
    assert not (tmp_path / "contracts" / "sprint-1.json").exists()
    assert not (tmp_path / "feedback" / "sprint-1-round-1.json").exists()
    assert not (tmp_path / "feedback" / "sprint-1-round-1-log.json").exists()
    assert not learnings_file.exists(), "generator-learnings.md should be deleted"
    assert log_file.exists(), "logs/ should be preserved"
    assert decision_file.exists(), "decisions/ should be preserved"
    assert len(deleted) == 5  # progress.json + sprint-1.json + feedback + log + learnings


def test_clean_run_artifacts_no_error_on_missing_dirs(tmp_path: Path) -> None:
    """clean_run_artifacts is a no-op when no prior artifacts exist."""
    checkpoint_dir = tmp_path / ".checkpoints"
    deleted = clean_run_artifacts(tmp_path, checkpoint_dir)
    assert deleted == []


def test_append_generator_history_creates_file(tmp_path: Path) -> None:
    """append_generator_history creates the file and writes a structured entry."""
    result = make_result()
    append_generator_history(
        tmp_path,
        sprint_num=1,
        round_num=1,
        previous_feedback=result,
        modified_files=[tmp_path / "c4-context.md"],
        input_tokens=12345,
    )
    history = (tmp_path / "generator-history.md").read_text()
    assert "## Sprint 1 · Round 1" in history
    assert "**Files modified**: c4-context.md" in history
    assert "**Token usage**: 12,345" in history
    assert "concern(s) from prior critic round" in history


def test_append_generator_history_first_round(tmp_path: Path) -> None:
    """First round with no prior feedback uses the correct label."""
    append_generator_history(
        tmp_path,
        sprint_num=2,
        round_num=1,
        previous_feedback=None,
        modified_files=[],
        input_tokens=0,
    )
    history = (tmp_path / "generator-history.md").read_text()
    assert "First round — no prior feedback" in history
    assert "**Files modified**: None" in history


def test_append_generator_history_accumulates(tmp_path: Path) -> None:
    """Multiple calls append; file grows with both entries searchable."""
    for rnd in (1, 2):
        append_generator_history(
            tmp_path, sprint_num=1, round_num=rnd,
            previous_feedback=None, modified_files=[], input_tokens=0,
        )
    history = (tmp_path / "generator-history.md").read_text()
    assert "## Sprint 1 · Round 1" in history
    assert "## Sprint 1 · Round 2" in history


def test_append_critic_history_creates_file(tmp_path: Path) -> None:
    """append_critic_history creates the file and writes a structured entry."""
    result = make_result()
    append_critic_history(tmp_path, sprint_num=1, round_num=1, result=result)
    history = (tmp_path / "critic-history.md").read_text()
    assert "## Sprint 1 · Round 1" in history
    assert "**Score**:" in history
    assert "**Concerns**:" in history
    assert "**Summary**:" in history


def test_append_critic_history_severity_labels(tmp_path: Path) -> None:
    """Severity labels appear in entries for grep-ability."""
    result = make_result()
    append_critic_history(tmp_path, sprint_num=1, round_num=1, result=result)
    history = (tmp_path / "critic-history.md").read_text()
    assert any(s in history for s in ("[Critical]", "[High]", "[Medium]", "[Low]"))


def make_progress(total: int = 3) -> HarnessProgress:
    return HarnessProgress(
        total_sprints=total,
        sprint_statuses=[
            SprintStatus(sprint_number=i, sprint_name=f"Sprint {i}") for i in range(1, total + 1)
        ],
    )


def test_reset_sprint_resets_status(tmp_path: Path) -> None:
    """reset_sprint_artifacts sets status=pending and clears counters."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = make_progress()
    progress.sprint_statuses[1].status = "building"
    progress.sprint_statuses[1].rounds_completed = 4
    progress.sprint_statuses[1].consecutive_passes = 2
    progress.current_sprint = 2
    save_progress(checkpoint_dir, progress)

    updated, _ = reset_sprint_artifacts(tmp_path, checkpoint_dir, sprint_number=2)

    assert updated.sprint_statuses[1].status == "pending"
    assert updated.sprint_statuses[1].rounds_completed == 0
    assert updated.sprint_statuses[1].consecutive_passes == 0
    assert updated.sprint_statuses[1].final_score is None
    assert updated.current_sprint == 2


def test_reset_sprint_passed_decrements_completed(tmp_path: Path) -> None:
    """Resetting a passed sprint decrements completed_sprints."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = make_progress()
    progress.sprint_statuses[1].status = "passed"
    progress.sprint_statuses[1].final_score = 9.5
    progress.completed_sprints = 2
    progress.current_sprint = 3
    save_progress(checkpoint_dir, progress)

    updated, _ = reset_sprint_artifacts(tmp_path, checkpoint_dir, sprint_number=2)

    assert updated.completed_sprints == 1
    assert updated.current_sprint == 2
    assert updated.sprint_statuses[1].status == "pending"


def test_reset_sprint_unterminate_harness(tmp_path: Path) -> None:
    """Resetting a sprint un-terminates a complete/failed harness."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = make_progress()
    progress.status = "complete"
    progress.completed_sprints = 3
    progress.sprint_statuses[2].status = "passed"
    save_progress(checkpoint_dir, progress)

    updated, _ = reset_sprint_artifacts(tmp_path, checkpoint_dir, sprint_number=3)

    assert updated.status == "running"


def test_reset_sprint_deletes_artifacts(tmp_path: Path) -> None:
    """Contract and feedback files for the sprint are deleted; other sprints untouched."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    save_progress(checkpoint_dir, make_progress())

    criteria3 = [
        SprintCriterion(name="a", description="desc a"),
        SprintCriterion(name="b", description="desc b"),
        SprintCriterion(name="c", description="desc c"),
    ]
    contract2 = SprintContract(
        sprint_number=2, sprint_name="S2",
        files_to_produce=["c2.md"],
        criteria=criteria3,
    )
    save_contract(tmp_path, contract2)
    result = make_result()
    save_feedback(tmp_path, 2, 1, result)
    save_feedback(tmp_path, 2, 2, result)
    save_round_log(tmp_path, 2, 1, {"sprint": 2})

    # Sprint 1 artifact should survive
    contract1 = SprintContract(
        sprint_number=1, sprint_name="S1",
        files_to_produce=["c1.md"],
        criteria=criteria3,
    )
    save_contract(tmp_path, contract1)

    reset_sprint_artifacts(tmp_path, checkpoint_dir, sprint_number=2)

    assert not (tmp_path / "contracts" / "sprint-2.json").exists()
    assert not (tmp_path / "feedback" / "sprint-2-round-1.json").exists()
    assert not (tmp_path / "feedback" / "sprint-2-round-2.json").exists()
    assert not (tmp_path / "feedback" / "sprint-2-round-1-log.json").exists()
    assert (tmp_path / "contracts" / "sprint-1.json").exists()


def test_reset_sprint_strips_history(tmp_path: Path) -> None:
    """Sprint N entries are removed from history files; other sprints kept."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    save_progress(checkpoint_dir, make_progress())

    result = make_result()
    append_generator_history(
        tmp_path, sprint_num=1, round_num=1,
        previous_feedback=None, modified_files=[], input_tokens=0,
    )
    append_generator_history(
        tmp_path, sprint_num=2, round_num=1,
        previous_feedback=result, modified_files=[], input_tokens=100,
    )
    append_generator_history(
        tmp_path, sprint_num=2, round_num=2,
        previous_feedback=result, modified_files=[], input_tokens=200,
    )
    append_critic_history(tmp_path, sprint_num=2, round_num=1, result=result)

    reset_sprint_artifacts(tmp_path, checkpoint_dir, sprint_number=2)

    gen_hist = (tmp_path / "generator-history.md").read_text()
    critic_hist = (tmp_path / "critic-history.md").read_text()
    assert "## Sprint 1 · Round 1" in gen_hist
    assert "## Sprint 2" not in gen_hist
    assert "## Sprint 2" not in critic_hist


def test_reset_sprint_invalid_number(tmp_path: Path) -> None:
    """Out-of-range sprint number raises ValueError."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    save_progress(checkpoint_dir, make_progress(total=3))

    with pytest.raises(ValueError, match="out of range"):
        reset_sprint_artifacts(tmp_path, checkpoint_dir, sprint_number=5)


def test_write_index(tmp_path: Path) -> None:
    """write_index creates INDEX.md grouped by sprint, excluding harness artifacts."""
    from deep_architect.sprints import SPRINTS

    init_workspace(tmp_path)

    # Architecture files across several sprints
    (tmp_path / "c1-context.md").write_text("# C1")
    (tmp_path / "c2-container.md").write_text("# C2")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "c2-container.md").write_text("# FE C2")
    (tmp_path / "frontend" / "auth.md").write_text("# Auth")
    (tmp_path / "decisions").mkdir(exist_ok=True)
    (tmp_path / "decisions" / "ADR-001.md").write_text("# ADR-001")

    # Harness artifacts that must NOT appear in the index
    (tmp_path / "generator-history.md").write_text("history")
    (tmp_path / "generator-learnings.md").write_text("learnings")
    (tmp_path / "feedback" / "sprint-1-round-1.json").write_text("{}")

    statuses = [
        SprintStatus(sprint_number=i, sprint_name=f"S{i}", status="passed", final_score=8.5)
        for i in range(1, 8)
    ]
    progress = HarnessProgress(total_sprints=7, sprint_statuses=statuses)

    index_path = write_index(tmp_path, SPRINTS, progress)

    assert index_path == tmp_path / "INDEX.md"
    content = index_path.read_text()

    # Architecture files appear
    assert "c1-context.md" in content
    assert "c2-container.md" in content
    assert "frontend/c2-container.md" in content
    assert "frontend/auth.md" in content
    assert "decisions/ADR-001.md" in content

    # Harness artifacts do not appear
    assert "generator-history.md" not in content
    assert "generator-learnings.md" not in content
    assert "sprint-1-round-1.json" not in content

    # Sprint headers include score and status icon
    assert "Sprint 1 · C1 System Context — 8.5/10 ✓" in content
    assert "Sprint 3 · Frontend Container — 8.5/10 ✓" in content
    assert "Sprint 7 · ADRs + Cross-Cutting Concerns — 8.5/10 ✓" in content


def test_write_index_failed_sprint(tmp_path: Path) -> None:
    """Failed sprints show ✗ and score if available; missing files are simply absent."""
    from deep_architect.sprints import SPRINTS

    init_workspace(tmp_path)
    (tmp_path / "c1-context.md").write_text("# C1")

    statuses = [
        SprintStatus(sprint_number=i, sprint_name=f"S{i}", status="pending")
        for i in range(1, 8)
    ]
    statuses[0].status = "passed"
    statuses[0].final_score = 9.0
    statuses[1].status = "failed"
    statuses[1].final_score = 5.5
    progress = HarnessProgress(total_sprints=7, sprint_statuses=statuses)

    content = write_index(tmp_path, SPRINTS, progress).read_text()

    assert "Sprint 1 · C1 System Context — 9.0/10 ✓" in content
    assert "Sprint 2 · C2 Container Overview — 5.5/10 ✗" in content
    # Sprint with no score shows dash
    assert "— ✗" in content


def test_write_index_excludes_artifact_subdirs(tmp_path: Path) -> None:
    """Files inside contracts/, feedback/, and logs/ are never indexed."""
    from deep_architect.sprints import SPRINTS

    init_workspace(tmp_path)
    (tmp_path / "contracts" / "sprint-1.json").write_text("{}")
    (tmp_path / "feedback" / "sprint-1-round-1.json").write_text("{}")
    (tmp_path / "logs" / "run.log").write_text("log")

    statuses = [
        SprintStatus(sprint_number=i, sprint_name=f"S{i}") for i in range(1, 8)
    ]
    progress = HarnessProgress(total_sprints=7, sprint_statuses=statuses)

    content = write_index(tmp_path, SPRINTS, progress).read_text()

    assert "sprint-1.json" not in content
    assert "sprint-1-round-1.json" not in content
    assert "run.log" not in content


def test_clean_run_artifacts_removes_history_files(tmp_path: Path) -> None:
    """clean_run_artifacts deletes generator-history.md and critic-history.md."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    checkpoint_dir.mkdir()
    (tmp_path / "generator-history.md").write_text("## Sprint 1 · Round 1\n---\n")
    (tmp_path / "critic-history.md").write_text("## Sprint 1 · Round 1\n---\n")

    deleted = clean_run_artifacts(tmp_path, checkpoint_dir)

    assert not (tmp_path / "generator-history.md").exists()
    assert not (tmp_path / "critic-history.md").exists()
    deleted_names = [p.name for p in deleted]
    assert "generator-history.md" in deleted_names
    assert "critic-history.md" in deleted_names


def test_save_final_agreement_writes_markdown(tmp_path: Path) -> None:
    gen_text = "I think this is READY_TO_SHIP because the architecture is solid."
    critic_text = "I do NOT think this is ready. Many gaps remain."

    path = save_final_agreement(tmp_path, gen_text, critic_text, gen_ready=True, critic_ready=False)

    assert path == tmp_path / "final-agreement.md"
    content = path.read_text()
    assert "# Final Mutual Agreement" in content
    assert "**Generator**: READY" in content
    assert "**Critic**: NOT READY" in content
    assert gen_text.strip() in content
    assert critic_text.strip() in content
    assert "## Generator Response" in content
    assert "## Critic Response" in content
