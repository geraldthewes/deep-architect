# Resume/Checkpoint Support Implementation Plan (PROJ-0002)

## Overview

Enhance the existing sprint-level resume system to round-level granularity, move checkpoints to a dedicated `.checkpoints/` directory at repo root, add atomic writes, and add fail-fast behavior when `--resume` is passed without a checkpoint.

## Current State Analysis

Sprint-level resume already works end-to-end. The `--resume` CLI flag, `HarnessProgress`/`SprintStatus` models, `save_progress`/`load_progress` functions, and sprint-skip logic in `harness.py` are fully wired. The harness writes `progress.json` at 10+ call sites across every state transition.

What is missing:
- `progress.json` lives in `output_dir` (architecture output), not in an isolated `.checkpoints/` directory
- `save_progress` uses `Path.write_text()` — a non-atomic write; a kill during write leaves a corrupt file
- `--resume` with no checkpoint silently starts a fresh run instead of failing fast
- The round loop always starts from round 1; `sprint_status.rounds_completed` is persisted but never used to skip completed rounds on resume
- `consecutive_passes` is a local variable — lost on crash, never persisted
- No `.gitignore` entry for `.checkpoints/`
- Zero test coverage for the `resume=True` code path

## Desired End State

Running `adversarial-architect --prd knowledge/prd.md --output knowledge/architecture --resume` after a crash continues from the exact sprint and round where work was interrupted. No completed sprint or round is re-executed. Killing the process mid-round restarts that round from the beginning (generator re-runs), which is acceptable because architecture file writes and git commits are already on disk.

**Verify with:**
```bash
# Automated
uv run python -m pytest tests/ -v
uv run ruff check deep_architect/ tests/
uv run mypy deep_architect/
uv run bandit -r deep_architect/ -ll

# Manual
# 1. Start a run; kill it after sprint 2 passes
# 2. Run with --resume; verify sprint 3 starts (not sprint 1 or 2)
# 3. Kill mid-sprint after round 2 completes; --resume picks up at round 3
# 4. Pass --resume with no .checkpoints/ present; verify clear error message
# 5. Delete .checkpoints/ and run without --resume; verify clean fresh start
```

### Key Discoveries

- `save_progress` / `load_progress` are at `io/files.py:41-49`; both take `output_dir: Path` and construct the path `output_dir / "progress.json"` internally
- All 10+ `save_progress(output_dir, progress)` call sites are in `harness.py`
- `consecutive_passes` is initialized at `harness.py:229` and updated at lines 379/389; it is never written to the progress model
- `sprint_status.rounds_completed` is written at `harness.py:351` but there is no `save_progress` call immediately after — the next save is at the start of the following round (line 266)
- The repo root is available as `Path(repo.working_tree_dir)` from the `git.Repo` object returned by `validate_git_repo`
- `load_feedback(output_dir, sprint_number, round_num)` at `io/files.py:36` can reconstruct `last_result` for mid-sprint resume
- Existing harness tests mock `validate_git_repo` with a bare `MagicMock()` — after we call `repo.working_tree_dir`, those mocks will need `working_tree_dir` set to a real path

### Known Limitation: Generator Session Context Loss on Resume

The Claude Code CLI session ID (`generator_session_id`, `harness.py:230`) is an in-process handle to the agent's conversation history. When the process dies, the session is gone — there is no way to reconnect. On mid-sprint resume the generator starts with a fresh session. It retains access to on-disk files via Read/Grep/Glob tools, so it can reconstruct context from the architecture files already written, but it loses the iterative conversation history of "why I made these decisions." This is an inherent platform constraint, not a design flaw. It is documented in ADR-011 (updated in Phase 4).

## What We Are NOT Doing

- Round-level resume within a generator/critic call (mid-tool-call recovery) — out of scope per ticket
- Persisting or restoring `generator_session_id` — technically impossible (session dies with process)
- Tracking "file paths written" in the checkpoint — informational only, not needed for correctness
- Multiple named checkpoints or checkpoint archiving — MVP uses a single `progress.json` in `.checkpoints/`
- Any changes to `SprintContract`, `CriticResult`, `AgentConfig`, or the prompt files

---

## Phase 1: Storage Layer — Atomic Writes, Checkpoint Path, .gitignore

### Overview

Change `save_progress`/`load_progress` to write to an explicit `checkpoint_dir` rather than deriving the path from `output_dir`. Add atomic write (tmp + `os.replace`). Create `.checkpoints/` automatically on first write. Add `.gitignore` entry.

### Changes Required

#### 1. `deep_architect/io/files.py`

**Add `import os` at the top of the file** (after existing stdlib imports).

**Replace `save_progress` (lines 41-44):**

```python
def save_progress(checkpoint_dir: Path, progress: HarnessProgress) -> Path:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = checkpoint_dir / "progress.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(progress.model_dump_json(indent=2))
    os.replace(tmp, path)
    return path
```

**Replace `load_progress` (lines 47-49):**

```python
def load_progress(checkpoint_dir: Path) -> HarnessProgress:
    path = checkpoint_dir / "progress.json"
    return HarnessProgress.model_validate_json(path.read_text())
```

The parameter is renamed from `output_dir` to `checkpoint_dir` to make the semantic change explicit. All callers are in `harness.py` (updated in Phase 3).

#### 2. `.gitignore`

Add at the end of the file:

```
# Checkpoints (transient — safe to delete between runs)
.checkpoints/
```

### Success Criteria

#### Automated Verification
- [x] `uv run ruff check deep_architect/ tests/` passes
- [x] `uv run mypy deep_architect/` passes
- [x] `uv run python -m pytest tests/test_files.py -v` passes (existing tests still pass with updated call sites)

---

## Phase 2: Model Changes — `consecutive_passes` Field

### Overview

Add `consecutive_passes: int = 0` to `SprintStatus` so the value survives a crash and can be restored on mid-sprint resume.

### Changes Required

#### `deep_architect/models/progress.py`

**Add one field to `SprintStatus` (after `rounds_completed` at line 16):**

```python
consecutive_passes: int = 0
```

The full updated `SprintStatus` model:

```python
class SprintStatus(BaseModel):
    sprint_number: int
    sprint_name: str
    status: Literal["pending", "negotiating", "building", "evaluating", "passed", "failed"] = "pending"
    rounds_completed: int = 0
    consecutive_passes: int = 0
    final_score: float | None = None
```

No migration needed — existing `progress.json` files that lack this field will deserialize with the default value of `0`, which is the correct fallback for a fresh sprint.

### Success Criteria

#### Automated Verification
- [x] `uv run mypy deep_architect/` passes
- [x] `uv run python -m pytest tests/test_models.py tests/test_files.py -v` passes

---

## Phase 3: Harness Wiring — checkpoint_dir, Fail-Fast, Round-Level Resume

### Overview

Wire the storage layer changes into `harness.py`: compute `checkpoint_dir` from the git repo root, add fail-fast for missing checkpoints, update all `save_progress`/`load_progress` call sites, add round-level resume (start from `rounds_completed + 1`, restore `consecutive_passes`, reconstruct `last_result`), persist `consecutive_passes` after each completed round, and skip already-complete sprints on resume.

### Changes Required

#### `deep_architect/harness.py`

**1. Compute `checkpoint_dir` after `validate_git_repo` (around line 186):**

Insert immediately after the line `repo = validate_git_repo(output_dir)`:

```python
checkpoint_dir = Path(repo.working_tree_dir) / ".checkpoints"
```

**2. Replace the resume branch (lines 191-204):**

```python
if resume:
    checkpoint = checkpoint_dir / "progress.json"
    if not checkpoint.exists():
        raise FileNotFoundError(
            f"--resume passed but no checkpoint found at {checkpoint}. "
            "Run without --resume to start a fresh run, or restore a prior checkpoint."
        )
    progress = load_progress(checkpoint_dir)
    start_sprint_idx = progress.current_sprint - 1
    logger.info("Resuming from sprint %d", progress.current_sprint)
else:
    progress = HarnessProgress(
        total_sprints=len(SPRINTS),
        sprint_statuses=[
            SprintStatus(sprint_number=s.number, sprint_name=s.name) for s in SPRINTS
        ],
    )
    start_sprint_idx = 0
save_progress(checkpoint_dir, progress)
```

**3. Replace all `save_progress(output_dir, progress)` with `save_progress(checkpoint_dir, progress)`.**

There are 10 call sites in `harness.py`. Every occurrence of `save_progress(output_dir, progress)` becomes `save_progress(checkpoint_dir, progress)`. The lines affected are: 204 (now in the new block above), 226, 241, 250, 266, 302, 423, 433, 436, 441.

**4. Add already-complete sprint skip after `sprint_status` lookup (after line 223):**

```python
sprint_status = progress.sprint_statuses[sprint.number - 1]

# On resume, a sprint may already be passed/failed if the crash happened
# between completed_sprints++ (line 436) and the next sprint's current_sprint update (line 225).
if resume and sprint_status.status in ("passed", "failed"):
    logger.info(
        "[Sprint %d] Already %s — skipping", sprint.number, sprint_status.status
    )
    continue
```

**5. Replace the per-sprint local variable initialization block (lines 228-230) and add round-resume logic:**

```python
last_result: CriticResult | None = None
consecutive_passes = 0
generator_session_id: str | None = None  # persist context across rounds per sprint

# On mid-sprint resume: restore round state from checkpoint
start_round = sprint_status.rounds_completed + 1
if resume and sprint_status.rounds_completed > 0:
    consecutive_passes = sprint_status.consecutive_passes
    prior_feedback_path = (
        output_dir / "feedback"
        / f"sprint-{sprint.number}-round-{sprint_status.rounds_completed}.json"
    )
    if prior_feedback_path.exists():
        last_result = load_feedback(output_dir, sprint.number, sprint_status.rounds_completed)
    logger.info(
        "[Sprint %d] Resuming from round %d "
        "(rounds_completed=%d, consecutive_passes=%d) — generator session context lost",
        sprint.number, start_round,
        sprint_status.rounds_completed, consecutive_passes,
    )
```

**6. Update the round loop (line 232) to use `start_round`:**

```python
for round_num in range(start_round, t.max_rounds_per_sprint + 1):
```

**7. Persist `consecutive_passes` after each completed round.**

After `consecutive_passes` is updated (after line 389/390, before the ping-pong check at line 391), insert:

```python
sprint_status.consecutive_passes = consecutive_passes
save_progress(checkpoint_dir, progress)
```

This is the new round-completion checkpoint. At this point `sprint_status.rounds_completed` and `sprint_status.consecutive_passes` are both current. A crash between this save and the next round's start will correctly resume at `rounds_completed + 1`.

### Success Criteria

#### Automated Verification
- [x] `uv run ruff check deep_architect/ tests/` passes
- [x] `uv run mypy deep_architect/` passes
- [x] `uv run python -m pytest tests/ -v` passes (all existing tests pass)
- [x] `uv run bandit -r deep_architect/ -ll` passes

#### Manual Verification
- [ ] Kill a live run after sprint 2 passes; `--resume` picks up at sprint 3 with no completed sprint re-run
- [ ] Kill mid-sprint after round 2 completes; `--resume` picks up at round 3 with correct `last_result`
- [ ] Pass `--resume` with no `.checkpoints/` present; verify clear `FileNotFoundError` message
- [ ] Delete `.checkpoints/` and run without `--resume`; verify clean fresh start with no errors

---

## Phase 4: Update ADR-011

### Overview

ADR-011 documents sprint-level resume as the design. PROJ-0002 upgrades this to round-level. Update the ADR to reflect the new design, checkpoint location, atomic write approach, and the session context loss limitation.

### Changes Required

#### `knowledge/adr/ADR-011-resume-via-progress-json.md`

Replace the entire file contents with the updated ADR:

```markdown
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

**Files:** `deep_architect/io/files.py:41-49`, `deep_architect/models/progress.py`, `deep_architect/harness.py:191-210`
```

### Success Criteria

#### Automated Verification
- [x] File exists and is valid Markdown

---

## Phase 5: Tests

### Overview

Add test coverage for: atomic write behavior, checkpoint directory auto-creation, the fail-fast `--resume` error, mid-sprint resume (correct round start and `last_result` restoration), and sprint-skip on resume.

### Changes Required

#### `tests/test_files.py` — Three new tests

**Test 1: Checkpoint directory is auto-created by `save_progress`**

```python
def test_save_progress_creates_checkpoint_dir(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / ".checkpoints"
    assert not checkpoint_dir.exists()
    progress = HarnessProgress(
        total_sprints=2,
        sprint_statuses=[
            SprintStatus(sprint_number=i, sprint_name=f"Sprint {i}") for i in range(1, 3)
        ],
    )
    save_progress(checkpoint_dir, progress)
    assert checkpoint_dir.exists()
    assert (checkpoint_dir / "progress.json").exists()
```

**Test 2: No `.tmp` file left after a clean write**

```python
def test_save_progress_no_tmp_remnant(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=2,
        sprint_statuses=[
            SprintStatus(sprint_number=i, sprint_name=f"Sprint {i}") for i in range(1, 3)
        ],
    )
    save_progress(checkpoint_dir, progress)
    assert not (checkpoint_dir / "progress.tmp").exists()
    assert (checkpoint_dir / "progress.json").exists()
```

**Test 3: Round-trip with `consecutive_passes` field**

```python
def test_progress_round_trip_with_consecutive_passes(tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=2,
        sprint_statuses=[
            SprintStatus(sprint_number=1, sprint_name="Sprint 1", rounds_completed=3, consecutive_passes=1),
            SprintStatus(sprint_number=2, sprint_name="Sprint 2"),
        ],
    )
    save_progress(checkpoint_dir, progress)
    loaded = load_progress(checkpoint_dir)
    assert loaded.sprint_statuses[0].rounds_completed == 3
    assert loaded.sprint_statuses[0].consecutive_passes == 1
    assert loaded.sprint_statuses[1].consecutive_passes == 0
```

**Update the existing `test_progress_round_trip`** to pass a `checkpoint_dir` (e.g., `tmp_path / ".checkpoints"`) instead of bare `tmp_path`, since the function signature now expects a `checkpoint_dir`.

#### `tests/test_harness_retry.py` — Three new tests + fixture update

**Fixture update: set `working_tree_dir` on the mock repo**

The existing `_INFRA_PATCHES` mocks `validate_git_repo` with `return_value=MagicMock()`. After Phase 3, `harness.py` calls `repo.working_tree_dir` on that return value. Update all `validate_git_repo` patch calls to:

```python
mock_repo = MagicMock()
mock_repo.working_tree_dir = str(output_dir)
patch("deep_architect.harness.validate_git_repo", return_value=mock_repo),
```

This affects all existing tests in the file. The simplest approach is to extract a `_make_mock_repo(output_dir)` helper and use it everywhere.

**Test 1: `--resume` with no checkpoint fails fast**

```python
async def test_resume_fails_fast_without_checkpoint(output_dir: Path) -> None:
    prd = output_dir / "prd.md"
    prd.write_text("# PRD")
    mock_repo = MagicMock()
    mock_repo.working_tree_dir = str(output_dir)

    with (
        patch("deep_architect.harness.validate_git_repo", return_value=mock_repo),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        pytest.raises(FileNotFoundError, match="--resume passed but no checkpoint found"),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=True,
            config=_make_config(),
        )
```

**Test 2: `--resume` skips already-passed sprints**

```python
async def test_resume_skips_completed_sprints(output_dir: Path) -> None:
    prd = output_dir / "prd.md"
    prd.write_text("# PRD")
    mock_repo = MagicMock()
    mock_repo.working_tree_dir = str(output_dir)

    # Write a checkpoint with sprint 1 passed, sprint 2 pending
    from deep_architect.io.files import save_progress
    from deep_architect.models.progress import HarnessProgress, SprintStatus
    from deep_architect.sprints import SPRINTS
    checkpoint_dir = output_dir / ".checkpoints"
    progress = HarnessProgress(
        total_sprints=len(SPRINTS),
        current_sprint=2,
        completed_sprints=1,
        sprint_statuses=[
            SprintStatus(
                sprint_number=s.number,
                sprint_name=s.name,
                status="passed" if s.number == 1 else "pending",
            )
            for s in SPRINTS
        ],
    )
    save_progress(checkpoint_dir, progress)

    negotiate_calls: list[int] = []

    async def _record_negotiate(*args: object, **kwargs: object) -> object:
        sprint_arg = args[1]  # SprintDefinition
        negotiate_calls.append(sprint_arg.number)
        return _make_contract()

    with (
        patch("deep_architect.harness.validate_git_repo", return_value=mock_repo),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.negotiate_contract", side_effect=_record_negotiate),
        patch("deep_architect.harness.run_generator", new_callable=AsyncMock, return_value=None),
        patch("deep_architect.harness.run_critic", new_callable=AsyncMock, return_value=_passing_result()),
        patch("deep_architect.harness.check_ping_pong", new_callable=AsyncMock, return_value=MagicMock(similarity_score=0.0)),
    ):
        await run_harness(prd=prd, output_dir=output_dir, resume=True, config=_make_config())

    assert 1 not in negotiate_calls, "Sprint 1 should have been skipped"
    assert 2 in negotiate_calls, "Sprint 2 should have been executed"
```

**Test 3: Mid-sprint resume starts from correct round**

```python
async def test_resume_mid_sprint_starts_from_correct_round(output_dir: Path) -> None:
    prd = output_dir / "prd.md"
    prd.write_text("# PRD")
    mock_repo = MagicMock()
    mock_repo.working_tree_dir = str(output_dir)

    # Write checkpoint with sprint 1 at round 2 completed
    from deep_architect.io.files import save_progress, save_feedback
    from deep_architect.models.progress import HarnessProgress, SprintStatus
    from deep_architect.sprints import SPRINTS
    checkpoint_dir = output_dir / ".checkpoints"
    (output_dir / "feedback").mkdir(parents=True, exist_ok=True)
    (output_dir / "contracts").mkdir(parents=True, exist_ok=True)
    (output_dir / "decisions").mkdir(parents=True, exist_ok=True)
    (output_dir / "logs").mkdir(parents=True, exist_ok=True)

    prior_result = _passing_result()
    save_feedback(output_dir, 1, 2, prior_result)

    progress = HarnessProgress(
        total_sprints=len(SPRINTS),
        current_sprint=1,
        sprint_statuses=[
            SprintStatus(
                sprint_number=s.number,
                sprint_name=s.name,
                status="building" if s.number == 1 else "pending",
                rounds_completed=2 if s.number == 1 else 0,
                consecutive_passes=1 if s.number == 1 else 0,
            )
            for s in SPRINTS
        ],
    )
    save_progress(checkpoint_dir, progress)

    generator_rounds: list[int] = []

    async def _record_generator(*args: object, **kwargs: object) -> None:
        round_num = args[6]  # positional: config, sprint, contract, prd, last_result, output_dir, round_num
        generator_rounds.append(round_num)
        return None

    with (
        patch("deep_architect.harness.validate_git_repo", return_value=mock_repo),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.negotiate_contract", new_callable=AsyncMock, return_value=_make_contract()),
        patch("deep_architect.harness.run_generator", side_effect=_record_generator),
        patch("deep_architect.harness.run_critic", new_callable=AsyncMock, return_value=_passing_result()),
        patch("deep_architect.harness.check_ping_pong", new_callable=AsyncMock, return_value=MagicMock(similarity_score=0.0)),
    ):
        await run_harness(prd=prd, output_dir=output_dir, resume=True, config=_make_config())

    assert generator_rounds, "Generator should have been called"
    assert min(generator_rounds) == 3, (
        f"Resume should start at round 3 (rounds_completed=2), got first round={min(generator_rounds)}"
    )
    assert 1 not in generator_rounds and 2 not in generator_rounds, \
        "Rounds 1 and 2 should have been skipped"
```

### Success Criteria

#### Automated Verification
- [x] `uv run python -m pytest tests/ -v` — all tests pass, including 6 new tests
- [x] `uv run ruff check deep_architect/ tests/` passes
- [x] `uv run mypy deep_architect/` passes
- [x] `uv run bandit -r deep_architect/ -ll` passes

---

## Testing Strategy

### Unit Tests Summary

| Test | File | What it verifies |
|---|---|---|
| `test_save_progress_creates_checkpoint_dir` | `test_files.py` | `checkpoint_dir` auto-created |
| `test_save_progress_no_tmp_remnant` | `test_files.py` | Atomic write leaves no `.tmp` |
| `test_progress_round_trip_with_consecutive_passes` | `test_files.py` | New field round-trips correctly |
| `test_resume_fails_fast_without_checkpoint` | `test_harness_retry.py` | `FileNotFoundError` on missing checkpoint |
| `test_resume_skips_completed_sprints` | `test_harness_retry.py` | Passed sprints not re-executed |
| `test_resume_mid_sprint_starts_from_correct_round` | `test_harness_retry.py` | Round loop starts at `rounds_completed + 1` |

### Integration / Manual Tests

1. Start a real run, kill after sprint 2 completes (`Ctrl-C` or `kill`). Verify `.checkpoints/progress.json` exists and `current_sprint == 3`.
2. Run `--resume`. Verify log shows "Resuming from sprint 3" and sprint 1/2 contracts are untouched.
3. Start a run, kill mid-sprint (after a round log file appears but not the next one). Run `--resume`. Verify round starts at the correct number.
4. Run `--resume` with no `.checkpoints/`. Verify: exits non-zero, message contains the checkpoint path.
5. Delete `.checkpoints/` entirely, run without `--resume`. Verify: clean start, no errors.

## Implementation Order

Phases must be executed in order — each phase builds on the previous:

1. **Phase 1** (storage layer) first — changes `save_progress`/`load_progress` signatures
2. **Phase 2** (model) — adds `consecutive_passes` field
3. **Phase 3** (harness) — wires everything together; requires Phase 1 and 2
4. **Phase 4** (ADR) — documentation; can be done at any point but logically after the design is finalized
5. **Phase 5** (tests) — requires all implementation phases complete; also requires updating existing test mocks

## References

- Ticket: `knowledge/tickets/PROJ-0002.md`
- Research: `knowledge/research/2026-04-10-PROJ-0002-resume-checkpoint-support.md`
- ADR: `knowledge/adr/ADR-011-resume-via-progress-json.md`
- `deep_architect/io/files.py:41-49` — save/load progress
- `deep_architect/models/progress.py:10-28` — HarnessProgress, SprintStatus
- `deep_architect/harness.py:191-204` — resume branch
- `deep_architect/harness.py:228-232` — local variable init + round loop
- `deep_architect/harness.py:349-412` — round completion, consecutive_passes, exit criteria
- `tests/test_harness_retry.py` — existing harness tests with mock patterns
