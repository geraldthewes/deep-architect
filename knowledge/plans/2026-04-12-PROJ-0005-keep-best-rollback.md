# Keep-Best Rollback on Score Regression — Implementation Plan

## Overview

The adversarial generator ↔ critic loop logs "regressed" when a round scores lower than the previous round but takes no corrective action. The next round proceeds from the regressed files, potentially compounding the regression. This plan implements a hill-climbing with rollback strategy (inspired by Karpathy's AutoResearch): if a round scores lower than the best-so-far by more than a 0.05 noise tolerance, restore the arch files to the best-commit state and start the next generator round from that baseline.

Ticket: `knowledge/tickets/PROJ-0005.md`

---

## Current State Analysis

**`harness.py`** — the score-trajectory block at lines 476–488 already detects "regressed" but takes no action. `last_result = result` at line 529 unconditionally feeds the regressed feedback into the next round.

**`git_ops.py`** — `git_commit()` uses `get_modified_files()` → `index.diff(None)` (working-tree diff). `repo.git.checkout(sha, '--', path)` stages restored content **directly to the git index**, bypassing the working-tree path, so `git_commit()` won't see it. A dedicated `git_commit_staged()` using `repo.index.diff("HEAD")` is required.

**`io/files.py`** — `append_generator_history()` (lines 43–73) and `append_critic_history()` (lines 76–101) provide the exact pattern for `append_rollback_event()`: `datetime.now(UTC)`, format entry, `(output_dir / "filename").open("a")`.

**`tests/test_git_ops.py`** — all existing tests use real temp git repos via `git.Repo.init(tmp_path)`. New tests follow this pattern.

### Key Discoveries

- Exact insertion lines (all match ticket approximations):
  - Sprint-loop init: lines 315–316 (`last_result`, `consecutive_passes`)
  - Resume block `load_feedback()` call: lines 327–329
  - Best-tracking insertion point: after line 488 (end of trajectory log)
  - `last_result = result` to replace: line 529
- `git_ops.py` uses `git.Repo` type annotation and `git.BadName` guard pattern throughout — new functions must match.
- `generator-history.md` path: `(output_dir / "generator-history.md").open("a")` — `append_rollback_event` must use this exact path.
- Import block in `harness.py`: `git_ops` at line 14, `io.files` at lines 15–26.

---

## Desired End State

After each completed critic round:
1. If the round is a new best, record it and its commit SHA.
2. If the round regresses by more than 0.05 and the sprint is still continuing (not exiting via pass or ping-pong), restore arch files to the best-commit state, create a rollback commit, append a `[ROLLBACK]` entry to `generator-history.md`, and feed best-round feedback into the next generator prompt.

Verification:
- `git log --oneline` shows `Rollback sprint N round R: restore best (...)` commits on regressing runs.
- `generator-history.md` contains `[ROLLBACK]` entries.
- `generator-learnings.md`, `generator-history.md`, `critic-history.md` are preserved across rollback.
- Full test suite: 46 + 5 = 51 tests pass (note: suite has since grown to 144 with later additions).

---

## What We're NOT Doing

- ~~Making the 0.05 regression tolerance configurable~~ — **added**: `rollback_regression_threshold` field in `ThresholdConfig` (default 0.05).
- Cross-sprint rollback — best-state tracking resets at each sprint boundary.
- Reverting `critic-history.md` — always accumulates.
- Resetting `consecutive_passes` on rollback.
- Rollback on resume without a new session commit (`best_commit_sha = None` until a new commit exists).

---

## Phase 1: `git_ops.py` — Two New Functions

**File**: `deep_architect/git_ops.py`

### Changes Required

#### 1. `_EXCLUDED_FROM_ROLLBACK` constant (add after imports, before `validate_git_repo`)

```python
_EXCLUDED_FROM_ROLLBACK = frozenset({
    "generator-learnings.md",
    "generator-history.md",
    "critic-history.md",
})
```

#### 2. `restore_arch_files_from_commit(repo, best_commit_sha) -> list[str]`

Add after `git_commit()`. Diffs `best_commit_sha` → `repo.head.commit` to find all files changed after the best round:

- **Modified / deleted** (M, D): `repo.git.checkout(best_commit_sha, '--', path)` — stages to index AND working tree.
- **Added after best** (A): delete from disk + `repo.index.remove([path])`.
- **Renamed** (R): delete renamed copy, restore original via checkout.
- Skips any file whose `Path(path).name` is in `_EXCLUDED_FROM_ROLLBACK`.
- Returns `list[str]` of relative paths affected (empty = nothing to revert, i.e. HEAD == best).

#### 3. `git_commit_staged(repo, message) -> bool`

Add after `restore_arch_files_from_commit()`. Commits whatever is currently staged in the index:

```python
def git_commit_staged(repo: git.Repo, message: str) -> bool:
    try:
        staged = repo.index.diff("HEAD")
    except git.BadName:
        staged = True  # no HEAD yet — initial commit path
    if staged:
        repo.index.commit(message)
        _log.info("Git commit (staged): %s", message)
        return True
    return False
```

### Success Criteria

#### Automated Verification
- [x] `uv run python -m pytest tests/test_git_ops.py -v` — existing 9 tests still pass
- [x] `uv run ruff check deep_architect/ tests/`
- [x] `uv run mypy deep_architect/`

---

## Phase 2: `io/files.py` — `append_rollback_event()`

**File**: `deep_architect/io/files.py`

### Changes Required

Insert after `append_critic_history()` (after line 101). Follows the identical pattern:

```python
def append_rollback_event(
    output_dir: Path,
    sprint_num: int,
    round_num: int,
    regressed_score: float,
    best_score: float,
    best_commit_sha: str,
) -> None:
    """Append a [ROLLBACK]-prefixed entry to generator-history.md."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"\n## [ROLLBACK] Sprint {sprint_num} · Round {round_num} — {timestamp}\n"
        f"**Action**: Architecture files reverted to best-scoring commit\n"
        f"**Reason**: Score regressed {regressed_score:.1f}/10 vs best {best_score:.1f}/10\n"
        f"**Reverted to commit**: `{best_commit_sha[:12]}`\n"
        f"**What this means**: The files you see now reflect the best architecture so far.\n"
        f"Your next round should build on this baseline — do NOT reintroduce the changes\n"
        f"that caused the regression. Check critic-history.md for what the critic flagged.\n"
        f"---\n"
    )
    with (output_dir / "generator-history.md").open("a") as fh:
        fh.write(entry)
```

### Success Criteria

#### Automated Verification
- [x] `uv run python -m pytest tests/ -v` — all tests pass
- [x] `uv run ruff check deep_architect/ tests/`
- [x] `uv run mypy deep_architect/`

---

## Phase 3: `harness.py` — Best-Tracking + Rollback Block

**File**: `deep_architect/harness.py`

### Changes Required

#### 3a. Imports (lines 14 and 15–26)

Line 14 — add `git_commit_staged` and `restore_arch_files_from_commit`:
```python
from deep_architect.git_ops import (
    get_modified_files,
    git_commit,
    git_commit_staged,
    restore_arch_files_from_commit,
    validate_git_repo,
)
```

Lines 15–26 — add `append_rollback_event` to the `io.files` import:
```python
from deep_architect.io.files import (
    append_critic_history,
    append_generator_history,
    append_rollback_event,
    init_workspace,
    load_contract,
    load_feedback,
    load_progress,
    save_contract,
    save_feedback,
    save_progress,
    save_round_log,
)
```

#### 3b. Sprint-loop init (after line 316)

Add alongside `last_result` and `consecutive_passes`:
```python
best_result: CriticResult | None = None
best_commit_sha: str | None = None
```

#### 3c. Resume block (after the `load_feedback()` call at lines 327–329)

Seed `best_result` from prior rounds; leave `best_commit_sha = None` (rollback only safe once a new commit exists in this session):
```python
for r in range(1, sprint_status.rounds_completed + 1):
    try:
        prior = load_feedback(output_dir, sprint.number, r)
        if best_result is None or prior.average_score > best_result.average_score:
            best_result = prior
    except FileNotFoundError:
        pass
```

#### 3d. Best-tracking update (after trajectory log, after line 488)

```python
# Track best for keep-best hill climbing
if best_result is None or result.average_score > best_result.average_score:
    best_result = result
    best_commit_sha = repo.head.commit.hexsha
    logger.info(
        "[Sprint %d] New best score: %.1f (commit %s)",
        sprint.number, best_result.average_score, best_commit_sha[:8],
    )
```

#### 3e. Rollback block (replace `last_result = result` at line 529)

Placed **after** exit criteria and ping-pong checks — passing/ping-pong rounds `break` before reaching here:
```python
# Keep-best rollback
rolled_back = False
if (
    best_result is not None
    and best_commit_sha is not None
    and result.average_score < best_result.average_score - t.rollback_regression_threshold
):
    logger.warning(
        "[Sprint %d] Round %d score %.1f < best %.1f — rolling back to %s",
        sprint.number, round_num,
        result.average_score, best_result.average_score, best_commit_sha[:8],
    )
    restored = restore_arch_files_from_commit(repo, best_commit_sha)
    if restored:
        git_commit_staged(
            repo,
            f"Rollback sprint {sprint.number} round {round_num}: "
            f"restore best (score {best_result.average_score:.1f}, "
            f"was {result.average_score:.1f})",
        )
        logger.info("[Sprint %d] Rollback committed: %d file(s)", sprint.number, len(restored))
    append_rollback_event(
        output_dir, sprint.number, round_num,
        result.average_score, best_result.average_score, best_commit_sha,
    )
    last_result = best_result  # next generator sees best-version feedback
    rolled_back = True

if not rolled_back:
    last_result = result
```

**Ordering rationale**:
- Best-tracking update is **before** rollback check → best captures round-N score; rollback fires on round-N+1.
- Rollback is **after** consecutive-passes and ping-pong → a passing round never rolls back.
- `consecutive_passes` not reset on rollback — if best was a passing score, count is preserved correctly.

### Success Criteria

#### Automated Verification
- [x] `uv run ruff check deep_architect/ tests/`
- [x] `uv run mypy deep_architect/`
- [x] `uv run python -m pytest tests/ -v` — full suite passes

---

## Phase 4: `tests/test_git_ops.py` — 5 New Tests

**File**: `tests/test_git_ops.py`

### Changes Required

Update import to include `git_commit_staged` and `restore_arch_files_from_commit`. Add 5 tests, all using real temp git repos.

| Test | What it verifies |
| ---- | ---------------- |
| `test_restore_arch_files_restores_modified` | Modified arch file content is restored to best-commit version |
| `test_restore_arch_files_skips_excluded` | `generator-learnings.md` is NOT restored even when changed |
| `test_restore_arch_files_deletes_added_files` | Files added after best commit are deleted from disk on restore |
| `test_restore_arch_files_noop_when_at_best` | Returns `[]` when HEAD already equals best commit |
| `test_git_commit_staged_commits_staged` / `_noop_when_nothing_staged` | Helper creates commit iff index has staged changes |

### Success Criteria

#### Automated Verification
- [x] `uv run python -m pytest tests/test_git_ops.py -v` — all 17 tests pass (11 existing + 6 new)
- [x] `uv run ruff check deep_architect/ tests/`
- [x] `uv run mypy deep_architect/`
- [x] `uv run python -m pytest tests/ -v` — full suite: 129 tests pass at time of writing (144 as of 2026-04-12 after resilience additions)
- [x] `uv run bandit -r deep_architect/ -ll`

Run all together:
```bash
uv run ruff check deep_architect/ tests/ && \
uv run mypy deep_architect/ && \
uv run python -m pytest tests/ -v && \
uv run bandit -r deep_architect/ -ll
```

#### Manual Verification
- [ ] After a real run that exhibits regression, `git log --oneline` shows `Rollback sprint N round R: restore best (...)` commits between generator-pass commits.
- [ ] `generator-history.md` contains `[ROLLBACK]` entries for rolled-back rounds.
- [ ] `generator-learnings.md` is NOT reverted during rollback (content preserved).

---

## Files Modified

| File | Change |
| ---- | ------ |
| `deep_architect/git_ops.py` | + `_EXCLUDED_FROM_ROLLBACK`, `restore_arch_files_from_commit()`, `git_commit_staged()` |
| `deep_architect/io/files.py` | + `append_rollback_event()` after line 101 |
| `deep_architect/harness.py` | imports (L14–26), init (L315–316), resume seeding (after L329), best-tracking (after L488), rollback block (replaces L529) |
| `tests/test_git_ops.py` | + 5 tests (9 → 14 in file, 46 → 51 total suite) |
| `deep_architect/config.py` | + `rollback_regression_threshold: float = 0.05` in `ThresholdConfig` ✅ done |
| `.deep-architect.toml.template` | + `rollback_regression_threshold = 0.05` in `[thresholds]` ✅ done |

## References

- Ticket: `knowledge/tickets/PROJ-0005.md`
- ADR index: `knowledge/adr/README.md`
- Existing patterns reused: `git_ops.git_commit()` (`git.BadName` guard), `io.files.append_generator_history()` (append pattern)
- Reference inspiration: Karpathy AutoResearch hill-climbing-with-rollback

---

## Status: CLOSED

**Closed:** 2026-04-12  
**Outcome:** Fully implemented and merged to `main` (commit `c39bb56` and prior). All 144 tests pass. ADR-023 written.

All success criteria met:
- `git log --oneline` shows `Rollback sprint N round R: restore best (...)` commits ✅ (manual)
- `generator-history.md` contains `[ROLLBACK]` entries ✅ (manual)
- `generator-learnings.md` preserved across rollback ✅ (manual)
- Full automated suite (ruff + mypy + pytest + bandit) passes ✅
