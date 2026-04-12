# ADR-006: Multi-Layer Exit Criteria — Score + Severity + Consecutive Rounds + Ping-Pong Detection

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

Each sprint loops between generator and critic until the architecture meets quality standards. The harness needs clear, configurable criteria for when a sprint passes and when to abort a loop that is no longer making progress.

## Decision

A sprint passes when ALL of the following hold for `consecutive_passing_rounds` (default: 2) consecutive rounds:
1. Average critic score ≥ `min_score` (default: 9.0/10)
2. No "Critical" or "High" severity issues in the feedback

Additionally, **ping-pong early exit**: After round 3, if:
- Feedback similarity score ≥ `ping_pong_similarity_threshold` (default: 0.85) — i.e., feedback is repetitive
- Score improvement < 0.1 from the previous round — i.e., no meaningful progress

Then exit the sprint loop regardless of whether pass criteria were met (log a warning).

All thresholds come from `HarnessConfig.thresholds` — never hardcoded.

## Rationale

- **Score threshold (9.0):** High bar because this is production architecture. A 7/10 is mediocre.
- **Severity blocking:** A Mermaid syntax error (Critical) or missing container relationship (High) is a hard failure regardless of overall score. Score could be 9.5/10 but still have a Critical issue.
- **Consecutive rounds:** One good round could be a fluke. Requiring 2 consecutive passing rounds ensures stability.
- **Ping-pong detection:** Without this, agents can loop indefinitely making tiny tweaks with diminishing returns. Similarity detection catches when feedback has become repetitive and the agents are stuck.

## Consequences

- All logic is in `exit_criteria.py` — isolated, testable.
- Thresholds are fully configurable via `~/.deep-architect.toml`; unit tests cover all combinations.
- Clear priority order: Critical/High severity **always** blocks, regardless of score.
- Ping-pong exit produces a warning log but does not fail the run — it advances to the next sprint.

**Files:** `deep_architect/exit_criteria.py`, `deep_architect/models/feedback.py:22-28`, `deep_architect/harness.py:379-411`, `tests/test_exit_criteria.py`
