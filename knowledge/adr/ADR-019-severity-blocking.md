# ADR-019: Critical/High Severity Issues Block Sprint Completion Regardless of Score

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The critic assigns both a numeric score (0-10) and a severity level (Critical, High, Medium, Low, Info) to each criterion. A sprint could have a high average score but still have a Critical issue. Should the score alone determine pass/fail?

## Decision

No. A sprint does **not** pass if any criterion is scored as `Critical` or `High` severity — regardless of the numeric score. The pass condition is:

```python
passed = (average_score >= min_score) AND (no Critical or High issues)
```

This is computed as a Pydantic `model_validator` on `CriticResult.passed` at parse time.

## Rationale

- **Architecture errors are qualitative, not quantitative:** A Mermaid syntax error (Critical) makes the diagram unrenderable. A missing C2 relationship (High) is a structural gap. These failures cannot be averaged away by high scores on other criteria.
- **Score is not sufficient alone:** The model might rate a diagram 9.5/10 overall but flag a Critical issue in one criterion. The intent is that Critical/High issues are always blocking.
- **Clear priority ordering:** Severity > Score. This is unambiguous and predictable for users.

## Consequences

- The generator must address ALL Critical and High issues before a sprint can pass.
- A sprint with perfect 10/10 scores but one Critical issue does not pass.
- Severity level is part of `CriterionScore` and must be explicitly set by the critic model.
- Tests in `test_exit_criteria.py` verify that Critical/High always blocks even at perfect scores.

**Files:** `deep_architect/models/feedback.py:22-28`, `deep_architect/exit_criteria.py:6-11`, `tests/test_exit_criteria.py`
