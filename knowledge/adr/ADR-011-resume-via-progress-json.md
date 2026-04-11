# ADR-011: Round-Level Resume via .checkpoints/progress.json

**Status:** Accepted (updated by PROJ-0002)
**Date:** 2026-04-10 (revised 2026-04-10)
**Deciders:** Project design

---

## Context

A full 7-sprint run is expensive (many LLM calls, high token usage, potentially 3+ hours). If the harness crashes mid-run, the user should not have to regenerate completed work. PROJ-0001 established sprint-level resume; PROJ-0002 extends this to round-level granularity.

## Decision

The harness writes a `progress.json` checkpoint after every completed round (both generator and critic done) and at every sprint state transition. The `--resume` CLI flag loads the checkpoint, skips completed sprints and rounds, and continues from the first incomplete round.

Checkpoints are stored in `.checkpoints/progress.json` at the repository root (not inside the output directory). This keeps transient recovery state separate from permanent architecture output.

`progress.json` tracks:
- `current_sprint`: 1-based index of the sprint currently in progress
- `completed_sprints`: count of fully passed sprints
- `status`: `"running"` | `"complete"` | `"failed"`
- Per-sprint: status, `rounds_completed`, `consecutive_passes`, final score

Checkpoint writes use an atomic pattern (`write_text` to `.tmp` then `os.replace`) so a SIGKILL during write leaves the prior checkpoint intact.

## Rationale

- **Cost protection:** Skipping completed sprints and rounds avoids re-paying for expensive LLM calls.
- **Round-level granularity:** `sprint_status.rounds_completed` is already persisted after each round; using it to set the round loop start is a small, low-risk change.
- **Isolated checkpoint location:** `.checkpoints/` at repo root is gitignored and obviously transient — safe to delete between runs without touching architecture output.
- **Atomic writes:** `os.replace` is POSIX-atomic; a partial write from SIGKILL leaves the previous checkpoint in place rather than a corrupt file.

## Consequences

- `--resume` is opt-in; default behavior always starts fresh.
- If `.checkpoints/` does not exist when `--resume` is passed, the harness fails immediately with a clear error.
- If `progress.json` is from a different PRD or run, the user must delete `.checkpoints/` manually before starting fresh.
- Mid-round resume is not supported (if the generator is killed mid-tool-call, that round restarts from the beginning). The atomic unit of recovery is a completed generator+critic round.
- **Generator session context is lost on mid-sprint resume.** The Claude Code CLI session ID is in-process only; it cannot be restored after a crash. On resume, the generator starts a fresh session but retains access to on-disk architecture files via its Read/Grep/Glob tools.
- `.checkpoints/` is added to `.gitignore` — it is not part of the architecture output and should not be committed.

**Files:** `deep_researcher/io/files.py:41-49`, `deep_researcher/models/progress.py`, `deep_researcher/harness.py:191-210`
