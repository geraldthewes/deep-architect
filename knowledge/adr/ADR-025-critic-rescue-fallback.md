# ADR-025: Critic Rescue Fallback for Empty Structured Output

**Status:** Accepted  
**Date:** 2026-04-14  
**Deciders:** Project design

---

## Context

The critic uses `output_format=json_schema_format(CriticResult)` (ADR-012) to enforce
structured JSON output via the CLI's `StructuredOutput` tool. In April 2026, the
harness began failing repeatedly at Sprint 4 with:

```
ERROR [Sprint 4] Round 9 attempt 3/3 FAILED: Expecting value: line 1 column 1 (char 0)
```

### Root Cause Investigation

The failure chain:

1. CLI 2.1.104 introduced a regression: when `--json-schema` is passed, the injected
   `StructuredOutput` tool caused the LiteLTM proxy to return `invalid_request` on the
   first API call. `output_format` was temporarily removed in commit `fcf262a` to unblock
   the harness.

2. Without `output_format` enforcement, the critic's agentic loop could end its session
   after the last tool call (typically a `Bash: mmdc ...` validation) without emitting a
   final text response. `result.result` came back empty from the SDK.

3. Commit `fdb4ae1` added a `_last_agent_text` ContextVar to capture the last non-empty
   text block as a fallback, but when the critic's only text turn was its opening
   analysis (not JSON), `json.loads()` still failed.

4. A diagnostic script (`scripts/test_output_format.py`) was written to test three
   structured-output approaches against the live proxy:
   - **A**: Claude Code SDK + `output_format` (original design)
   - **B**: pydantic-ai via OpenAI-compatible endpoint
   - **C**: Baseline — system prompt instruction only

   Results with CLI **2.1.107**:
   - **A: PASS** — `output_format` works again. The 2.1.104 regression was fixed.
   - **B: FAIL** — Connection error (endpoint URL mismatch; the OpenAI provider path
     needs the litellm OpenAI endpoint URL, not the Anthropic one).
   - **C: PASS** — Baseline works when the model cooperates (as expected).

### Resolution

`output_format` was restored in `run_critic()`. Protocol-level enforcement is back as
the primary path.

## Decision

Keep the rescue call (`_critic_rescue`) that was added as an interim fix, as a last-resort
fallback behind the restored `output_format` enforcement:

```
primary:  output_format → StructuredOutput tool → result.structured_output (populated)
fallback: result.structured_output is None → parse result.result / _last_agent_text
rescue:   ValueError / JSONDecodeError → _critic_rescue() reads files via Python I/O,
          calls run_simple_structured() directly (no subprocess)
```

The rescue call is defense-in-depth. If `output_format` enforcement holds — as it does
with CLI 2.1.107 — it will never trigger. If a future CLI regression re-introduces the
failure, the harness degrades gracefully instead of failing the sprint.

## Rationale

- **Protocol enforcement is more reliable than prompt instructions.** The `StructuredOutput`
  tool makes the CLI retry internally until the model conforms to the schema. Prompt
  instructions can be ignored; tool-calling cannot.
- **Defense-in-depth.** Keeping the rescue path costs nothing at runtime (it only fires on
  exception) but prevents a single CLI regression from causing a multi-hour sprint failure.
- **Rescue avoids re-running the generator.** The harness round-retry loop (ADR-015) re-runs
  both the generator and the critic on failure. A rescue call costs one fast direct API call
  instead of 3 × (generator + critic) retries at ~$1/each.
- **Diagnostic script as living documentation.** `scripts/test_output_format.py` can be
  re-run after any CLI upgrade to verify that approach A still works before investing in a
  full harness run.

## Consequences

- `run_critic()` in `critic.py` passes `output_format=json_schema_format(CriticResult)`.
- `_critic_rescue()` remains in `critic.py` as a fallback; it reads architecture files via
  Python I/O and calls `run_simple_structured()`. Note: it cannot run `mmdc` validation,
  so Mermaid syntax scores may be less precise when the rescue path fires.
- `critic_rescue.md` prompt template must remain in `deep_architect/prompts/`.
- After any Claude Code CLI upgrade, run `uv run python scripts/test_output_format.py --approach A`
  to verify `output_format` still works before a full harness run.

**Files:** `deep_architect/agents/critic.py`, `deep_architect/prompts/critic_rescue.md`,
`deep_architect/agents/client.py` (`_last_agent_text`, `_deref_schema`),
`scripts/test_output_format.py`
