from deep_architect.exit_criteria import (
    is_perfect_score,
    should_early_accept,
    should_ping_pong_exit,
    sprint_passes,
)
from deep_architect.models.feedback import CriterionScore, CriticResult


def make_result(avg: float, severities: list[str]) -> CriticResult:
    feedback = [
        CriterionScore(criterion=f"c{i}", score=avg, severity=s, details="test")
        for i, s in enumerate(severities)
    ]
    return CriticResult(scores={}, feedback=feedback, overall_summary="test")


def test_passes_when_high_score_no_critical() -> None:
    r = make_result(9.5, ["Medium", "Low"])
    assert sprint_passes(r, 9.0) is True


def test_fails_when_score_below_threshold() -> None:
    r = make_result(8.9, ["Medium"])
    assert sprint_passes(r, 9.0) is False


def test_fails_when_critical_even_high_score() -> None:
    r = make_result(9.5, ["Critical"])
    assert sprint_passes(r, 9.0) is False


def test_fails_when_high_severity() -> None:
    r = make_result(9.5, ["High"])
    assert sprint_passes(r, 9.0) is False


def test_passes_with_low_medium_only() -> None:
    r = make_result(9.2, ["Low", "Medium", "Low"])
    assert sprint_passes(r, 9.0) is True


def test_ping_pong_exit_both_conditions() -> None:
    curr = make_result(9.0, ["Medium"])
    prev = make_result(8.95, ["Medium"])
    assert should_ping_pong_exit(0.90, curr, prev, 0.85) is True


def test_no_ping_pong_when_improving() -> None:
    curr = make_result(9.5, ["Low"])
    prev = make_result(8.0, ["Medium"])
    assert should_ping_pong_exit(0.90, curr, prev, 0.85) is False


def test_no_ping_pong_below_threshold() -> None:
    curr = make_result(9.0, ["Medium"])
    prev = make_result(8.95, ["Medium"])
    assert should_ping_pong_exit(0.80, curr, prev, 0.85) is False


def test_perfect_score_all_10() -> None:
    r = make_result(10.0, ["Low", "Low", "Low"])
    assert is_perfect_score(r) is True


def test_perfect_score_single_criterion() -> None:
    r = make_result(10.0, ["Low"])
    assert is_perfect_score(r) is True


def test_perfect_score_fails_when_one_criterion_below() -> None:
    feedback = [
        CriterionScore(criterion="c0", score=10.0, severity="Low", details="ok"),
        CriterionScore(criterion="c1", score=9.5, severity="Low", details="ok"),
    ]
    r = CriticResult(scores={}, feedback=feedback, overall_summary="test")
    assert is_perfect_score(r) is False


def test_perfect_score_fails_for_passing_but_not_perfect() -> None:
    r = make_result(9.5, ["Low", "Medium"])
    assert is_perfect_score(r) is False


# ---------------------------------------------------------------------------
# should_early_accept
# ---------------------------------------------------------------------------

def test_early_accept_triggers() -> None:
    assert should_early_accept(9.8, 3, 9.5, 3) is True


def test_early_accept_exact_boundary() -> None:
    assert should_early_accept(9.5, 3, 9.5, 3) is True


def test_early_accept_score_too_low() -> None:
    assert should_early_accept(9.3, 5, 9.5, 3) is False


def test_early_accept_stalls_too_few() -> None:
    assert should_early_accept(9.8, 2, 9.5, 3) is False


def test_early_accept_both_conditions_must_hold() -> None:
    # Score high enough but stalls short by one
    assert should_early_accept(9.9, 2, 9.5, 3) is False
    # Stalls met but score just below threshold
    assert should_early_accept(9.49, 3, 9.5, 3) is False


def test_early_accept_custom_thresholds() -> None:
    assert should_early_accept(9.8, 2, 9.8, 2) is True
    assert should_early_accept(9.79, 2, 9.8, 2) is False
