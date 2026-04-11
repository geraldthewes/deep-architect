# ADR-004: Generator Session Persistence Within Sprint, Reset on Failure

**Status:** Superseded by ADR-021 (2026-04-11)  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

Within a sprint, the generator iterates over multiple rounds — each round refining the same files based on critic feedback. A session ID can be passed to the Claude CLI to resume a prior conversation, preserving context across calls.

## Decision

The generator maintains a persistent `session_id` across all rounds **within the same sprint**. The session resets to `None` if:
1. The agent raises an exception (CLI crash, disallowed tool call, etc.)
2. A round-level retry is triggered

The session is always reset between sprints.

## Rationale

- Within a sprint, the generator is making incremental improvements to the same files. Session persistence lets it remember prior edits without re-reading all files from scratch.
- On CLI crash, the session state may be corrupted; retrying with the same session_id can cause repeated failures. Resetting to `None` forces a clean slate.
- Persisting sessions across sprints would accumulate context from earlier sprints, which is irrelevant (and potentially confusing) to the current sprint's focus.

## Consequences

- `harness.py` tracks `generator_session_id` per sprint (initialized to `None`, updated from `run_generator()` return value).
- On exception in the round-retry loop, `generator_session_id` is reset to `None`.
- Faster iteration within a sprint: the generator "remembers" what it already wrote.
- Fresh start on retry ensures recovery from corrupted session state.

**Files:** `deep_researcher/agents/generator.py:33-92`, `deep_researcher/harness.py:230,268-278,333`
