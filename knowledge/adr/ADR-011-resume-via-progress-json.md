# ADR-011: Round-Level Resume via .checkpoints/progress.json

**Status:** Accepted (updated by PROJ-0002)
**Date:** 2026-04-10 (revised 2026-04-11)
**Deciders:** Project design

---

## Context

A full 7-sprint run is expensive (many LLM calls, high token usage, potentially 3+ hours). If the harness crashes mid-run, the user should not have to regenerate completed work. PROJ-0001 established sprint-level resume; PROJ-0002 extends this to round-level granularity.

## Decision

The harness writes a `progress.json` checkpoint after every completed round (both generator and critic done) and at every sprint state transition. The `--resume` CLI flag loads the checkpoint, skips completed sprints and rounds, and continues from the first incomplete round.

### Checkpoint location

Checkpoints are stored in `.checkpoints/progress.json` at the git repository root (derived from `repo.working_tree_dir`), not inside the output directory. This keeps transient recovery state separate from permanent architecture output. The `.checkpoints/` directory is gitignored.

### What `progress.json` tracks

Top-level (`HarnessProgress`):
- `status`: `"running"` | `"complete"` | `"failed"`
- `current_sprint`: 1-based index of the sprint currently in progress
- `total_sprints`: total number of sprints in the run
- `completed_sprints`: count of fully passed sprints
- `total_rounds`: cumulative count of completed rounds across all sprints
- `started_at`: UTC timestamp of the run start
- `seed`: integer seed for reproducibility

Per-sprint (`SprintStatus`):
- `status`: `"pending"` | `"negotiating"` | `"building"` | `"evaluating"` | `"passed"` | `"failed"`
- `rounds_completed`: number of fully completed rounds in this sprint
- `consecutive_passes`: number of consecutive passing rounds (for exit criteria)
- `final_score`: final critic score when sprint completes (null while in progress)

### Atomic writes

Checkpoint writes use an atomic pattern: `write_text` to a `.tmp` sibling then `os.replace` to swap atomically. A SIGKILL during write leaves the prior checkpoint intact rather than a corrupt file.

### Fail-fast on missing checkpoint

If `--resume` is passed but no `.checkpoints/progress.json` exists, the harness raises `FileNotFoundError` immediately — before any expensive operations (workspace init, preflight check, LLM calls). The error message includes the expected checkpoint path and guidance to run without `--resume` for a fresh start.

### Resume behavior

On resume:
1. **Progress status reset** — `progress.status` is set back to `"running"` so a previously failed run can be retried.
2. **Sprint skip** — Sprints with status `"passed"` or `"failed"` are skipped entirely.
3. **Round skip** — Within an in-progress sprint, the round loop starts at `rounds_completed + 1`, skipping already-completed rounds.
4. **State restoration** — `consecutive_passes` is restored from `sprint_status.consecutive_passes`. The last critic result (`last_result`) is reloaded from the feedback file on disk if it exists.
5. **Contract reload** — On mid-sprint resume (`rounds_completed > 0`), the negotiated contract is loaded from disk rather than re-negotiated. Falls back to re-negotiation if the contract file is missing.

### Clean restart

`clean_run_artifacts(output_dir, checkpoint_dir)` deletes all files in `.checkpoints/`, `contracts/`, and `feedback/` subdirectories, plus the `generator-learnings.md` file. This enables a clean fresh start without manually deleting directories. Logs and decisions are preserved.

## Rationale

- **Cost protection:** Skipping completed sprints and rounds avoids re-paying for expensive LLM calls.
- **Round-level granularity:** `sprint_status.rounds_completed` is persisted after each round; using it to set the round loop start is a small, low-risk change.
- **Isolated checkpoint location:** `.checkpoints/` at repo root is gitignored and obviously transient — safe to delete between runs without touching architecture output.
- **Atomic writes:** `os.replace` is POSIX-atomic; a partial write from SIGKILL leaves the previous checkpoint in place rather than a corrupt file.
- **Fail-fast:** Catching the missing-checkpoint case before any LLM calls prevents wasted cost and confusing behavior.

## Consequences

- `--resume` is opt-in; default behavior always starts fresh.
- If `.checkpoints/` does not exist when `--resume` is passed, the harness fails immediately with a clear error.
- If `progress.json` is from a different PRD or run, the user must delete `.checkpoints/` manually before starting fresh.
- Mid-round resume is not supported (if the generator is killed mid-tool-call, that round restarts from the beginning). The atomic unit of recovery is a completed generator+critic round.
- **Generator session context is lost on mid-sprint resume.** The Claude Code CLI session ID (`generator_session_id`) is an in-process handle; it cannot be restored after a crash. On resume, the generator starts a fresh session but retains access to on-disk architecture files via its Read/Grep/Glob tools. This is logged as a warning during resume.
- `.checkpoints/` is added to `.gitignore` — it is not part of the architecture output and should not be committed.

## Key Files

- `deep_architect/io/files.py:42-53` — `save_progress` (atomic write), `load_progress`
- `deep_architect/io/files.py:64-84` — `clean_run_artifacts`
- `deep_architect/models/progress.py:10-29` — `SprintStatus`, `HarnessProgress`
- `deep_architect/harness.py:206-247` — checkpoint_dir setup, fail-fast, resume/init branch
- `deep_architect/harness.py:258-264` — already-complete sprint skip
- `deep_architect/harness.py:266-295` — contract reload on mid-sprint resume
- `deep_architect/harness.py:306-325` — round-state restoration and round loop start
- `deep_architect/harness.py:490-492` — `consecutive_passes` persistence after each round
