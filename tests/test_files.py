from pathlib import Path

from deep_researcher.io.files import (
    init_workspace,
    load_contract,
    load_feedback,
    load_progress,
    save_contract,
    save_feedback,
    save_progress,
    save_round_log,
)
from deep_researcher.models.contract import SprintContract, SprintCriterion
from deep_researcher.models.feedback import CriterionScore, CriticResult
from deep_researcher.models.progress import HarnessProgress, SprintStatus


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
