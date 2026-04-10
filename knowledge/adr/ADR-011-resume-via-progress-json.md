# ADR-011: Sprint-Level Resume via progress.json

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

A full 7-sprint run is expensive (many LLM calls, high token usage, potentially 30+ minutes). If the harness crashes after sprint 5, the user should not have to regenerate sprints 1-5.

## Decision

The harness writes a `progress.json` file after each sprint completes. The `--resume` CLI flag loads this file and skips completed sprints, resuming from the last incomplete sprint.

`progress.json` tracks:
- `current_sprint`: index of the sprint currently in progress
- `completed_sprints`: count of fully passed sprints
- `status`: `"running"` | `"complete"` | `"failed"`
- Per-sprint: status, rounds completed, final score, files produced

## Rationale

- **Cost protection:** Regenerating 5 completed sprints due to a crash in sprint 6 wastes significant time and money.
- **Sprint-level granularity is sufficient:** Resuming at a round level within a sprint would be more complex. Since sprints are self-contained, restarting a partially-complete sprint from the beginning is acceptable.
- **Architecture files preserved:** Already-committed architecture files are left untouched when resuming. The git history holds all prior work.

## Consequences

- `--resume` is an opt-in flag; default behavior always starts fresh.
- If `progress.json` is from a different PRD or run, the user must delete it manually before starting fresh.
- Round-level resume is not supported (a partially-completed sprint always restarts from round 1 on resume).
- The progress file is written atomically after each sprint to avoid partial writes.

**Files:** `deep_researcher/io/files.py:41-49`, `deep_researcher/models/progress.py`, `deep_researcher/harness.py:191-202`
