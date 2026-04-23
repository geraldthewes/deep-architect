---
id: PROJ-0009
title: Support Creating Architecture from Code Base (reverse-engineer mode)
status: ready
created_at: 2026-04-23T00:00:00Z
research: knowledge/research/2026-04-23-PROJ-0009-reverse-engineer-mode.md
ticket: knowledge/tickets/PROJ-0009.md
---

# PROJ-0009 — Reverse-Engineer Mode Implementation Plan

## Overview

Add `--codebase <path>` CLI support so `adversarial-architect` can generate C4 architecture docs from an *existing* git repository instead of a PRD. The mode is implicit: `--prd` → greenfield, `--codebase` → reverse-engineer. The existing 7-sprint harness loop, exit criteria, git-ops, checkpoint, and rollback machinery are all reused unchanged.

## Current State Analysis

- `cli.py`: `prd` and `output` are both required (`...`) Typer options; single-mode entry point.
- `harness.py:267`: `run_harness(prd: Path, ...)` — PRD read at line 321 and threaded as `prd_content: str` through `negotiate_contract` (lines 404, 413) and `run_generator` (lines 510–525).
- `agents/generator.py:101`: PRD embedded as `## PRD\n{prd_content}\n\n` in the generator round prompt.
- `agents/generator.py:155–162`: `propose_contract` loads `contract_proposal.md` with `{prd}` variable.
- `harness.py:170`: `run_final_agreement` loads `final_agreement.md` which checks for "no critical gaps between PRD requirements and architecture."
- `prompts/sprint_N_*.md`: These files exist in `EXPECTED_PROMPTS` but are **never loaded at runtime** — they are reference documentation only. Sprint context flows through `SprintDefinition.description` → contract JSON → generator prompt.
- `agents/critic.py`, `io/files.py`, `git_ops.py`, `sprints.py`, `exit_criteria.py`: All output-dir-relative and fully mode-agnostic — zero changes needed.

## Desired End State

`adversarial-architect --codebase /path/to/repo` runs the full 7-sprint adversarial loop. The generator reads the target repo's code via its tools (Read, Glob, Grep, Bash). Output and git commits land in `<repo>/knowledge/architecture/`. Greenfield mode (`--prd`) is identical to today. All existing tests pass; new prompt files covered by `test_prompts.py`.

Verified by:
```bash
adversarial-architect --codebase /path/to/repo   # output defaults to <repo>/knowledge/architecture/
adversarial-architect --prd my.prd.md --output ./arch   # unchanged greenfield
uv run ruff check deep_architect/ tests/ && uv run mypy deep_architect/ && uv run python -m pytest tests/ -v && uv run bandit -r deep_architect/ -ll
```

### Key Discoveries

- Sprint `*.md` prompt files are reference docs only — no sprint-specific RE prompts needed.
- `propose_contract` uses `run_simple_structured` (no tool use) — `contract_proposal_re.md` cannot instruct codebase exploration; it provides path-as-metadata only. Actual exploration happens in `run_generator` (agentic, has tools).
- `validate_git_repo(output_dir)` uses `search_parent_directories=True` — commits automatically target the repo *containing* `output_dir`. If `--output <codebase>/knowledge/architecture/`, commits land in the target repo automatically.
- `cwd=str(output_dir)` in agent options is a default-path convenience only; Read/Glob/Grep accept absolute paths outside `cwd`. Generator can already read any absolute path.

## What We're NOT Doing

- No `sprint_N_xxx_re.md` files — they would never be loaded.
- No changes to `sprints.py` — single `SPRINTS` list, mode-invariant sprint metadata.
- No changes to `critic.py` — critic is already mode-agnostic.
- No changes to `io/files.py`, `git_ops.py`, `exit_criteria.py`, `config.py`, `logger.py`.
- No pre-computed file-tree injection in prompts (V1: agent discovers codebase via tools each round).
- No support for non-git directories.
- No changes to sprint count (7 sprints fixed).

## Implementation Approach

Mode is inferred from which path flag is set (`--prd` vs `--codebase`). The mode flows as `prd_content: str | None` and `codebase_path: str | None` through the call stack — exactly one is non-None. Prompt selection is conditional on which is set.

---

## Phase 1 — Python wiring

### Overview

Update `cli.py`, `harness.py`, and `agents/generator.py` to accept and route both modes. New prompt files are not created yet; this phase is verifiable with existing tests (all pass since greenfield path is unchanged).

### Changes Required

#### 1. `deep_architect/cli.py`

**File**: `deep_architect/cli.py`

Make `prd` and `output` optional; add `codebase`; add mutual-exclusion validation; default `output` to `<codebase>/knowledge/architecture/` when unset in RE mode.

Replace the `main` function signature and the leading validation block:

```python
@app.command()
def main(
    prd: Path | None = typer.Option(None, "--prd", help="Path to the PRD Markdown file (greenfield mode)"),
    codebase: Path | None = typer.Option(None, "--codebase", help="Path to the git repository to analyze (reverse-engineer mode)"),
    output: Path | None = typer.Option(None, "--output", help="Output directory (default: <codebase>/knowledge/architecture/ in reverse-engineer mode)"),
    resume: bool = typer.Option(False, "--resume", help="Resume an interrupted run"),
    config_file: Path | None = typer.Option(None, "--config", help="Config file path (default: ~/.deep-architect.toml)"),
    model_generator: str | None = typer.Option(None, "--model-generator", help="Override generator model name"),
    model_critic: str | None = typer.Option(None, "--model-critic", help="Override critic model name"),
    context: list[Path] = typer.Option([], "--context", help="Supplementary context files (repeatable)"),
    reset_sprint: int | None = typer.Option(None, "--reset-sprint", help=(...), min=1),
    strict: bool = typer.Option(False, "--strict", help=(...)),
) -> None:
    """Run the adversarial C4 architecture harness."""
    # Mode validation
    if prd is not None and codebase is not None:
        console.print("[red]Error:[/red] --prd and --codebase are mutually exclusive")
        raise typer.Exit(1)
    if prd is None and codebase is None:
        console.print("[red]Error:[/red] Either --prd (greenfield) or --codebase (reverse-engineer) is required")
        raise typer.Exit(1)
    if prd is not None and not prd.exists():
        console.print(f"[red]Error:[/red] PRD file not found: {prd}")
        raise typer.Exit(1)
    if codebase is not None and not codebase.is_dir():
        console.print(f"[red]Error:[/red] Codebase directory not found: {codebase}")
        raise typer.Exit(1)

    # Resolve output directory
    resolved_output: Path
    if output is not None:
        resolved_output = output
    elif codebase is not None:
        resolved_output = codebase.resolve() / "knowledge" / "architecture"
    else:
        console.print("[red]Error:[/red] --output is required in greenfield mode")
        raise typer.Exit(1)

    for ctx_path in context:
        if not ctx_path.exists():
            console.print(f"[red]Error:[/red] Context file not found: {ctx_path}")
            raise typer.Exit(1)
    ...
```

Update the `asyncio.run(...)` call (pass both `prd` and `codebase`):

```python
asyncio.run(
    run_harness(
        prd=prd,
        codebase=codebase,
        output_dir=resolved_output,
        resume=resume,
        config=cfg,
        context_files=context,
        strict=strict,
    )
)
```

All `reset_sprint` and checkpoint logic uses `resolved_output` instead of `output`.

#### 2. `deep_architect/harness.py`

**File**: `deep_architect/harness.py`

Update `run_harness` signature and the PRD-reading block:

```python
async def run_harness(
    prd: Path | None,
    output_dir: Path,
    resume: bool,
    config: HarnessConfig,
    context_files: list[Path] | None = None,
    codebase: Path | None = None,
    *,
    strict: bool = False,
) -> None:
```

Replace `prd_content = prd.read_text()` (line 321) with:

```python
prd_content: str | None = prd.read_text() if prd is not None else None
codebase_path: str | None = str(codebase.resolve()) if codebase is not None else None
```

Update `negotiate_contract` calls (lines 404–416) — add `codebase_path` keyword:

```python
contract = await negotiate_contract(
    config.generator, config.critic, sprint, prd_content, output_dir,
    codebase_path=codebase_path,
    cli_path=cli_path, supplementary_context=supplementary_context,
)
```

Update `run_generator` calls (lines 510–525) — add `codebase_path` keyword:

```python
gen_round: GeneratorRoundResult = await run_generator(
    config.generator,
    sprint,
    contract,
    prd_content,
    last_result,
    output_dir,
    round_num,
    codebase_path=codebase_path,
    cli_path=cli_path,
    ...
)
```

Update `run_final_agreement` call — add `codebase_path`:

```python
gen_ready, critic_ready = await run_final_agreement(
    config.generator, config.critic, output_dir,
    codebase_path=codebase_path,
    cli_path=cli_path,
)
```

Update `negotiate_contract` function signature:

```python
async def negotiate_contract(
    generator_config: AgentConfig,
    critic_config: AgentConfig,
    sprint: SprintDefinition,
    prd_content: str | None,
    output_dir: Path,
    *,
    codebase_path: str | None = None,
    cli_path: str | None = None,
    supplementary_context: str = "",
) -> SprintContract:
```

Inside `negotiate_contract`, update the `propose_contract` call:

```python
contract = await propose_contract(
    generator_config, sprint, prd_content,
    codebase_path=codebase_path,
    cli_path=cli_path, supplementary_context=supplementary_context,
)
```

Update `run_final_agreement` signature and prompt selection:

```python
async def run_final_agreement(
    generator_config: AgentConfig,
    critic_config: AgentConfig,
    output_dir: Path,
    *,
    codebase_path: str | None = None,
    cli_path: str | None = None,
) -> tuple[bool, bool]:
    ...
    prompt_name = "final_agreement_re" if codebase_path is not None else "final_agreement"
    final_prompt = load_prompt(prompt_name, output_dir=str(output_dir))
```

#### 3. `deep_architect/agents/generator.py`

**File**: `deep_architect/agents/generator.py`

Add `codebase_path: str | None = None` to `run_generator` signature and replace the hardcoded source section:

```python
async def run_generator(
    config: AgentConfig,
    sprint: SprintDefinition,
    contract: SprintContract,
    prd_content: str | None,
    previous_feedback: CriticResult | None,
    output_dir: Path,
    round_num: int,
    *,
    codebase_path: str | None = None,
    cli_path: str | None = None,
    ...
) -> GeneratorRoundResult:
```

Replace the hardcoded `## PRD` header (line 101):

```python
if prd_content is not None:
    source_section = f"## PRD\n{prd_content}\n\n"
else:
    source_section = (
        f"## Target Codebase\n{codebase_path}\n\n"
        "Use Read, Glob, Grep, and Bash to survey the repository. "
        "Read key files (e.g. README, package manifests, entry points, IaC configs) "
        "to understand the system before producing architecture docs.\n\n"
    )

prompt = (
    source_section
    + context_section
    + f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
    ...
)
```

Update `propose_contract` signature and prompt selection:

```python
async def propose_contract(
    config: AgentConfig,
    sprint: SprintDefinition,
    prd_content: str | None,
    *,
    codebase_path: str | None = None,
    cli_path: str | None = None,
    supplementary_context: str = "",
) -> SprintContract:
    if prd_content is not None:
        prompt = load_prompt(
            "contract_proposal",
            prd=prd_content,
            sprint_number=str(sprint.number),
            sprint_name=sprint.name,
            sprint_description=sprint.description,
            primary_files=str(sprint.primary_files),
        )
    else:
        prompt = load_prompt(
            "contract_proposal_re",
            codebase_path=codebase_path or "",
            sprint_number=str(sprint.number),
            sprint_name=sprint.name,
            sprint_description=sprint.description,
            primary_files=str(sprint.primary_files),
        )
```

### Success Criteria

#### Automated Verification
- [ ] `uv run ruff check deep_architect/ tests/` — no errors
- [ ] `uv run mypy deep_architect/` — no errors (update type annotations as needed)
- [ ] `uv run python -m pytest tests/ -v` — all existing tests pass (greenfield path unchanged)
- [ ] `uv run bandit -r deep_architect/ -ll` — no issues

#### Manual Verification
- [ ] `adversarial-architect --prd /nonexistent.md --output /tmp/out` still prints "PRD file not found"
- [ ] `adversarial-architect --prd foo.md --codebase /path` prints mutual-exclusion error
- [ ] `adversarial-architect --codebase /nonexistent` prints "Codebase directory not found"
- [ ] `adversarial-architect --codebase /tmp` (no --output) resolves output to `/tmp/knowledge/architecture/`

**Pause here before Phase 2 — confirm all Phase 1 checks pass.**

---

## Phase 2 — New prompt files + test coverage

### Overview

Author `contract_proposal_re.md` and `final_agreement_re.md`, wire them into `test_prompts.py`.

### Changes Required

#### 4. `deep_architect/prompts/contract_proposal_re.md` (new)

Template variables: `{codebase_path}`, `{sprint_number}`, `{sprint_name}`, `{sprint_description}`, `{primary_files}`

```markdown
# Contract Proposal Prompt — Reverse-Engineer Mode

You are proposing a sprint contract for the following sprint. The source is an **existing codebase** at the path below. The generator will survey the codebase using its tools (Read, Glob, Grep, Bash) during sprint rounds.

## Target Codebase
{codebase_path}

## Sprint Definition
Sprint {sprint_number}: {sprint_name}
{sprint_description}

Note: Sprint descriptions may reference "PRD" — treat that as "the existing codebase."

Primary files to produce: {primary_files}

Propose a sprint contract as a JSON object with this exact structure:
```json
{{
  "sprint_number": {sprint_number},
  "sprint_name": "{sprint_name}",
  "files_to_produce": ["<file1>", "..."],
  "criteria": [
    {{"name": "criterion_name", "description": "Specific, testable criterion", "threshold": 9.0}},
    ...
  ]
}}
```

Requirements for criteria:
- Include 5–10 criteria total
- Each criterion must be SPECIFIC and TESTABLE — not vague
- Cover: Mermaid diagram validity, C4 completeness, narrative quality, **accuracy to the actual codebase** (does the diagram reflect what is really there?), relationship documentation, Markdown readability (heading hierarchy, whitespace, list formatting, code block labels)
- Add criteria for edge cases relevant to this sprint (e.g. "if no frontend exists, the sprint file documents this explicitly")
- Threshold must be ≥ 9.0 for critical quality requirements

Output ONLY the JSON — no explanation, no markdown fencing.
```

#### 5. `deep_architect/prompts/final_agreement_re.md` (new)

Template variables: `{output_dir}`

```markdown
# Final Agreement Prompt — Reverse-Engineer Mode

Review the complete architecture in {output_dir}.

Check:
1. All 7 sprints have produced their required files
2. C1 and C2 diagrams are present and syntactically correct Mermaid
3. All containers from C2 have detailed breakdowns
4. ADRs cover the major architectural decisions
5. The architecture accurately reflects the **actual codebase** — no components invented, no real components omitted

If the architecture is production-ready and all C4 levels are complete and accurate to the code, output exactly:

    READY_TO_SHIP

Otherwise describe specifically what is missing, inaccurate, or needs correction.
Do not output READY_TO_SHIP unless the architecture is genuinely complete and accurate.
```

#### 6. `tests/test_prompts.py`

Add both prompt names to `EXPECTED_PROMPTS` and two variable-substitution tests.

Add to the list:
```python
"contract_proposal_re",
"final_agreement_re",
```

Add tests:
```python
def test_contract_proposal_re_variable_substitution() -> None:
    content = load_prompt(
        "contract_proposal_re",
        codebase_path="/path/to/repo",
        sprint_number="1",
        sprint_name="C1 Context",
        sprint_description="Generate C1 diagram",
        primary_files="['c1-context.md']",
    )
    assert "/path/to/repo" in content
    assert "C1 Context" in content


def test_final_agreement_re_variable_substitution() -> None:
    content = load_prompt("final_agreement_re", output_dir="/tmp/output")
    assert "/tmp/output" in content
    assert "accurate" in content
```

### Success Criteria

#### Automated Verification
- [ ] `uv run python -m pytest tests/ -v` — `test_prompt_loads[contract_proposal_re]` and `test_prompt_loads[final_agreement_re]` pass
- [ ] `test_contract_proposal_re_variable_substitution` and `test_final_agreement_re_variable_substitution` pass
- [ ] Full suite still clean: `ruff check`, `mypy`, `pytest`, `bandit`

**Pause here before Phase 3 — confirm all tests pass.**

---

## Phase 3 — README + final CI pass

### Overview

Update `README.md` with `--codebase` usage examples and run the full CI suite.

### Changes Required

#### 7. `README.md`

Add a **Reverse-Engineer Mode** section alongside the existing invocation examples. Show:

```bash
# Reverse-engineer an existing codebase (output defaults to <repo>/knowledge/architecture/)
adversarial-architect --codebase /path/to/existing-repo

# Explicit output override
adversarial-architect --codebase /path/to/existing-repo --output /custom/output/dir

# Greenfield mode (unchanged)
adversarial-architect --prd my-project.prd.md --output ./knowledge/architecture
```

Note that `--prd` and `--codebase` are mutually exclusive and that both require the target to be a git repository (needed for commit tracking).

### Success Criteria

#### Automated Verification
- [ ] `uv run ruff check deep_architect/ tests/` — clean
- [ ] `uv run mypy deep_architect/` — clean
- [ ] `uv run python -m pytest tests/ -v` — all pass
- [ ] `uv run bandit -r deep_architect/ -ll` — clean

#### Manual Verification
- [ ] Run `adversarial-architect --codebase /home/gerald/repos/deep-architect` against this repo and confirm run starts, generator reads codebase files, output lands in `knowledge/architecture/`, commits appear in git log.

---

## Testing Strategy

### Unit Tests
- All existing greenfield tests cover the unchanged path — no new unit tests for logic.
- `test_prompts.py` additions cover existence and variable substitution of both new prompt files.
- No LLM calls mocked or tested end-to-end.

### Manual Testing Steps
1. `adversarial-architect --codebase /home/gerald/repos/deep-architect` — full run against this repo
2. Inspect `knowledge/architecture/` for C4 artifacts
3. `git log` confirms commits in this repo
4. Run old greenfield command to confirm no regression

## References

- Ticket: `knowledge/tickets/PROJ-0009.md`
- Research: `knowledge/research/2026-04-23-PROJ-0009-reverse-engineer-mode.md`
- ADR-003 (asymmetric tool access): `knowledge/adr/ADR-003-asymmetric-tool-access.md`
- ADR-007 (seven fixed sprints): `knowledge/adr/ADR-007-seven-fixed-sprints.md`
- ADR-008 (prompts as markdown): `knowledge/adr/ADR-008-prompts-as-markdown-files.md`
