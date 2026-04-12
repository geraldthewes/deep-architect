# ADR-018: Token/Cost Tracking via RunStats and Context Variables

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

A full run makes dozens of LLM calls across 7 sprints. Users need visibility into total token usage and estimated cost for billing and optimization. This state must be accumulated across all agent calls without threading issues.

## Decision

A `RunStats` object is initialized at the start of `run_harness()` via `init_run_stats()` and stored in a Python `contextvars.ContextVar`. Each agent call in `run_agent()` reads the current stats object and calls `stats.accumulate(result)` to merge in per-call token counts and costs.

At the end of the run, `run_stats.log_summary()` logs a summary.

`RunStats` tracks per model:
- Input tokens, output tokens, cache-read tokens, cache-write tokens
- Estimated cost in USD
- Number of calls, total turns, total duration

## Rationale

- **Context variables over globals:** A module-level global would not be safe if the harness is ever called concurrently (e.g., multiple runs in the same process). Context variables isolate state per async task.
- **Accumulate at call site:** Each `run_agent()` call knows its result; it's the right place to merge stats rather than pushing that responsibility to callers.
- **Log at end:** Emitting stats at the end of the run gives a clean summary without interspersing cost info in the per-sprint logs.

## Consequences

- `RunStats` is reset on each `run_harness()` call; there is no cross-run accumulation.
- Token counts depend on the LLM response including usage metadata. If the endpoint does not return usage, stats will be zero.
- The context variable approach means stats are not accessible from outside the async task — by design.

**Files:** `deep_architect/agents/client.py:141-217`, `deep_architect/harness.py:180,448`
