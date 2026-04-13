# ADR-024: Soft-Fail Sprint Completion (Best-Effort Accept)

**Status:** Accepted  
**Date:** 2026-04-12  
**Deciders:** Project design  
**Supersedes:** —  
**Related:** ADR-023 (keep-best rollback), ADR-006 (exit criteria)

---

## Context

When a sprint exhausted `max_rounds_per_sprint` without achieving `consecutive_passing_rounds` of passing critic scores, the harness marked the sprint and the entire run as `"failed"` and returned immediately.

ADR-023 introduced keep-best rollback, which already tracks `best_result` and `best_commit_sha` throughout every sprint. By the time max rounds are exhausted, the architecture files are at or near the best-scoring version seen during that sprint. Discarding this work and halting the run is wasteful — particularly for long-running autonomous sessions where a sprint may get stuck on one stubborn criterion while producing solid output across the others.

The same issue applies to Path B (inner retry exhaustion): if at least one critic round succeeded before the failure, there is a usable `best_result` to fall back on.

## Decision

Change the default behavior to **soft-fail**:

- When max rounds are exhausted (`for...else`) or inner retries are exhausted (`round_ok = False`) and `best_result is not None`:
  1. Call `restore_arch_files_from_commit(repo, best_commit_sha)` to ensure the workspace is at the best-scoring commit (idempotent if rollback already ran).
  2. If files changed, create a commit: `"Accept best-effort sprint N (score X.XX / threshold Y.YY)"`.
  3. Set `sprint_status.status = "accepted"` (new status value, distinct from `"passed"` and `"failed"`).
  4. Set `sprint_status.final_score = best_result.average_score`.
  5. Continue to the next sprint's sprint-boundary commit and then the next sprint.

- If `best_result is None` (no critic round completed successfully), hard-stop regardless of mode — there is no usable artifact to accept.

A new `--strict` CLI flag restores the previous hard-stop behavior for users who want the run to halt when a sprint cannot meet its full exit criteria.

On `--resume`, sprints with status `"accepted"` are skipped (same as `"passed"`).

## Consequences

**Positive:**
- Runs always produce a complete set of architecture documents across all sprints, even when some sprints do not fully converge. The accepted sprint's best-scoring version is preserved in git history.
- Combines naturally with keep-best rollback: the best-effort file state is guaranteed to be at least as good as the best round seen in the sprint.
- Users who need strict quality gates can opt in with `--strict`.

**Negative:**
- The default no longer guarantees that all sprints meet exit criteria. The `"accepted"` status in `progress.json` makes this visible.
- Downstream consumers of `progress.json` must handle the new `"accepted"` status value.

## Implementation Notes

- `SprintStatus.status` gains a new literal value: `"accepted"`.
- `run_harness()` gains a keyword-only parameter `strict: bool = False`.
- `cli.py` exposes `--strict` (default: `False`).
- Both failure paths in `harness.py` check `not strict and best_result is not None and best_commit_sha is not None` before deciding whether to soft-fail or hard-stop.
- `restore_arch_files_from_commit` is already imported and used for mid-sprint rollback; the soft-fail path reuses it.
