# ADR-021: Stateless Session Per Turn — Generator Session Reset Every Round

**Status:** Accepted
**Date:** 2026-04-11
**Supersedes:** ADR-004 (Generator Session Persistence Within Sprint)
**Deciders:** Project design (PROJ-0003)

---

## Context

ADR-004 established that the generator reuses a `session_id` across all rounds within a sprint,
giving it implicit memory of prior tool calls. However:

1. On crash or `--resume`, the session was already lost and the system handled it gracefully.
2. All explicit context (PRD, contract, feedback, learnings) was re-injected every round regardless.
3. Accumulated session context from many rounds contributed to context-overflow failures on long runs.
4. The "generator session context lost on resume" warning in ADR-011 documented the existing
   graceful degradation path — PROJ-0003 normalizes this into the standard execution model.

## Decision

The generator starts a **fresh session for every round**. `session_id` is never reused across
rounds. The harness no longer tracks `generator_session_id`.

The implicit memory previously provided by session continuity is replaced by:
- `generator-history.md` — harness-written, structured per-round record of files changed and
  feedback addressed (objective, grep-searchable)
- `generator-learnings.md` — agent-written, free-form working memory (subjective, fully injected)

Both files persist across sprints and survive crashes. `--resume` loads them automatically from
disk — no special-case logic required.

## Rationale

- **Eliminates context accumulation.** A fresh session every round bounds context window usage
  to a single turn, removing a known source of instability on long runs.
- **Normalizes the existing crash/resume path.** The system already handled the no-session case
  correctly (confirmed by ADR-011 and crash recovery tests). PROJ-0003 makes this the only path.
- **File-mediated handoff is strictly more robust.** History files survive process restarts;
  session context does not.
- **Resume becomes the standard flow.** `--resume` and a fresh start use identical code paths.

## Consequences

- Each generator round pays the full context-window cost of re-reading history and making
  file-discovery tool calls. This is mitigated by history files being path-only (not injected)
  and learnings being concise.
- `generator_session_id` tracking removed from `harness.py`. `session_id` and
  `last_known_input_tokens` parameters removed from `run_generator()`.
- `GeneratorRoundResult.session_id` retained (SDK still returns it) but never fed back into
  subsequent rounds.
- `test_harness_resets_generator_session_on_retry` replaced — the behavior it tested no longer exists.

**Files:** `deep_architect/agents/generator.py`, `deep_architect/harness.py`,
`deep_architect/io/files.py`, `deep_architect/agents/critic.py`
