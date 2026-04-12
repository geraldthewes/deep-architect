# ADR-023: Keep-Best Hill Climbing with Rollback on Score Regression

**Status:** Accepted  
**Date:** 2026-04-12  
**Deciders:** Project design  
**Ticket:** PROJ-0005

---

## Context

The generator ↔ critic loop detected score regressions (logged "regressed") but took no corrective action. The next round started from the regressed files, potentially compounding the regression. Over multiple rounds this could drive quality downward rather than upward, wasting tokens and producing a worse final architecture.

## Decision

Implement hill-climbing with rollback (inspired by Karpathy's AutoResearch). After each critic round:

1. **Track best**: If the round is a new best score, record `best_result` and `best_commit_sha` (the HEAD commit SHA at that moment).
2. **Roll back on regression**: If `result.average_score < best_result.average_score - rollback_regression_threshold` (default 0.05) and the sprint is still running (not exiting via pass or ping-pong), restore arch files to the best-commit state, create a rollback commit, append a `[ROLLBACK]` entry to `generator-history.md`, and feed the best-round feedback into the next generator prompt.

**Excluded files** — `_EXCLUDED_FROM_ROLLBACK = {"generator-learnings.md", "generator-history.md", "critic-history.md"}` — these always accumulate and are never reverted.

**Sprint-scoped** — `best_result` and `best_commit_sha` reset at each sprint boundary. No cross-sprint rollback.

**Session-safe** — `best_commit_sha = None` on resume until the first new commit exists in the current session. Rollback is only triggered when a known-good commit exists to restore from.

**New git operations** in `git_ops.py`:
- `restore_arch_files_from_commit(repo, best_commit_sha)` — diffs `best_commit_sha → HEAD`, restores M/D files via `git checkout`, deletes added files from disk and index, skips excluded files. Returns list of affected paths.
- `git_commit_staged(repo, message)` — commits whatever is already in the git index (uses `repo.index.diff("HEAD")` rather than working-tree diff). Needed because `restore_arch_files_from_commit` stages directly to the index.

Rollback commit message format: `"Rollback sprint N round R: restore best (score X.X, was Y.Y)"`

## Rationale

- **Compounding regressions are worse than a missed round**: Without rollback, each bad round builds on the previous bad round. A single rollback absorbs the cost of one wasted generator call but prevents a cascading quality spiral.
- **Threshold over strict equality**: A 0.05 noise tolerance avoids rolling back on scoring noise. Configurable as `rollback_regression_threshold` in `ThresholdConfig`.
- **Excluded files preserve continuity**: `generator-learnings.md` contains the agent's working memory — reverting it would erase knowledge gained. `generator-history.md` and `critic-history.md` provide the full audit trail and must not be truncated.
- **Feed best-round feedback on rollback**: The generator should see the feedback that accompanied the best-scoring state, not the feedback that caused the regression. This maximizes the signal going into the next round.
- **Separate `git_commit_staged()`**: `git_commit()` detects changes via `git status` (working-tree diff). `repo.git.checkout(sha, '--', path)` stages to the index without touching the working tree. A dedicated staged-commit function avoids a mismatched detection path.
- **Best-state reset per sprint**: Sprints cover different architectural concerns. Carrying a best-state across sprint boundaries would restore files from a different design phase, which is semantically wrong.

## Consequences

- Regressing rounds produce two additional commits: the original generator commit (already there) and a rollback commit. `git log --oneline` will show interleaved `Rollback sprint N round R: ...` entries.
- `generator-history.md` accumulates `[ROLLBACK]` entries alongside normal round entries.
- `generator-learnings.md` is preserved across rollback — the agent retains lessons learned even from rounds that were rolled back.
- On resume, `best_result` is seeded by scanning prior round feedback files; `best_commit_sha` is left `None` (safe default — no rollback until a new session commit exists).
- Rollback only fires outside of pass/ping-pong exit paths — a round that exits the sprint never triggers rollback.

**Files:** `deep_architect/git_ops.py` (`_EXCLUDED_FROM_ROLLBACK`, `restore_arch_files_from_commit`, `git_commit_staged`), `deep_architect/io/files.py` (`append_rollback_event`), `deep_architect/harness.py` (best-tracking + rollback block), `deep_architect/config.py` (`rollback_regression_threshold`), `tests/test_git_ops.py`
