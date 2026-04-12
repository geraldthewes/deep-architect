# ADR-003: Asymmetric Tool Access — Generator Writes, Critic Reads Only

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The adversarial loop requires two agents with distinct roles: a Generator (Winston the architect) that produces architecture files and a Critic (Boris) that evaluates them. Tool access must reflect these roles.

## Decision

- **Generator tools:** `["Read", "Write", "Edit", "Bash", "Glob", "Grep"]` — can create and modify files
- **Critic tools:** `["Read", "Bash", "Glob", "Grep"]` — read-only; `Write` and `Edit` are explicitly excluded

The critic inspects files directly via tools rather than receiving file contents pasted into the prompt.

## Rationale

- **Role enforcement by design:** Removing `Write`/`Edit` from the critic makes it structurally impossible for the critic to modify files, preserving the adversarial separation.
- **Real inspection is better than pasted content:** The critic can `Grep` for specific patterns, `Glob` to discover all files, and `Read` exactly what it needs. Pasting large file trees into prompts is wasteful and potentially context-overflowing.
- **Adversarial value:** The critic must be an independent evaluator, not a co-author. Separate tool sets enforce this independence.

## Consequences

- Two separate agent configurations, two different prompts, two tool lists.
- The critic cannot accidentally corrupt files (even if the model tries to call `Write`, the disallowed tools list blocks it).
- File inspection quality is high: the critic can navigate the full architecture tree the same way a human reviewer would.

**Files:** `deep_architect/agents/generator.py:20`, `deep_architect/agents/critic.py:20`
