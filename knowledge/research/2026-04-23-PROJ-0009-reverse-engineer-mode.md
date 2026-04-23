---
date: 2026-04-23T00:00:00Z
researcher: Gerald Hewes
git_commit: 052b2fe8a74a2a6667fa5144beb4293cf1dd0c3e
branch: main
repository: deep-architect
topic: "PROJ-0009 — Support Creating Architecture from Code Base (reverse-engineer mode)"
tags: [research, codebase, PROJ-0009, reverse-engineer, cli, prompts, harness, git-ops]
status: complete
last_updated: 2026-04-23
last_updated_by: Gerald Hewes
---

# Research: PROJ-0009 — Support Creating Architecture from Code Base

**Date**: 2026-04-23
**Researcher**: Gerald Hewes
**Git Commit**: 052b2fe8a74a2a6667fa5144beb4293cf1dd0c3e
**Branch**: main
**Repository**: deep-architect

## Research Question

How should we add a `--reverse-engineer --codebase <path>` mode to `adversarial-architect` that reverse-engineers C4 architecture docs from an existing codebase, while leaving the current greenfield/PRD flow untouched? The ticket (PROJ-0009) raises eight concrete research questions — this document answers all of them with code-level evidence.

## Summary

**Good news: the existing architecture is already mostly reverse-engineer-ready.** The path/CWD/git plumbing is all `output_dir`-relative with zero hidden CWD dependencies, and Claude Code's `Read/Glob/Grep` tools accept absolute paths outside the agent's `cwd`. The heavy lifting is therefore confined to three areas:

1. **CLI surface** (`cli.py`) — add a mode flag + `--codebase` path, make `--prd` optional, validate mutual exclusivity.
2. **Harness entry** (`harness.py::run_harness`) — accept either a PRD path or a codebase path, swap the prompt substitution variable, point the generator/critic at the codebase via absolute paths in the prompt (their `cwd` stays on `output_dir`).
3. **Prompts** (`deep_architect/prompts/`) — author 7 reverse-engineer sprint prompts plus a new `contract_proposal_re.md`. Eleven other prompts (system, C4 guide, Mermaid guide, critic_*, contract_system, contract_review, critic_rescue, ping_pong_check, final_agreement) are mode-agnostic and reused as-is.

Sprint structure, exit criteria, keep-best rollback, generator/critic tool sets, checkpoint scheme, and the two history-file mechanism all remain unchanged. No ADR needs superseding — ADR-003, ADR-007, ADR-008 all extend naturally to a second mode.

**No prior artifacts exist for PROJ-0009.** No research, plans, handoffs, or ADRs touch reverse-engineering, brownfield, or multi-repo output. Everything below is net-new.

## Detailed Findings

### 1. How `harness.py` currently wires PRD into generator prompts

The PRD is read once at the top of `run_harness` and threaded through every agent call:

- [`harness.py:321`](../deep_architect/harness.py) — `prd_content = prd.read_text()`
- [`harness.py:413-416`](../deep_architect/harness.py) — passed to `negotiate_contract(..., prd_content, ...)`
- [`harness.py:510-525`](../deep_architect/harness.py) — passed to `run_generator(..., prd_content, ...)`
- [`agents/generator.py:101`](../deep_architect/agents/generator.py) — embedded verbatim into the user prompt: `f"## PRD\n{prd_content}\n\n"`
- [`agents/generator.py:155-162`](../deep_architect/agents/generator.py) — `propose_contract` substitutes `{prd}` into `contract_proposal.md` via `load_prompt("contract_proposal", prd=prd_content, ...)`

The critic never sees the PRD directly (it evaluates files against the contract only — see `agents/critic.py:65-72`). That means reverse-engineer mode only needs to replace PRD injection in **two places**: generator round prompts and contract proposal.

**Cleanest swap-in for a codebase path**: keep the `prd_content` variable slot but pass a different string. The generator prompt's `## PRD` section becomes `## Target Codebase` with an absolute path + "Read the code via Read/Glob/Grep to design the architecture." This keeps the harness loop untouched; the mode switch lives in `cli.py` and in prompt selection.

### 2. CLI flag design: mutually-exclusive flags vs. positional mode

Typer doesn't have built-in mutually-exclusive option groups, but the existing code (`cli.py:32-67`) uses a simple pattern of post-parse validation that fits well. Two viable designs:

**Option A — mutually-exclusive flags (recommended)**:
```python
prd: Path | None = typer.Option(None, "--prd", ...)
codebase: Path | None = typer.Option(None, "--codebase", ...)
reverse_engineer: bool = typer.Option(False, "--reverse-engineer", ...)
greenfield: bool = typer.Option(False, "--greenfield", ...)
```
Validate at startup: exactly one of `--greenfield`/`--reverse-engineer` set, and the matching path argument present. Default to greenfield if neither flag is set (ticket note: "existing default should be made explicit"). This keeps invocation self-documenting.

**Option B — positional mode argument**: `adversarial-architect greenfield --prd ...` or `adversarial-architect reverse-engineer --codebase ...` using Typer sub-commands. Cleaner but a larger refactor of the existing `@app.command()` entry point.

**Recommendation: Option A.** It's a one-file change to `cli.py` with backwards compatibility (old `--prd /path` invocations can keep working if we default missing flags to greenfield, or be deprecated cleanly).

### 3. `cli.py` changes

Current signature (`cli.py:32-67`):
- `prd: Path = typer.Option(..., ...)` — **required**, must become optional
- `output: Path = ...` — unchanged
- All other flags (`--resume`, `--config`, `--model-*`, `--context`, `--reset-sprint`, `--strict`) — unchanged

New changes:
- Add `--reverse-engineer` bool and `--codebase` path flags.
- Loosen `prd` to `Path | None`.
- Pre-validation block: enforce exactly-one-mode and matching path, similar to the existing `prd.exists()` / `context_file.exists()` checks (`cli.py:69-76`).
- Pass the mode + resolved "source" path into `run_harness`.
- `_find_checkpoint` and the resume-prompt logic (`cli.py:20-29`, `cli.py:103-131`) work on `output` alone — no change needed, already mode-agnostic.

### 4. SprintDefinition: separate lists or mode-parameterized?

The current `SPRINTS` list (`sprints.py:15-84`) contains seven entries whose `name`, `number`, `primary_files`, and `allow_extra_files` are **mode-invariant** — a C1 context sprint produces `c1-context.md` whether the source is a PRD or a codebase. Only the `description` field leaks PRD language ("Generate the C4 Level 1 System Context diagram…") but it's generic enough to stand.

**Recommendation: keep ONE `SPRINTS` list** and do mode switching at the **prompt-loading layer**. `SprintDefinition` stays unchanged. The harness loads `sprint_N_name.md` for greenfield and `sprint_N_name_re.md` for reverse-engineer, or better: passes a `mode` argument to `load_prompt` and the prompt path convention encodes it.

This avoids doubling metadata and keeps the "exactly 7 sprints" invariant from ADR-007 clean.

### 5. Prompt files needed

From the exhaustive scan of `deep_architect/prompts/` (17 files total):

**New prompts needed (8 files)**:
- `sprint_1_c1_context_re.md` — extract actors/externals from code rather than PRD
- `sprint_2_c2_container_re.md` — infer containers from imports/deployment manifests
- `sprint_3_frontend_re.md` — analyze actual component tree, routing, state
- `sprint_4_backend_re.md` — inspect API routes, workers, orchestration code
- `sprint_5_database_re.md` — schema introspection, query analysis
- `sprint_6_edge_re.md` — inspect IaC (terraform/helm/compose), observability stack
- `contract_proposal_re.md` — variant that takes `{codebase_path}` instead of `{prd}`
- (Optionally) `generator_system_re.md` — only if code-reading guidance differs substantially from generic generator system prompt; **recommended to reuse** `generator_system.md` unchanged and put code-reading directives in each sprint prompt.

**Reused unchanged (9 files)**:
- `sprint_7_adrs.md` — feeds off the in-repo decisions manifest, not the PRD directly
- `generator_system.md`, `critic_system.md` — mode-agnostic tool/behavior rules
- `contract_system.md`, `contract_review.md` — format-generic
- `mermaid_c4_guide.md`, `c4_skill.md` — pure C4/Mermaid reference
- `ping_pong_check.md`, `final_agreement.md` — algorithm-level, mode-agnostic
- `critic_rescue.md` — reads files via Python I/O, mode-agnostic

Variable expectations (Python `str.format`): `{prd}`, `{sprint_number}`, `{sprint_name}`, `{sprint_description}`, `{primary_files}`, `{contract_json}`, `{output_dir}`, `{previous_summary}`, `{current_summary}`, `{files_text}`, `{round_num}`. The new `contract_proposal_re.md` introduces `{codebase_path}` in place of `{prd}`.

### 6. Scoping `generator-history.md` / `generator-learnings.md`

Today both files live at `output_dir / "*.md"` — written by the harness (`io/files.py:57-87`) and/or the agent (`agents/generator.py:75-83`), loaded into prompts on subsequent rounds, and deleted on clean-restart (`io/files.py:342-346`).

When `output_dir = <target-repo>/knowledge/architecture/`:
- Both files will naturally land in the target codebase repo alongside the generated architecture docs. ✓
- They'll be committed to the target repo by the existing `get_modified_files` detection path. ✓
- `--resume` loads them with no changes needed.

**No code changes required for this.** The one thing to decide is a project policy: do we want these history files committed into the target repo permanently (noisy but reproducible) or gitignored? I recommend committing — they're small, they document how the architecture was derived, and the existing greenfield behavior already commits them.

### 7. `init_workspace()` and non-CWD output

[`io/files.py:24-27`](../deep_architect/io/files.py):
```python
def init_workspace(output_dir: Path) -> None:
    for subdir in ["contracts", "feedback", "decisions", "logs"]:
        (output_dir / subdir).mkdir(parents=True, exist_ok=True)
```
Pure `output_dir`-relative. Works identically whether `output_dir` is `./knowledge/architecture/` or `/path/to/other/repo/knowledge/architecture/`. **No changes needed.**

### 8. `test_prompts.py` EXPECTED_PROMPTS

Currently (`tests/test_prompts.py:5-23`) a flat list of 17 names. For the new 7 (or 8) prompt files, two options:

- **Flat addition** — append the new names to the same `EXPECTED_PROMPTS` list. Simplest.
- **Split lists** — `GREENFIELD_PROMPTS` + `REVERSE_ENGINEER_PROMPTS` + `SHARED_PROMPTS`, combined via parametrize. More legible but more machinery.

**Recommendation: flat addition.** The test's only job is "all advertised prompts exist and load." Organizing by mode adds no safety.

## Code References

- `deep_architect/cli.py:32-67` — current Typer entry point (single mode today)
- `deep_architect/cli.py:20-29` — `_find_checkpoint` uses `validate_git_repo(output_dir)` — already mode-agnostic
- `deep_architect/harness.py:267-275` — `run_harness` signature (takes `prd: Path`)
- `deep_architect/harness.py:321` — `prd_content = prd.read_text()` (single injection point)
- `deep_architect/harness.py:413-416`, `harness.py:510-525` — two places PRD content flows downstream
- `deep_architect/agents/generator.py:101` — PRD literal embedded in generator round prompt (`## PRD\n{prd_content}\n\n`)
- `deep_architect/agents/generator.py:108-111` — prompt instructs Write-via-absolute-paths into `output_dir`
- `deep_architect/agents/generator.py:124` — generator `cwd=str(output_dir)`
- `deep_architect/agents/generator.py:155-162` — `contract_proposal.md` substitution (needs `_re` variant)
- `deep_architect/agents/critic.py:79` — critic `cwd=str(output_dir)` (read-only tools, can still Read absolute paths into target codebase)
- `deep_architect/agents/client.py:376-418` — `make_agent_options(..., cwd=None)` passes `cwd` through to `ClaudeAgentOptions`; Claude Code CLI Read/Glob/Grep accept absolute paths beyond `cwd`
- `deep_architect/git_ops.py:18-27` — `validate_git_repo(path)` uses `git.Repo(path, search_parent_directories=True)` — finds repo containing `output_dir`, fully CWD-agnostic
- `deep_architect/git_ops.py:30-129` — `git_commit`, `git_commit_staged`, `get_modified_files`, `restore_arch_files_from_commit` all operate on the passed `repo` object; no hidden CWD calls
- `deep_architect/io/files.py:24-27` — `init_workspace(output_dir)` pure output_dir-relative
- `deep_architect/io/files.py:326-347` — `clean_run_artifacts` operates on `output_dir`+`checkpoint_dir`
- `deep_architect/sprints.py:15-84` — `SPRINTS` list (mode-invariant metadata)
- `deep_architect/prompts/` — 17 `.md` prompt files loaded via `load_prompt(name, **kwargs)`
- `tests/test_prompts.py:5-23` — `EXPECTED_PROMPTS` flat list

## Architecture Insights

1. **The existing architecture is CWD-agnostic by design.** Every write, every commit, every artifact, every history file is reached from `output_dir`. `validate_git_repo(output_dir)` finds the repo *containing* output_dir via `search_parent_directories=True`, so the `.checkpoints/` dir lands in the target codebase's repo, not the caller's CWD. This is exactly the property we need for reverse-engineer mode and it required no forethought — it's a natural consequence of not taking shortcuts with `os.getcwd()` / `chdir` anywhere in the codebase.

2. **The `cwd` passed to the agent is a default-path convenience, not a sandbox.** Claude Code's Read/Glob/Grep accept absolute paths outside `cwd`. So the generator (currently `cwd=output_dir`) can already Read files from the target codebase if the prompt tells it the absolute path. We don't need to restructure agent options — we just need to include the codebase path in the new sprint prompts.

3. **The prompt-as-markdown design (ADR-008) pays off.** Adding a reverse-engineer mode is authoring eight `.md` files plus ~20 lines of Python (CLI flag parsing + conditional prompt name). If prompts had been hard-coded Python strings this would be a substantial refactor; as it is, it's close to a pure-content change.

4. **Sprint 7 (ADRs) is the convergence point.** Both modes produce ADRs from a decisions manifest written by earlier sprints. Sprint 7's prompt doesn't reference the PRD directly, so it slots unchanged into either pipeline — the modes converge at the ADR output stage.

5. **The critic is entirely mode-agnostic.** Its job is "does the architecture file satisfy the contract criteria?" which has no dependence on whether the design came from a PRD or a codebase. The only slight nuance: the critic's acceptance criteria in reverse-engineer mode should emphasize accuracy-vs-code (the ticket notes "Critic confirms the generated docs accurately reflect the actual code"), which is a prompt-level concern within each sprint contract, not a code change.

## Historical Context (from knowledge/)

- `knowledge/tickets/PROJ-0009.md` — the ticket itself; specifies requirements, out-of-scope items, and the eight research questions answered above
- `knowledge/adr/README.md` — ADR index, 25 accepted ADRs (ADR-004 superseded)
- `knowledge/adr/ADR-003-asymmetric-tool-access.md` — locks Generator=write-capable, Critic=read-only. Reverse-engineer mode preserves this invariant; both modes share the tool sets in `generator.py:31` and `critic.py:22`.
- `knowledge/adr/ADR-007-seven-fixed-sprints.md` — seven sprints are fixed by design. Ticket's "no changes to sprint count" constraint matches.
- `knowledge/adr/ADR-008-prompts-as-markdown-files.md` — prompts are `.md` files loaded at runtime. New `_re` variants should follow the same convention.
- No prior research, plans, handoffs, or other artifacts exist for this ticket or topic.
- Only other untracked architecture-related artifact: `architecture_critique_report.md` at repo root — unrelated (critiques current implementation, no reverse-engineer content).

## Related Research

None — this is the first research doc for PROJ-0009.

## Open Questions

1. **Should `generator-learnings.md` and `generator-history.md` land in the target repo or in a separate staging area?** Current behavior would commit them in the target repo alongside the architecture files. Acceptable default, but worth confirming with the user before writing the plan.

2. **Does reverse-engineer mode need its own `final_agreement.md`?** The current one checks that 7 sprints ran + ADRs exist + C1/C2 diagrams are present. Those checks apply equally. But the *criterion* for "ready to ship" in reverse-engineer mode is accuracy-to-code, not design completeness — the critic's per-sprint evaluation already catches this, so probably no change needed. Worth re-reading `final_agreement.md` when writing the plan.

3. **Context-window risk for large codebases.** Greenfield mode embeds the entire PRD in every generator-round prompt. If reverse-engineer mode does the symmetric thing (embed "Target Codebase: /abs/path" + a pre-computed file tree), prompt size stays bounded. But if we ever pre-compute a codebase synopsis and embed it, we need to cap its size. Worth a small threshold (e.g., top-N files by size) or a separate "exploration sprint 0" if the architecture is very large. Not blocking for v1; flag for the planning phase.

4. **`--prd` vs `--codebase` naming when `--reverse-engineer` is implied.** If we go with "mode flag + two possible source-path flags," we should decide whether `--prd` and `--codebase` can both appear together as an error, or whether `--codebase` implies reverse-engineer mode (making `--reverse-engineer` redundant). Simpler UX: require both the mode flag *and* the path flag, error on mismatch. Ticket's wording ("New `--reverse-engineer` CLI flag *alongside* existing `--greenfield`") reads as two explicit modes. Confirm during `/create_plan`.
