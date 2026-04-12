---
date: 2026-04-10T20:19:00-04:00
researcher: Claude
git_commit: 0256d47
branch: master
repository: deep-architect
topic: "What already exists for resume/checkpoint support and what needs to change for PROJ-0002"
tags: [research, codebase, resume, checkpoint, harness, progress]
status: complete
last_updated: 2026-04-10
last_updated_by: Claude
---

# Research: Resume/Checkpoint Support for PROJ-0002

**Date**: 2026-04-10T20:19:00-04:00
**Researcher**: Claude
**Git Commit**: 0256d47
**Branch**: master
**Repository**: deep-architect

## Research Question

What already exists in the codebase for resume/checkpoint support, and what gaps need to be filled to satisfy PROJ-0002 requirements?

## Summary

**The resume system already works at sprint-level granularity.** The `--resume` CLI flag, `HarnessProgress` model, `save_progress`/`load_progress` functions, and sprint-skip logic in `harness.py` are all fully wired end-to-end. The harness writes `progress.json` at every status transition (10+ call sites). On resume, it reads the file and slices `SPRINTS[start_sprint_idx:]` to skip completed sprints.

**What's missing for PROJ-0002:**
1. No `.checkpoints/` directory — progress.json lives inside the output directory (gets committed)
2. No `.gitignore` entry for checkpoints
3. No atomic writes — `Path.write_text()` is used directly (no tmp + `os.replace`)
4. No round-level resume — a crashed mid-sprint always restarts that sprint from round 1
5. No `--resume` error handling when no checkpoint exists (silently falls through to fresh start)
6. No test coverage for the `resume=True` code path
7. No persistence of `generator_session_id` or `consecutive_passes` across restarts

## Detailed Findings

### 1. HarnessProgress / SprintStatus Models (`models/progress.py`)

**`SprintStatus`** (line 10-17):

| Field | Type | Default |
|---|---|---|
| `sprint_number` | `int` | required |
| `sprint_name` | `str` | required |
| `status` | `Literal["pending","negotiating","building","evaluating","passed","failed"]` | `"pending"` |
| `rounds_completed` | `int` | `0` |
| `final_score` | `float \| None` | `None` |

**`HarnessProgress`** (line 20-28):

| Field | Type | Default |
|---|---|---|
| `status` | `Literal["running","complete","failed"]` | `"running"` |
| `current_sprint` | `int` | `1` |
| `total_sprints` | `int` | required |
| `completed_sprints` | `int` | `0` |
| `total_rounds` | `int` | `0` |
| `started_at` | `datetime` | `datetime.now(UTC)` |
| `seed` | `int` | `int(time.time())` |
| `sprint_statuses` | `list[SprintStatus]` | required |

**Missing fields for round-level resume:**
- `current_round` — no field tracking which round within a sprint was in progress
- `generator_session_id` — ephemeral local variable in `harness.py:230`, never persisted
- `consecutive_passes` — resets to 0 on resume
- `last_result` reference — most recent `CriticResult` not referenced (can be reconstructed from disk)

**Verdict**: Sufficient for sprint-level resume. Needs new fields only if round-level resume is desired (currently out of scope per PROJ-0002).

### 2. CLI Flag (`cli.py`)

`cli.py:20-42` — `resume` is a `typer.Option(False, "--resume")` boolean, passed directly to `run_harness()`. No CLI-level logic; purely a pass-through. **Fully wired, not a stub.**

### 3. Harness Orchestration (`harness.py`)

**Resume branch** (lines 191-204):
```python
if resume and (output_dir / "progress.json").exists():
    progress = load_progress(output_dir)
    start_sprint_idx = progress.current_sprint - 1
else:
    progress = HarnessProgress(...)
    start_sprint_idx = 0
save_progress(output_dir, progress)
```

**Sprint loop** (line 208): `SPRINTS[start_sprint_idx:]` — skips completed sprints.

**Progress save call sites** (10+ locations):

| Line | Trigger |
|------|---------|
| 204 | Initial/resumed state write |
| 226 | After contract negotiation → `"building"` |
| 266 | Before generator runs → `"building"` |
| 303 | Before critic runs → `"evaluating"` |
| 241 | Max total rounds exceeded → `"failed"` |
| 249 | Timeout exceeded → `"failed"` |
| 387/405 | Sprint passes (consecutive or ping-pong) → `"passed"` |
| 423 | Max rounds exhausted → `"failed"` |
| 433 | Round retry exhaustion → `"failed"` |
| 437 | Sprint complete, increment `completed_sprints` |
| 442 | Harness complete → `"complete"` |

**Key behavioral note**: `progress.current_sprint` is updated at line 225 at the **start** of each sprint. A crash mid-sprint leaves `current_sprint` pointing at that sprint, causing resume to re-run it from round 1.

### 4. IO Layer (`io/files.py`)

Six public functions + `init_workspace`:
- `save_progress(output_dir, progress)` (line 41-44) — `model_dump_json(indent=2)` → `Path.write_text()`
- `load_progress(output_dir)` (line 47-49) — `model_validate_json()`, raises `FileNotFoundError` if missing
- `save_contract` / `load_contract` — per sprint: `contracts/sprint-N.json`
- `save_feedback` / `load_feedback` — per round: `feedback/sprint-N-round-M.json`
- `save_round_log` — `feedback/sprint-N-round-M-log.json`
- `init_workspace` — creates `contracts/`, `feedback/`, `decisions/`, `logs/`

**No atomic write protection** — `Path.write_text()` is a direct overwrite. A SIGKILL during write could leave a truncated `progress.json`.

### 5. .gitignore

**No entries** for `.checkpoints/`, `progress.json`, or any checkpoint-related paths. Only standard Python/tooling artifacts (`.venv/`, `__pycache__/`, `*.log`). The `progress.json` currently lives inside the `--output` directory and would be committed alongside architecture files.

### 6. Config (`config.py`)

`HarnessConfig` has no resume-specific fields. `ThresholdConfig` defines:
- `min_score=9.0`, `consecutive_passing_rounds=2`, `max_rounds_per_sprint=6`
- `max_total_rounds=40`, `timeout_hours=3.0`, `ping_pong_similarity_threshold=0.85`
- `max_round_retries=2`

### 7. Git Operations (`git_ops.py`)

Three functions: `validate_git_repo`, `get_modified_files`, `git_commit`. Used after each generator pass to auto-commit. No checkpoint-specific logic. Git history serves as a secondary durable record but is not used for resume.

### 8. Agent Client (`agents/client.py`)

`make_agent_options` accepts `resume: str | None` — this is the Claude CLI session-resume mechanism (passing a prior session ID). `ResultMessage` does not expose a `session_id` field for capture. Session IDs are ephemeral and lost on process restart.

### 9. Existing Tests

| File | What it tests |
|---|---|
| `tests/test_files.py` | All 6 io/files functions + `init_workspace` |
| `tests/test_models.py` | `CriticResult`, `SprintContract`, `ContractReviewResult` computed fields |
| `tests/test_exit_criteria.py` | `sprint_passes()` and `should_ping_pong_exit()` |
| `tests/test_harness_retry.py` | `run_harness()` with mocked LLM — generator/critic failure, max retries, session reset |
| `tests/test_config.py` | Config loading |
| `tests/test_prompts.py` | Prompt file existence |
| `tests/test_git_ops.py` | Git operations (real temp repos) |
| `tests/test_client.py` | Agent client |

**Resume test gaps:**
- `test_harness_retry.py` always uses `resume=False`
- `test_files.py:73-86` tests `HarnessProgress` round-trip but only with default values (all sprints pending)
- No test for `resume=True` with existing `progress.json`
- No test for `resume=True` without `progress.json`
- No test for mid-run progress state round-trip

### 10. ADR-011: Resume via progress.json

`knowledge/adr/ADR-011-resume-via-progress-json.md` documents the design decision:
- Sprint-level granularity only; round-level explicitly out of scope
- `--resume` is opt-in; default always starts fresh
- If `progress.json` is from a different run, user must delete manually
- Progress file is written "atomically" (single `write_text` call)

## Architecture Insights

### What Works Well
1. **Sprint-level resume is fully functional** — the architecture already supports skipping completed sprints
2. **Frequent progress writes** — 10+ call sites ensure state is captured at every transition
3. **Pydantic serialization** — clean round-trip via `model_dump_json` / `model_validate_json`
4. **All artifacts on disk** — contracts, feedback, and round logs are already persisted per-sprint/round

### What Needs Work for PROJ-0002
1. **Checkpoint location**: Move from `output_dir/progress.json` to `.checkpoints/` at repo root — or add `.checkpoints/` as a separate concern. The ticket specifies `.checkpoints/` should be gitignored and separate from output.
2. **Atomic writes**: Replace `Path.write_text()` with write-to-tempfile + `os.replace()` in `save_progress`
3. **Fail-fast on missing checkpoint**: When `resume=True` but no checkpoint exists, currently falls through to fresh start. Ticket requires a clear error.
4. **`.gitignore` update**: Add `.checkpoints/` entry
5. **Test coverage**: Add resume-specific tests (resume with checkpoint, resume without checkpoint, mid-run state round-trip)

### Design Question: `.checkpoints/` vs Current Approach

The ticket specifies `.checkpoints/` at repo root, but the current implementation stores `progress.json` inside the output directory. Two options:

**Option A** — Move `progress.json` to `.checkpoints/`:
- Pros: Matches ticket spec, gitignored, easy to wipe, separate from output
- Cons: Requires changing `save_progress`/`load_progress` signatures, need to pass repo root path

**Option B** — Keep `progress.json` in output dir, just gitignore it:
- Pros: Simpler change, collocated with the run's artifacts
- Cons: Mixes transient checkpoint with permanent architecture output

The planning phase should resolve this.

## Code References

- `deep_architect/models/progress.py:10-28` — HarnessProgress and SprintStatus models
- `deep_architect/io/files.py:41-49` — save_progress / load_progress
- `deep_architect/harness.py:191-204` — Resume branch logic
- `deep_architect/harness.py:208` — Sprint loop with start_sprint_idx slicing
- `deep_architect/harness.py:225-226` — current_sprint update + save at sprint start
- `deep_architect/cli.py:20-42` — --resume CLI flag
- `deep_architect/config.py:16-26` — ThresholdConfig (no resume fields)
- `deep_architect/agents/client.py:251-293` — make_agent_options with session resume param
- `deep_architect/git_ops.py:12-43` — Git operations
- `tests/test_files.py:73-86` — HarnessProgress round-trip test (default values only)
- `tests/test_harness_retry.py` — Harness tests (all resume=False)
- `knowledge/adr/ADR-011-resume-via-progress-json.md` — Design decision document

## Historical Context (from knowledge/)

- `knowledge/adr/ADR-011-resume-via-progress-json.md` — Explicit design decision: sprint-level granularity only, opt-in via `--resume`, single `write_text` as "atomic"
- `knowledge/tickets/PROJ-0001.md` — Parent ticket listing `--resume` as a must-have feature

## Open Questions

1. **Should checkpoints move to `.checkpoints/`?** The current implementation stores `progress.json` in the output directory. The ticket requests `.checkpoints/` at repo root. This is a design decision for the planning phase.
2. **Is the loss of `generator_session_id` on resume acceptable?** The Claude CLI session is lost, meaning the generator loses conversation context from prior rounds in the same sprint. Since resume restarts the sprint from round 1, this is consistent but worth noting.
3. **Should log files be continued on resume?** Currently each run creates a fresh timestamped log. On resume, there's no link to the prior run's log.
4. **Should `progress.json` be validated against the current PRD on resume?** ADR-011 notes this as a manual concern. A stale checkpoint from a different PRD could cause confusion.
