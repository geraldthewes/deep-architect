# ADR-012: Structured Critic Output via JSON Schema (output_format)

**Status:** Accepted  
**Date:** 2026-04-10 (restored 2026-04-14 — see ADR-025)  
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

**Files:** `deep_architect/agents/client.py:57-59`, `deep_architect/agents/critic.py:60-75`, `deep_architect/models/feedback.py`

## History

**CLI 2.1.104 regression (April 2026):** The injected `StructuredOutput` tool caused the
LiteLTM proxy to return `invalid_request` on the first API call. `output_format` was
temporarily removed (commit `fcf262a`), causing the critic to rely on prompt instructions
alone. This led to intermittent failures where the critic ended its session after the last
tool call without emitting JSON. See ADR-025 for the full investigation.

**CLI 2.1.107 restoration (April 2026):** Confirmed via `scripts/test_output_format.py
--approach A` that the regression was fixed. `output_format` restored as the primary path.
A rescue fallback (`_critic_rescue`) was added and kept as defense-in-depth (ADR-025).
