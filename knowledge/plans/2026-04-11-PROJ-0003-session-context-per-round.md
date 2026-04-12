# PROJ-0003: Session Context Per Round — Implementation Plan

## Overview

Eliminate session_id reuse across generator rounds. Each generator turn starts a fresh Claude CLI
session; all context arrives exclusively from files. Two structured Markdown history files
(`generator-history.md`, `critic-history.md`) replace the implicit memory that session continuity
previously provided, giving agents a searchable record of prior decisions and concerns.

## Current State Analysis

- **Generator** reuses `session_id` across rounds within a sprint (`harness.py:303-375`).
  Session is reset to `None` only on crash (`harness.py:430-431`) or between sprints.
- **Critic** is already stateless — it never uses `session_id` (`critic.py:40-81`).
- On `--resume`, `generator_session_id` stays `None` — the system already handles the no-session
  case. The resume log says "generator session context lost" (`harness.py:318-323`).
- All explicit context (PRD, contract, feedback, learnings) is re-injected every round; session
  continuity only provides implicit memory of the generator's own prior tool calls.
- `generator-learnings.md` is written by the LLM itself via Write/Edit tools, read and injected
  fully into every generator prompt (`generator.py:76-85`). It is the agent's subjective working
  memory. History files will be the harness's objective structured record — these serve distinct,
  complementary purposes and will coexist.

## Desired End State

- `run_generator()` has no `session_id` or `last_known_input_tokens` parameters; `make_agent_options()`
  is called with `resume=None`.
- `harness.py` does not track `generator_session_id` or `last_generator_input_tokens`.
- After each generator round, the harness appends a structured entry to `{output_dir}/generator-history.md`.
- After each critic round, the harness appends a structured entry to `{output_dir}/critic-history.md`.
- Both `run_generator()` and `run_critic()` inject the history file path (not content) into the prompt
  when the file exists; agents search it via Read/Grep.
- `clean_run_artifacts()` deletes both history files on fresh start.
- `--resume` loads history files automatically (they are on disk; no special-case logic).
- All existing tests pass; new tests cover history append and session-removal behavior.
- ADR-004 is superseded; ADR-021 documents the new decision.

### Verification

```bash
uv run ruff check deep_architect/ tests/ && \
uv run mypy deep_architect/ && \
uv run python -m pytest tests/ -v && \
uv run bandit -r deep_architect/ -ll
```

## What We're NOT Doing

- Changing `run_critic()` session behavior (already stateless — no changes needed).
- Removing `GeneratorRoundResult.session_id` — the SDK still returns it; keep for logging.
- Merging `generator-learnings.md` into history — they serve different purposes.
- Adding history files to the `decisions/` subdirectory — `output_dir/` root matches the
  existing `generator-learnings.md` convention.
- Changing structured output format (`CriticResult`) or exit criteria logic.

## Key Discoveries

- `generator.py:43,45` — `session_id` and `last_known_input_tokens` are keyword-only parameters
  with defaults of `None`/`0`. Removing them is non-breaking at the call site once harness.py is
  updated.
- `harness.py:374-375` — `generator_session_id = gen_round.session_id` and
  `last_generator_input_tokens = gen_round.input_tokens` are the only two lines that feed the
  session state forward. Deleting these two lines (plus initialization at 303-304 and the exception
  reset at 430-431) completes Phase 1.
- `harness.py:382` — `written = get_modified_files(repo)` is already computed before `git_commit()`.
  The generator history entry must be appended after this line (it needs `written` for the files list).
- `harness.py:410-421` — `save_round_log()` is the last thing written after a critic round.
  Critic history entry is appended immediately after.
- `io/files.py` has no `datetime` import; `progress.py` already uses `from datetime import UTC, datetime`
  confirming Python 3.11+ is in use.
- The `datetime` import needed in `files.py` is `from datetime import UTC, datetime`.
- `harness.py:15-24` imports from `deep_architect.io.files` as an explicit list; the two new
  functions must be added to this import.
- `test_harness_retry.py:267-318` — `test_harness_resets_generator_session_on_retry` tests
  behavior that no longer exists after Phase 1. Replace it with a test that verifies the generator
  is invoked without `session_id` across rounds (the parameter no longer exists).

---

## Phase 1: Remove Session_id Reuse

### Overview

Remove all session threading from the generator. The generator starts a fresh session every
round. This is a pure deletion — no new functionality, just removing the plumbing that was
carrying `session_id` forward between rounds.

### Changes Required

#### 1. `deep_architect/agents/generator.py`

**Remove `session_id` and `last_known_input_tokens` parameters from `run_generator()`.**

Change the signature from:
```python
async def run_generator(
    config: AgentConfig,
    sprint: SprintDefinition,
    contract: SprintContract,
    prd_content: str,
    previous_feedback: CriticResult | None,
    output_dir: Path,
    round_num: int,
    *,
    cli_path: str | None = None,
    session_id: str | None = None,
    supplementary_context: str = "",
    last_known_input_tokens: int = 0,
) -> GeneratorRoundResult:
    """Run the Generator for one round. Returns session and token state for continuation."""
```

To:
```python
async def run_generator(
    config: AgentConfig,
    sprint: SprintDefinition,
    contract: SprintContract,
    prd_content: str,
    previous_feedback: CriticResult | None,
    output_dir: Path,
    round_num: int,
    *,
    cli_path: str | None = None,
    supplementary_context: str = "",
) -> GeneratorRoundResult:
    """Run the Generator for one round."""
```

**Remove the session_id log block** (lines 48-54):
```python
# DELETE this block:
if session_id:
    _log.info(
        "[Generator sprint=%d round=%d] Continuing session %s...",
        sprint.number, round_num, session_id[:12],
    )
else:
    _log.info("[Generator sprint=%d round=%d] Starting new session", sprint.number, round_num)
```

Replace with a single log line:
```python
_log.info("[Generator sprint=%d round=%d] Starting new session", sprint.number, round_num)
```

**Remove `resume=session_id`** from `make_agent_options()` call (line 111):
```python
# BEFORE:
options = make_agent_options(
    config,
    system_prompt,
    allowed_tools=GENERATOR_TOOLS,
    cwd=str(output_dir),
    cli_path=cli_path,
    resume=session_id,
)

# AFTER:
options = make_agent_options(
    config,
    system_prompt,
    allowed_tools=GENERATOR_TOOLS,
    cwd=str(output_dir),
    cli_path=cli_path,
)
```

**Remove `last_known_input_tokens=last_known_input_tokens`** from `run_agent()` call (line 119):
```python
# BEFORE:
result = await run_agent(
    options, prompt, label=label,
    max_retries=config.max_agent_retries,
    context_window=config.context_window,
    last_known_input_tokens=last_known_input_tokens,
)

# AFTER:
result = await run_agent(
    options, prompt, label=label,
    max_retries=config.max_agent_retries,
    context_window=config.context_window,
)
```

**Update `GeneratorRoundResult` docstring** (line 24):
```python
# BEFORE:
"""Result of one generator round, used to thread session state across rounds."""

# AFTER:
"""Result of one generator round."""
```

#### 2. `deep_architect/harness.py`

**Remove sprint-local session tracking** (lines 303-304):
```python
# DELETE both lines:
generator_session_id: str | None = None  # persist context across rounds per sprint
last_generator_input_tokens: int = 0     # last round's token count for ctx logging
```

**Remove session arguments from `run_generator()` call** (lines 370-372):
```python
# BEFORE:
gen_round: GeneratorRoundResult = await run_generator(
    config.generator,
    sprint,
    contract,
    prd_content,
    last_result,
    output_dir,
    round_num,
    cli_path=cli_path,
    session_id=generator_session_id,
    supplementary_context=supplementary_context,
    last_known_input_tokens=last_generator_input_tokens,
)

# AFTER:
gen_round: GeneratorRoundResult = await run_generator(
    config.generator,
    sprint,
    contract,
    prd_content,
    last_result,
    output_dir,
    round_num,
    cli_path=cli_path,
    supplementary_context=supplementary_context,
)
```

**Remove session capture** (lines 374-375):
```python
# DELETE both lines:
generator_session_id = gen_round.session_id
last_generator_input_tokens = gen_round.input_tokens
```

**Remove session reset in exception handler** (lines 430-431):
```python
# DELETE both lines:
generator_session_id = None       # reset corrupted session
last_generator_input_tokens = 0  # context lost with session
```

**Update the resume log message** (lines 318-323) — remove the "session context lost" language since
fresh sessions are now the standard (not a loss):
```python
# BEFORE:
logger.info(
    "[Sprint %d] Resuming from round %d "
    "(rounds_completed=%d, consecutive_passes=%d) — generator session context lost",
    sprint.number, start_round,
    sprint_status.rounds_completed, consecutive_passes,
)

# AFTER:
logger.info(
    "[Sprint %d] Resuming from round %d (rounds_completed=%d, consecutive_passes=%d)",
    sprint.number, start_round,
    sprint_status.rounds_completed, consecutive_passes,
)
```

#### 3. `tests/test_harness_retry.py`

**Replace `test_harness_resets_generator_session_on_retry`** (lines 267-318) with a test that
verifies `run_generator()` is called without `session_id` across all rounds (since the parameter
no longer exists, this is a signature-level assertion):

```python
async def test_harness_generator_receives_no_session_id(
    output_dir: Path,
) -> None:
    """Each generator call starts a new session — session_id is not a parameter."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")

    import inspect
    from deep_architect.agents.generator import run_generator
    sig = inspect.signature(run_generator)
    assert "session_id" not in sig.parameters, (
        "run_generator() must not have a session_id parameter — sessions are per-turn only"
    )
```

This is a static assertion. Pair it with an integration-style test that verifies the harness
runs two rounds without error (session state not tracked between them):

```python
async def test_harness_runs_multiple_rounds_stateless(
    output_dir: Path,
) -> None:
    """Harness completes two rounds without session threading."""
    prd = output_dir / "prd.md"
    prd.write_text("# Test PRD")
    round_count = 0

    async def _fake_generator(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal round_count
        round_count += 1
        return GeneratorRoundResult(session_id="sdk-session", input_tokens=0)

    with (
        patch("deep_architect.harness.run_preflight_check", new_callable=AsyncMock),
        patch("deep_architect.harness.run_final_agreement", new_callable=AsyncMock),
        patch(
            "deep_architect.harness.validate_git_repo",
            return_value=_make_mock_repo(output_dir),
        ),
        patch("deep_architect.harness.get_modified_files", return_value=[]),
        patch("deep_architect.harness.git_commit"),
        patch("deep_architect.harness.setup_logging", return_value=Path("/tmp/test.log")),
        patch(
            "deep_architect.harness.negotiate_contract",
            new_callable=AsyncMock,
            return_value=_make_contract(),
        ),
        patch("deep_architect.harness.run_generator", side_effect=_fake_generator),
        patch(
            "deep_architect.harness.run_critic",
            new_callable=AsyncMock,
            return_value=_passing_result(),
        ),
    ):
        await run_harness(
            prd=prd,
            output_dir=output_dir,
            resume=False,
            config=_make_config(max_round_retries=0),
        )

    assert round_count >= 1
```

### Success Criteria

#### Automated Verification
- [x] `mypy` reports no type errors (`uv run mypy deep_architect/`)
- [x] `ruff` passes (`uv run ruff check deep_architect/ tests/`)
- [x] All existing tests pass (`uv run python -m pytest tests/ -v`)
- [x] `grep -n "generator_session_id" deep_architect/harness.py` returns no results
- [x] `grep -n "session_id=" deep_architect/agents/generator.py` returns no results (in function signature/call)
- [x] New tests pass: `test_harness_generator_receives_no_session_id`, `test_harness_runs_multiple_rounds_stateless`

---

## Phase 2: History File Infrastructure

### Overview

Add `append_generator_history()` and `append_critic_history()` to `io/files.py`. These functions
format and append structured Markdown entries. Update `clean_run_artifacts()` to delete history
files on fresh start.

### Changes Required

#### 1. `deep_architect/io/files.py`

**Add `datetime` import** at the top of the file:
```python
from datetime import UTC, datetime
```

**Add `append_generator_history()` after `load_feedback()`**:

```python
def append_generator_history(
    output_dir: Path,
    sprint_num: int,
    round_num: int,
    previous_feedback: CriticResult | None,
    modified_files: list[Path],
    input_tokens: int,
) -> None:
    """Append a structured generator round entry to generator-history.md.

    Entries are grep-searchable by sprint number, round number, or filename.
    The file is never auto-loaded into context — agents search it via Read/Grep.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if previous_feedback is None:
        feedback_summary = "First round — no prior feedback"
    else:
        feedback_summary = (
            f"{len(previous_feedback.feedback)} concern(s) from prior critic round"
            f" (avg {previous_feedback.average_score:.1f}/10)"
        )
    files_str = ", ".join(f.name for f in modified_files) if modified_files else "None"
    entry = (
        f"\n## Sprint {sprint_num} · Round {round_num} — {timestamp}\n"
        f"**Feedback addressed**: {feedback_summary}\n"
        f"**Files modified**: {files_str}\n"
        f"**Token usage**: {input_tokens:,}\n"
        f"---\n"
    )
    with (output_dir / "generator-history.md").open("a") as fh:
        fh.write(entry)
```

**Add `append_critic_history()` after `append_generator_history()`**:

```python
def append_critic_history(
    output_dir: Path,
    sprint_num: int,
    round_num: int,
    result: CriticResult,
) -> None:
    """Append a structured critic round entry to critic-history.md.

    Entries are grep-searchable by sprint number, round number, severity, or criterion keyword.
    The file is never auto-loaded into context — agents search it via Read/Grep.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    passed_str = "Yes" if result.passed else "No"
    concerns = "\n".join(
        f"- [{f.severity}] {f.criterion} ({f.score:.1f}/10): {f.details[:200]}"
        for f in result.feedback
    )
    entry = (
        f"\n## Sprint {sprint_num} · Round {round_num} — {timestamp}\n"
        f"**Score**: {result.average_score:.1f}/10  **Passed**: {passed_str}\n"
        f"**Concerns**:\n{concerns}\n"
        f"**Summary**: {result.overall_summary[:300]}\n"
        f"---\n"
    )
    with (output_dir / "critic-history.md").open("a") as fh:
        fh.write(entry)
```

**Update `clean_run_artifacts()`** to delete history files alongside learnings:

```python
# BEFORE:
learnings = output_dir / "generator-learnings.md"
if learnings.exists():
    learnings.unlink()
    deleted.append(learnings)

# AFTER:
for filename in ("generator-learnings.md", "generator-history.md", "critic-history.md"):
    artifact = output_dir / filename
    if artifact.exists():
        artifact.unlink()
        deleted.append(artifact)
```

#### 2. `tests/test_files.py`

Add tests for the new functions and updated cleanup:

```python
def test_append_generator_history_creates_file(tmp_path: Path) -> None:
    """append_generator_history creates the file and writes a structured entry."""
    result = make_result()
    append_generator_history(tmp_path, sprint_num=1, round_num=1,
                             previous_feedback=result,
                             modified_files=[tmp_path / "c4-context.md"],
                             input_tokens=12345)
    history = (tmp_path / "generator-history.md").read_text()
    assert "## Sprint 1 · Round 1" in history
    assert "**Files modified**: c4-context.md" in history
    assert "**Token usage**: 12,345" in history
    assert "concern(s) from prior critic round" in history


def test_append_generator_history_first_round(tmp_path: Path) -> None:
    """First round with no prior feedback uses the correct label."""
    append_generator_history(tmp_path, sprint_num=2, round_num=1,
                             previous_feedback=None,
                             modified_files=[],
                             input_tokens=0)
    history = (tmp_path / "generator-history.md").read_text()
    assert "First round — no prior feedback" in history
    assert "**Files modified**: None" in history


def test_append_generator_history_accumulates(tmp_path: Path) -> None:
    """Multiple calls append; file grows with both entries searchable."""
    for rnd in (1, 2):
        append_generator_history(tmp_path, sprint_num=1, round_num=rnd,
                                 previous_feedback=None, modified_files=[], input_tokens=0)
    history = (tmp_path / "generator-history.md").read_text()
    assert "## Sprint 1 · Round 1" in history
    assert "## Sprint 1 · Round 2" in history


def test_append_critic_history_creates_file(tmp_path: Path) -> None:
    """append_critic_history creates the file and writes a structured entry."""
    result = make_result()
    append_critic_history(tmp_path, sprint_num=1, round_num=1, result=result)
    history = (tmp_path / "critic-history.md").read_text()
    assert "## Sprint 1 · Round 1" in history
    assert "**Score**:" in history
    assert "**Concerns**:" in history
    assert "**Summary**:" in history


def test_append_critic_history_severity_labels(tmp_path: Path) -> None:
    """Severity labels appear in entries for grep-ability."""
    result = make_result()  # make_result() must include at least one feedback item with severity
    append_critic_history(tmp_path, sprint_num=1, round_num=1, result=result)
    history = (tmp_path / "critic-history.md").read_text()
    # Severity labels are grep-searchable
    assert any(s in history for s in ("[Critical]", "[High]", "[Medium]", "[Low]"))


def test_clean_run_artifacts_removes_history_files(tmp_path: Path) -> None:
    """clean_run_artifacts deletes generator-history.md and critic-history.md."""
    init_workspace(tmp_path)
    checkpoint_dir = tmp_path / ".checkpoints"
    checkpoint_dir.mkdir()
    (tmp_path / "generator-history.md").write_text("## Sprint 1 · Round 1\n---\n")
    (tmp_path / "critic-history.md").write_text("## Sprint 1 · Round 1\n---\n")

    deleted = clean_run_artifacts(tmp_path, checkpoint_dir)

    assert not (tmp_path / "generator-history.md").exists()
    assert not (tmp_path / "critic-history.md").exists()
    deleted_names = [p.name for p in deleted]
    assert "generator-history.md" in deleted_names
    assert "critic-history.md" in deleted_names
```

Note: The existing `test_clean_run_artifacts_removes_checkpoint_contracts_feedback` test
asserts `len(deleted) == 5`. After this change, with history files present, the count will
differ. Either make history file creation conditional on file existence in the test setup,
or update the count assertion. The simplest fix: don't create history files in the existing
test (they aren't relevant to it), and write `test_clean_run_artifacts_removes_history_files`
as a separate focused test.

### Success Criteria

#### Automated Verification
- [x] `mypy` passes (new functions have correct type annotations)
- [x] All tests pass including new `test_files.py` tests
- [x] `grep -n "append_generator_history\|append_critic_history" deep_architect/io/files.py` returns both functions
- [x] `grep -n "generator-history\|critic-history" deep_architect/io/files.py` appears in `clean_run_artifacts`

---

## Phase 3: Wire History Into Harness Orchestration

### Overview

After each generator round (after `git_commit()`), append a generator history entry. After each
critic round (after `save_round_log()`), append a critic history entry. Both calls go inside the
`try` block of the inner retry loop — a failure to write history is an unexpected error and
should not be silently swallowed.

### Changes Required

#### 1. `deep_architect/harness.py`

**Add new functions to the import** (lines 15-24):

```python
from deep_architect.io.files import (
    append_critic_history,
    append_generator_history,
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

**Append generator history after `git_commit()`** (after line 396). Insert immediately after the
`git_commit(...)` call:

```python
append_generator_history(
    output_dir,
    sprint.number,
    round_num,
    previous_feedback=last_result,
    modified_files=written,
    input_tokens=gen_round.input_tokens,
)
```

The `last_result` variable holds the `CriticResult` from the prior round (or `None` for round 1).
`written` is already available from `get_modified_files(repo)` at line 382. `gen_round.input_tokens`
is available from the generator result.

**Append critic history after `save_round_log()`** (after line 421). Insert after the
`save_round_log(...)` call:

```python
append_critic_history(output_dir, sprint.number, round_num, result)
```

`result` is the `CriticResult` just returned from `run_critic()`.

The complete `try` block inner section (showing only the new lines in context):

```python
# ... existing git_commit() call ...
git_commit(
    repo,
    f"Generator pass {round_num} - sprint {sprint.number} ({sprint.name})",
    written,
)
append_generator_history(                    # NEW
    output_dir,
    sprint.number,
    round_num,
    previous_feedback=last_result,
    modified_files=written,
    input_tokens=gen_round.input_tokens,
)

# ... existing run_critic(), save_feedback(), save_round_log() calls ...
save_round_log(
    output_dir,
    sprint.number,
    round_num,
    {
        "sprint": sprint.number,
        "round": round_num,
        "average_score": result.average_score,
        "passed": result.passed,
        "feedback_count": len(result.feedback),
    },
)
append_critic_history(output_dir, sprint.number, round_num, result)  # NEW
round_ok = True
break
```

### Success Criteria

#### Automated Verification
- [x] All tests pass
- [x] `grep -n "append_generator_history\|append_critic_history" deep_architect/harness.py` returns two lines inside the sprint loop
- [ ] Manual inspection: run harness against a test PRD and confirm both `generator-history.md`
  and `critic-history.md` exist in the output directory after a round completes

#### Manual Verification
- [ ] After a full run, `generator-history.md` has one entry per generator round
- [ ] After a full run, `critic-history.md` has one entry per critic round
- [ ] `grep "Sprint 1" output_dir/generator-history.md` returns only Sprint 1 entries
- [ ] History files are absent after `--clean` flag invokes `clean_run_artifacts()`

---

## Phase 4: Inject History File Path Into Agent Prompts

### Overview

Both `run_generator()` and `run_critic()` inject the history file path (not content) into the
user prompt when the file exists. This gives agents orientation and lets them Read/Grep history
on demand. The learnings file is unchanged — it continues to be fully loaded into the generator
prompt.

### Changes Required

#### 1. `deep_architect/agents/generator.py`

Add a history section to the prompt, placed after the learnings section and before the final
instruction paragraph. The section is only injected when `generator-history.md` exists.

```python
# After the learnings_section block (after line 85):
history_section = ""
history_path = output_dir / "generator-history.md"
if history_path.exists():
    history_section = (
        f"\n## Round History\n"
        f"Your prior work is recorded at `{history_path}`. "
        f"Use Read or Grep to search it by sprint number, round number, "
        f"or filename. Do NOT write to this file.\n"
    )
```

Update the final prompt assembly (lines 91-102) to include `history_section`:

```python
prompt = (
    f"## PRD\n{prd_content}\n\n"
    f"{context_section}"
    f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
    f"## Working Directory\n{output_dir}\n\n"
    f"## Files to Produce\n"
    + "\n".join(f"- {f}" for f in contract.files_to_produce)
    + f"\n\n{feedback_section}{learnings_section}{history_section}\n"   # history_section added
    "Use the Write tool to create each file in the working directory using absolute paths. "
    "Use the Edit tool for targeted changes when addressing feedback on existing files. "
    "Each file must be a complete, standalone Markdown document."
)
```

#### 2. `deep_architect/agents/critic.py`

Add a history section to the critic prompt, injected when `critic-history.md` exists.

```python
# After the `files_list` line (line 49):
history_section = ""
history_path = output_dir / "critic-history.md"
if history_path.exists():
    history_section = (
        f"\n## Critic History\n"
        f"Prior evaluations are recorded at `{history_path}`. "
        f"Use Read or Grep to check for recurring concerns or score trends. "
        f"Do NOT write to this file.\n"
    )
```

Update the prompt:

```python
prompt = (
    f"Evaluate the architecture files in {output_dir} against the sprint contract.\n\n"
    f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
    f"## Files to Evaluate\n{files_list}\n\n"
    f"This is Round {round_num}. Use Read, Glob, and Grep to inspect each file. "
    "Score every criterion in the contract. "
    f"Return a CriticResult JSON object.{history_section}"   # history_section added
)
```

#### 3. `deep_architect/prompts/generator_system.md`

Extend the `## Learnings File` section to clarify the distinction between learnings (subjective,
agent-written) and history (objective, harness-written). Add a new subsection:

```markdown
## Learnings File

You maintain a persistent memory file at `generator-learnings.md` in the working directory.

**At the end of every round**, use Write or Edit to update it with:
- Architecture decisions made and their rationale
- Patterns and approaches that scored well with the Critic
- Issues the Critic raised, how you addressed them, and what worked
- Domain insights about the system gleaned from the PRD
- Mermaid/C4 syntax rules you confirmed work correctly

Keep entries concise and actionable. This is your subjective working memory — you write it, you own it.

## Round History File

When a `## Round History` section appears in your prompt, it points to `generator-history.md` —
a structured objective record maintained by the harness (not by you). Use Read or Grep to search
it for prior file changes, token counts, and critic score trends. **Do NOT write to this file.**

Your learnings file and the history file are complementary:
- `generator-learnings.md` — what you want to remember (write here)
- `generator-history.md` — what actually happened (read-only)
```

#### 4. `deep_architect/prompts/critic_system.md`

Add a note about the critic history file in the `## Inspection Method` section:

```markdown
## Inspection Method

- Use the **Read** tool to examine each architecture file before scoring it.
- Use **Glob** to discover all files in the working directory.
- Use **Grep** to search for specific patterns (e.g. missing relationships, diagram keywords).
- Include exact `file:line` references in your feedback details.
- When a `## Critic History` section appears in your prompt, use Read or Grep on that file to
  check for recurring concerns across rounds. **Do NOT write to this file.**
```

### Success Criteria

#### Automated Verification
- [x] All tests pass
- [x] `mypy` passes (no type errors from `Path` comparisons or string formatting)
- [x] `grep -n "history_section" deep_architect/agents/generator.py` returns the injection block
- [x] `grep -n "history_section" deep_architect/agents/critic.py` returns the injection block

#### Manual Verification
- [ ] On round 1 (history file does not yet exist), the `## Round History` section is absent
  from the generator prompt (no injection when file missing)
- [ ] On round 2+, the `## Round History` section appears with the correct file path
- [ ] The critic prompt shows `## Critic History` on round 2+

---

## Phase 5: ADR-021 and ADR-004 Update

### Overview

Document the architectural decision in a new ADR that supersedes ADR-004. Update ADR-004's status
field to "Superseded". No code changes.

### Changes Required

#### 1. Create `knowledge/adr/ADR-021-stateless-session-per-turn.md`

```markdown
# ADR-021: Stateless Session Per Turn — Generator Session Reset Every Round

**Status:** Accepted
**Date:** 2026-04-11
**Supersedes:** ADR-004 (Generator Session Persistence Within Sprint)
**Deciders:** Project design (PROJ-0003)

---

## Context

ADR-004 established that the generator reuses a `session_id` across all rounds within a sprint,
giving it implicit memory of prior tool calls. However:

1. On crash or `--resume`, the session was already lost and the system handled it gracefully.
2. All explicit context (PRD, contract, feedback, learnings) was re-injected every round regardless.
3. Accumulated session context from many rounds contributed to context-overflow failures on long runs.
4. The "generator session context lost on resume" warning in ADR-011 documented the existing
   graceful degradation path — PROJ-0003 normalizes this into the standard execution model.

## Decision

The generator starts a **fresh session for every round**. `session_id` is never reused across
rounds. The harness no longer tracks `generator_session_id`.

The implicit memory previously provided by session continuity is replaced by:
- `generator-history.md` — harness-written, structured per-round record of files changed and
  feedback addressed (objective, grep-searchable)
- `generator-learnings.md` — agent-written, free-form working memory (subjective, fully injected)

Both files persist across sprints and survive crashes. `--resume` loads them automatically from
disk — no special-case logic required.

## Rationale

- **Eliminates context accumulation.** A fresh session every round bounds context window usage
  to a single turn, removing a known source of instability on long runs.
- **Normalizes the existing crash/resume path.** The system already handled the no-session case
  correctly (confirmed by ADR-011 and crash recovery tests). PROJ-0003 makes this the only path.
- **File-mediated handoff is strictly more robust.** History files survive process restarts;
  session context does not.
- **Resume becomes the standard flow.** `--resume` and a fresh start use identical code paths.

## Consequences

- Each generator round pays the full context-window cost of re-reading history and making
  file-discovery tool calls. This is mitigated by history files being path-only (not injected)
  and learnings being concise.
- `generator_session_id` tracking removed from `harness.py`. `session_id` and
  `last_known_input_tokens` parameters removed from `run_generator()`.
- `GeneratorRoundResult.session_id` retained (SDK still returns it) but never fed back into
  subsequent rounds.
- `test_harness_resets_generator_session_on_retry` replaced — the behavior it tested no longer exists.

**Files:** `deep_architect/agents/generator.py`, `deep_architect/harness.py`,
`deep_architect/io/files.py`, `deep_architect/agents/critic.py`
```

#### 2. Update `knowledge/adr/ADR-004-generator-session-persistence.md`

Change the `Status` line:
```markdown
# BEFORE:
**Status:** Accepted

# AFTER:
**Status:** Superseded by ADR-021 (2026-04-11)
```

### Success Criteria

#### Automated Verification
- [x] Both ADR files exist and `grep "Superseded" knowledge/adr/ADR-004-generator-session-persistence.md` returns the status line

---

## Testing Strategy

### Unit Tests

**`tests/test_files.py`** — 5 new tests (listed in Phase 2):
- `test_append_generator_history_creates_file`
- `test_append_generator_history_first_round`
- `test_append_generator_history_accumulates`
- `test_append_critic_history_creates_file`
- `test_append_critic_history_severity_labels`
- `test_clean_run_artifacts_removes_history_files`

**`tests/test_harness_retry.py`** — replace 1 test, add 1 new test:
- Replace `test_harness_resets_generator_session_on_retry` with `test_harness_generator_receives_no_session_id`
- Add `test_harness_runs_multiple_rounds_stateless`

### Existing Tests

No changes required to:
- `test_client.py:308-329` — `test_run_agent_clears_resume_on_retry` still valid (SDK-level retry)
- `test_exit_criteria.py` — no session-related logic
- `test_models.py` — no session-related logic
- `test_files.py` — existing cleanup test: do not add history files to existing `test_clean_run_artifacts_removes_checkpoint_contracts_feedback`; write a separate focused test

### Manual Testing

- [ ] Run `adversarial-architect` against a real PRD for at least 2 rounds; confirm both history
  files created, entries are formatted correctly, run completes without error
- [ ] Kill mid-sprint; `--resume`; confirm run continues from the correct round using file state
  only; history files accumulate correctly across the resume boundary
- [ ] After run, `grep "Sprint 1" output_dir/generator-history.md` returns only Sprint 1 entries
- [ ] Confirm `generator-history.md` and `critic-history.md` are absent after fresh start with
  existing history (i.e., `clean_run_artifacts()` removed them)

## Implementation Order

Execute phases in sequence. Each phase leaves the codebase in a passing state:

1. **Phase 1** — Removes session threading. Tests updated. Run full test suite before proceeding.
2. **Phase 2** — Adds `io/files.py` functions. Unit-testable in isolation.
3. **Phase 3** — Wires history into harness. Builds on Phase 2.
4. **Phase 4** — Updates prompts and system prompt docs. No model changes.
5. **Phase 5** — ADR documentation only.

After each phase: `uv run ruff check deep_architect/ tests/ && uv run mypy deep_architect/ && uv run python -m pytest tests/ -v && uv run bandit -r deep_architect/ -ll`

## References

- Original ticket: `knowledge/tickets/PROJ-0003.md`
- Research: `knowledge/research/2026-04-11-PROJ-0003-session-context-per-round.md`
- Superseded ADR: `knowledge/adr/ADR-004-generator-session-persistence.md`
- Resume ADR: `knowledge/adr/ADR-011-resume-via-progress-json.md`
- Key source files:
  - `deep_architect/harness.py:303-431` — session tracking (to be removed)
  - `deep_architect/agents/generator.py:33-124` — full `run_generator()` implementation
  - `deep_architect/agents/critic.py:40-81` — full `run_critic()` implementation
  - `deep_architect/io/files.py` — save/load layer (history functions added here)
