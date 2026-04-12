import pytest

from deep_architect.models.contract import ContractReviewResult, SprintContract, SprintCriterion
from deep_architect.models.feedback import CriterionScore, CriticResult


def test_critic_result_passed_computed() -> None:
    feedback = [
        CriterionScore(criterion="c1", score=9.5, severity="Low", details="ok"),
        CriterionScore(criterion="c2", score=9.2, severity="Medium", details="ok"),
    ]
    r = CriticResult(scores={"c1": 9.5, "c2": 9.2}, feedback=feedback, overall_summary="good")
    assert r.passed is True
    assert abs(r.average_score - 9.35) < 0.01


def test_critic_result_fails_on_critical() -> None:
    feedback = [
        CriterionScore(criterion="c1", score=9.5, severity="Critical", details="bad"),
    ]
    r = CriticResult(scores={}, feedback=feedback, overall_summary="bad")
    assert r.passed is False


def test_critic_result_fails_on_high() -> None:
    feedback = [
        CriterionScore(criterion="c1", score=9.5, severity="High", details="bad"),
    ]
    r = CriticResult(scores={}, feedback=feedback, overall_summary="bad")
    assert r.passed is False


def test_critic_result_fails_low_score() -> None:
    feedback = [
        CriterionScore(criterion="c1", score=8.0, severity="Medium", details="ok"),
    ]
    r = CriticResult(scores={}, feedback=feedback, overall_summary="ok")
    assert r.passed is False
    assert abs(r.average_score - 8.0) < 0.01


def test_sprint_contract_validation() -> None:
    with pytest.raises(Exception):
        # criteria must have at least 3 items
        SprintContract(
            sprint_number=1,
            sprint_name="test",
            files_to_produce=["c1-context.md"],
            criteria=[SprintCriterion(name="c1", description="d")],
        )


def test_contract_review_result_approved() -> None:
    r = ContractReviewResult(approved=True, revised_contract=None)
    assert r.approved is True
    assert r.revised_contract is None


def test_contract_review_result_revised() -> None:
    contract = SprintContract(
        sprint_number=1,
        sprint_name="C1",
        files_to_produce=["c1-context.md"],
        criteria=[
            SprintCriterion(name="c1", description="Criterion 1"),
            SprintCriterion(name="c2", description="Criterion 2"),
            SprintCriterion(name="c3", description="Criterion 3"),
        ],
    )
    r = ContractReviewResult(approved=False, revised_contract=contract)
    assert r.approved is False
    assert r.revised_contract is not None
    assert r.revised_contract.sprint_number == 1


def test_sprint_contract_valid() -> None:
    contract = SprintContract(
        sprint_number=1,
        sprint_name="C1",
        files_to_produce=["c1-context.md"],
        criteria=[
            SprintCriterion(name="c1", description="Criterion 1"),
            SprintCriterion(name="c2", description="Criterion 2"),
            SprintCriterion(name="c3", description="Criterion 3"),
        ],
    )
    assert contract.sprint_number == 1
    assert len(contract.criteria) == 3
