from deep_researcher.exit_criteria import should_ping_pong_exit, sprint_passes
from deep_researcher.models.feedback import CriterionScore, CriticResult


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
