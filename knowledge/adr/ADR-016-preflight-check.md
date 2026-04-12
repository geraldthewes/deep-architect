# ADR-016: Preflight Check Before Starting the Main Loop

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

A 7-sprint run is expensive and long-running. Configuration errors (missing auth token, wrong endpoint URL, wrong model name) should be caught before the run starts, not 5 minutes in.

## Decision

Before the main sprint loop, `run_preflight_check()` sends a minimal "Reply with exactly one word: OK" prompt to both the generator model and the critic model with no tools. If either call fails, it raises `RuntimeError` with a descriptive message before any work is done.

## Rationale

- **Fail fast:** Surface configuration errors immediately rather than after spending time on contract negotiation.
- **Validates both models:** Generator and critic may use different model aliases. Both are checked independently.
- **Minimal cost:** The preflight prompt is tiny (a few tokens each way). If it fails, the run would have failed anyway; if it passes, the cost is negligible relative to the full run.

## Consequences

- Users get immediate feedback on misconfiguration (e.g., `RuntimeError: Preflight failed for generator: auth error`).
- The preflight adds a few seconds to startup time — an acceptable trade-off.
- The preflight does not validate that models are capable of architecture generation — only that they are reachable and responding.

**Files:** `deep_architect/harness.py:67-104`
