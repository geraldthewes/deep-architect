from __future__ import annotations

from deep_architect.models.feedback import CriticResult


def has_critical_or_high(result: CriticResult) -> bool:
    return any(f.severity in ("Critical", "High") for f in result.feedback)


def sprint_passes(result: CriticResult, min_score: float) -> bool:
    return result.average_score >= min_score and not has_critical_or_high(result)


def is_perfect_score(result: CriticResult) -> bool:
    """True when every individual criterion score is at the ceiling (10.0)."""
    return bool(result.feedback) and all(f.score >= 10.0 for f in result.feedback)


def should_ping_pong_exit(
    similarity_score: float,
    current: CriticResult,
    previous: CriticResult,
    threshold: float,
) -> bool:
    """Exit if similarity above threshold AND no meaningful score improvement."""
    score_improvement = current.average_score - previous.average_score
    return similarity_score >= threshold and score_improvement < 0.1
