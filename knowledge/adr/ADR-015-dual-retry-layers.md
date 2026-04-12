# ADR-015: Two Independent Retry Layers — Agent Retry and Round Retry

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The harness makes many LLM calls via subprocess. Transient failures occur: the CLI crashes, a disallowed tool is called, a network hiccup causes a timeout. Without retry logic, any single failure aborts the entire run.

## Decision

Two independent retry mechanisms:

**1. Agent retry (`max_agent_retries`, default: 2)**
- Scope: A single agent call (one generator or critic invocation)
- Trigger: CLI raises an exception (crash, tool error, timeout)
- Behavior: Retry the same agent call with the same session_id up to N times
- Implemented in: `run_agent()` in `client.py`

**2. Round retry (`max_round_retries`, default: 2)**
- Scope: An entire round (generator call + critic call)
- Trigger: Any exception in the round that wasn't resolved by agent retries
- Behavior: Retry the whole round, resetting `generator_session_id` to `None` (fresh context)
- Implemented in: the round loop in `harness.py`

## Rationale

- **Agent retry** handles transient failures where the same call is likely to succeed on retry (e.g., a disallowed tool hallucination — retrying with the same session often succeeds because the model takes a different path).
- **Round retry** handles deeper failures where agent state may be corrupted or the generator is stuck. Resetting to `None` forces a fresh session.
- A single retry layer would conflate "try this call again" with "start this round over" — these have different semantics and different recovery behaviors.

## Consequences

- The harness is resilient to most transient failures without human intervention.
- Maximum retry attempts per round: `max_agent_retries` × `max_round_retries` (e.g., 2 × 2 = 4 agent-level attempts within 2 round attempts).
- If all retries are exhausted, the sprint fails and the run stops with a clear error.
- Tests in `test_harness_retry.py` cover agent retry, round retry, and session reset behavior.

**Files:** `deep_architect/agents/client.py:296-406`, `deep_architect/harness.py:262-343`, `tests/test_harness_retry.py`
