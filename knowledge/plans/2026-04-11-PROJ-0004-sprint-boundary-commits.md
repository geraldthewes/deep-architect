# Sprint-Boundary Git Commits — Implementation Plan

## Overview

Add two new commit points to the harness loop so that sprint-completion metadata and final-completion state are captured in git. This extends the existing per-round commit pattern (ADR-010) without modifying it.

## Current State Analysis

- Per-round generator commits fire at `harness.py:387-391` after each `run_generator()` call, using `get_modified_files(repo)` + `git_commit(repo, msg, written)`.
- Sprint completion increments `progress.completed_sprints` and saves at `harness.py:540-541`, but **no git commit follows** — critic feedback JSON, history files, and `progress.json` from the final round remain uncommitted.
- Final completion sets `progress.status = "complete"` and saves at `harness.py:545-546`, but **no git commit follows** — the terminal `progress.json` is never committed.
- `git_ops.py` provides `get_modified_files()` (returns untracked + modified paths) and `git_commit()` (stages, diffs HEAD, commits or no-ops if nothing changed). Both are fully reusable.

### Key Discoveries:
- `git_commit()` has built-in no-op logic (`git_ops.py:40-50`): if no paths exist or nothing changed after staging, it skips silently. This means the sprint-boundary commit is safe to call unconditionally.
- The `repo` variable is available throughout `run_harness()` (set at `harness.py:208-211`).
- `get_modified_files` and `git_commit` are already imported at `harness.py:14`.
- Harness tests in `test_harness_retry.py` patch both `get_modified_files` and `git_commit` via `_INFRA_PATCHES` (line 93-99), so existing tests will not break.

## Desired End State

After implementation:
1. `git log --oneline` for a multi-sprint run shows `"Sprint {N} complete: {sprint_name}"` commits interleaved with per-round commits.
2. The final commit in a successful run has message `"Architecture complete — all {N} sprints passed"`.
3. `git show` on any sprint-boundary commit includes the sprint's final critic feedback, updated `progress.json`, and history files.
4. All existing per-round commits (ADR-010) are unaffected.
5. All existing tests pass; new tests verify the boundary commits.

### Verification:
```bash
uv run ruff check deep_researcher/ tests/ && uv run mypy deep_researcher/ && uv run python -m pytest tests/ -v && uv run bandit -r deep_researcher/ -ll
```

## What We're NOT Doing

- Git tags (`sprint-{N}-complete`, `architecture-complete`) — deferred, may add later
- Squashing per-round commits within a sprint
- Changing per-round commit messages or behavior (ADR-010)
- Branching strategies per sprint

## Implementation Approach

Reuse the existing `get_modified_files()` + `git_commit()` pattern at two new call sites in `harness.py`. No new abstractions needed. Tests follow the established `test_git_ops.py` pattern of real temporary git repos.

---

## Phase 1: Production Code — Sprint-Boundary and Final-Completion Commits

### Overview
Add two commit call sites to `harness.py`. No changes to `git_ops.py`.

### Changes Required:

#### 1. Sprint-boundary commit after sprint passes
**File**: `deep_researcher/harness.py`
**Location**: After line 541 (`save_progress(checkpoint_dir, progress)` inside the sprint loop)

Insert immediately after line 541:

```python
        # Sprint-boundary commit: capture critic feedback, progress, and history
        written = get_modified_files(repo)
        git_commit(
            repo,
            f"Sprint {sprint.number} complete: {sprint.name}",
            written,
        )
```

#### 2. Final-completion commit after all sprints pass
**File**: `deep_researcher/harness.py`
**Location**: After line 546 (`save_progress(checkpoint_dir, progress)` in the final agreement block)

Insert immediately after line 546:

```python
    # Final commit: capture terminal progress state
    written = get_modified_files(repo)
    git_commit(
        repo,
        f"Architecture complete — all {progress.total_sprints} sprints passed",
        written,
    )
```

### No other file changes needed:
- `get_modified_files` and `git_commit` are already imported at `harness.py:14`
- `repo` is already in scope (set at line 208)
- `sprint` and `progress` are already in scope at both insertion points

---

## Phase 2: Tests — Verify Sprint-Boundary and Final-Completion Commits

### Overview
Add tests to `tests/test_git_ops.py` that verify the commit messages are created at the right points. Follow the established pattern: real `git.Repo.init(tmp_path)`, write files, call `git_commit()`, assert on `repo.head.commit.message`.

### Changes Required:

#### 1. Test: sprint-boundary commit message format
**File**: `tests/test_git_ops.py`
**What**: New test function `test_git_commit_sprint_boundary_message` that verifies the commit message format `"Sprint {N} complete: {name}"` works correctly with `git_commit()`.

```python
def test_git_commit_sprint_boundary_message(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    feedback = tmp_path / "feedback.json"
    feedback.write_text('{"status": "passed"}')
    progress = tmp_path / "progress.json"
    progress.write_text('{"completed_sprints": 1}')

    message = "Sprint 1 complete: C4 Context View"
    git_commit(repo, message, [feedback, progress])

    assert repo.head.commit.message == message
```

#### 2. Test: final-completion commit message format
**File**: `tests/test_git_ops.py`
**What**: New test function `test_git_commit_final_completion_message` that verifies the final commit message format.

```python
def test_git_commit_final_completion_message(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    progress = tmp_path / "progress.json"
    progress.write_text('{"status": "complete"}')

    message = "Architecture complete — all 7 sprints passed"
    git_commit(repo, message, [progress])

    assert repo.head.commit.message == message
```

#### 3. Test: sprint-boundary commit is no-op when nothing changed
**File**: `tests/test_git_ops.py`
**What**: New test function `test_git_commit_sprint_boundary_noop` that verifies no commit is created if all files were already committed by the last generator round.

```python
def test_git_commit_sprint_boundary_noop(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    # Simulate: generator already committed everything
    f = tmp_path / "file.md"
    f.write_text("content")
    git_commit(repo, "Generator pass 3 - sprint 1 (Context)", [f])
    sha_after_gen = repo.head.commit.hexsha

    # Sprint-boundary commit should be a no-op
    git_commit(repo, "Sprint 1 complete: C4 Context View", [])

    assert repo.head.commit.hexsha == sha_after_gen
```

#### 4. Test: harness creates sprint-boundary commit (integration-style)
**File**: `tests/test_harness_retry.py`
**What**: New test function `test_harness_creates_sprint_boundary_commit` that patches the agent calls, lets `get_modified_files` and `git_commit` run against a real repo, and verifies commit messages in the git log after a single-sprint run.

This test follows the existing `output_dir` fixture pattern with `git.Repo.init`, identity config, and initial commit. It patches `run_generator`, `run_critic`, `propose_contract`, `review_contract`, `run_preflight_check`, `run_final_agreement`, and `setup_logging` but **does not** patch `get_modified_files` or `git_commit` — those run for real.

The mock generator writes a file to disk on each call. After `run_harness()` completes, the test inspects `list(repo.iter_commits())` and asserts:
- At least one commit message matches `"Sprint 1 complete: ..."` 
- The last commit message matches `"Architecture complete — all ... sprints passed"`

### Success Criteria:

#### Automated Verification:
- [x] All existing tests pass: `uv run python -m pytest tests/ -v`
- [x] New tests pass: `uv run python -m pytest tests/test_git_ops.py tests/test_harness_retry.py -v`
- [x] Linting clean: `uv run ruff check deep_researcher/ tests/`
- [x] Type checking clean: `uv run mypy deep_researcher/`
- [x] Security scan clean: `uv run bandit -r deep_researcher/ -ll`

#### Manual Verification:
- [ ] Run a multi-sprint harness; confirm `git log --oneline` shows sprint-boundary commits interleaved with per-round commits
- [ ] `git show` on a sprint-boundary commit includes feedback, progress, and history files

**Implementation Note**: After completing this phase and all automated verification passes, pause for manual confirmation before considering the ticket complete.

---

## Testing Strategy

### Unit Tests (Phase 2, items 1-3):
- Verify commit message format for sprint-boundary and final-completion commits
- Verify no-op behavior when nothing changed since last generator commit
- All use real temporary git repos (established pattern)

### Integration Test (Phase 2, item 4):
- Full harness run with mocked agents but real git operations
- Verify commit messages appear in git log in correct order
- Follows established `test_harness_retry.py` patterns

### Edge Cases Covered:
- Sprint-boundary commit is a no-op if generator already committed everything (no duplicate empty commits)
- Final-completion commit captures terminal `progress.json` even if no architecture files changed in the final agreement round

## References

- Original ticket: `knowledge/tickets/PROJ-0004.md`
- ADR-010: `knowledge/adr/ADR-010-git-detection-auto-commit.md` — per-round commit pattern this extends
- ADR-011: `knowledge/adr/ADR-011-resume-via-progress-json.md` — resume via progress.json, benefits from progress being committed
- Production code: `deep_researcher/harness.py` (lines 540-546), `deep_researcher/git_ops.py`
- Test patterns: `tests/test_git_ops.py`, `tests/test_harness_retry.py`
