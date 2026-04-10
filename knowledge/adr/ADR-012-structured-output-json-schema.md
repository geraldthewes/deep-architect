# ADR-012: Structured Critic Output via JSON Schema (output_format)

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The critic must return structured feedback (scores, severity levels, file references) that the harness can parse and act on. The feedback could be free-form text, a JSON string embedded in text, or enforced structured output.

## Decision

The critic uses `output_format` with a JSON schema derived from `CriticResult.model_json_schema()`. This is passed to the Claude CLI, which enforces that the model outputs valid JSON matching the schema. The result is parsed back into a `CriticResult` Pydantic model.

`CriticResult` contains:
- `criteria_scores`: list of `CriterionScore` (criterion name, score 0-10, severity, notes)
- `overall_score`: aggregate score
- `blocking_issues`: list of critical/high issues
- `passed`: computed field (True if score ≥ threshold AND no Critical/High issues)
- `summary`: human-readable feedback text

## Rationale

- **Schema enforcement at the CLI level:** The CLI retries internally if the model fails to conform to the schema. This is more reliable than parsing free-form JSON from text output.
- **Type safety:** Parsing into a Pydantic model gives the harness typed access to all fields. No string parsing or dict key lookups.
- **Consistency:** Every critic response has the same shape. Exit criteria logic can safely access `result.overall_score` and `result.passed` without defensive coding.
- **Alternative rejected:** Asking the critic to return "JSON in code fences" and parsing afterward is fragile and model-version-sensitive.

## Consequences

- `CriticResult.model_json_schema()` defines the contract between the harness and the critic prompt.
- Changes to `CriticResult` fields must be reflected in critic prompts and vice versa.
- The `passed` field is a Pydantic `model_validator` computed at parse time — it is not stored in the JSON, it is derived.
- For pydantic-ai structured calls, use the current API (not deprecated `result.data` / `result_type=`).

**Files:** `deep_researcher/agents/client.py:57-59`, `deep_researcher/agents/critic.py:60-75`, `deep_researcher/models/feedback.py`
