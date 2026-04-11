---
date: 2026-04-11T12:45:00-04:00
researcher: Claude (automated)
git_commit: b4fae512566e0ce2ac5d3a278a611fb61f2f6629
branch: master
repository: deep-researcher
topic: "PROJ-0003: Save context at each round for generator and critic and start new session"
tags: [research, codebase, session-management, generator, critic, context-accumulation, history-files]
status: complete
last_updated: 2026-04-11
last_updated_by: Claude (automated)
---

# Research: PROJ-0003 — Session Context Per Round

**Date**: 2026-04-11T12:45:00-04:00
**Researcher**: Claude (automated)
**Git Commit**: b4fae512566e0ce2ac5d3a278a611fb61f2f6629
**Branch**: master
**Repository**: deep-researcher

## Research Question

Answer the five research questions from PROJ-0003: How is `session_id` threaded? What does `generator_learnings.md` store? What context is injected vs. tool-discovered? What file naming conventions apply? What SDK session guarantees would break?

## Summary

The generator reuses a single `session_id` across rounds within a sprint (critic already starts fresh every round). The session provides implicit memory of prior tool calls and decisions but all explicit context (PRD, contract, feedback) is re-injected into the prompt every round anyway. `generator_learnings.md` is a free-form Markdown file written by the LLM itself, global across all sprints — it is distinct from (but could be reconciled with) the proposed structured per-round history. The workspace uses a flat-within-typed-subdirectories convention with hyphenated filenames. Dropping session reuse would lose the generator's implicit memory of prior round outputs, which must be replaced by the history file mechanism.

## Detailed Findings

### RQ1: How is `session_id` Currently Threaded?

**Flow:**
```
harness.py:303   generator_session_id = None          (fresh each sprint)
        |
harness.py:361-373
        v
run_generator(..., session_id=generator_session_id, ...)
        |
generator.py:105-112
        v
make_agent_options(..., resume=session_id)
        |
client.py:319-337
        v
ClaudeAgentOptions(resume=session_id, ...)
        |
client.py:369
        v
query(prompt=prompt, options=options)  → claude CLI with --resume <id>
        |
client.py:446
        v
result = message  [ResultMessage from SDK]
        |
generator.py:121-124
        v
return GeneratorRoundResult(session_id=result.session_id, ...)
        |
harness.py:374
        v
generator_session_id = gen_round.session_id   → fed back into next round
```

**Key behaviors:**
- `generator_session_id` is a local variable inside the sprint loop (`harness.py:303`), initialized to `None` per sprint
- After each round, `gen_round.session_id` is captured and fed back in (`harness.py:374`)
- On exception, session is reset to `None` (`harness.py:430-431`)
- On `--resume` (harness-level), session is explicitly NOT restored — log says "generator session context lost" (`harness.py:319-323`)
- The critic **never** uses session_id — each `run_critic()` call starts fresh (`critic.py:60-67` has no `resume=` argument)

**What changes to scope session to one turn:**
1. `harness.py:370` — pass `session_id=None` unconditionally (or remove the parameter)
2. `harness.py:303, 374` — remove `generator_session_id` variable and assignment
3. `harness.py:304, 372, 375, 431` — remove `last_generator_input_tokens` tracking (meaningless without session continuity)
4. `generator.py:43` — `session_id` parameter becomes unused; remove or ignore
5. `GeneratorRoundResult.session_id` field — still populated by SDK but never consumed; can be kept for logging or removed
6. Critic — no changes needed

### RQ2: What Does `generator_learnings.md` Store?

**Format:** Free-form Markdown written by the Generator LLM itself via Write/Edit tools. No enforced schema — content follows the system prompt's guidance to record:
- Architecture decisions and rationale
- Patterns that scored well with the Critic
- Issues raised by Critic and how they were addressed
- Domain insights from the PRD
- Mermaid/C4 syntax rules confirmed to work

**Write mechanism:** The LLM writes/edits `{output_dir}/generator-learnings.md` during each round as instructed by `generator_system.md:30-39`. There is no `save_learnings()` Python function — the agent's own tool calls create the file.

**Read mechanism:** At the start of every `run_generator()` call, `generator.py:76-84` reads the file from disk and injects its full content into the user prompt under `## Prior Learnings`.

**Scope:** Global across all sprints and rounds — single file at `{output_dir}/generator-learnings.md`, never reset between sprints.

**Cleanup:** `clean_run_artifacts()` (`files.py:80-83`) deletes it on fresh start. Preserved on `--resume`.

**Relationship to proposed history:**

| Aspect | generator_learnings.md | Proposed generator_history.md |
|---|---|---|
| Writer | LLM (via Write tool) | Harness (Python code) |
| Format | Free-form Markdown | Structured Markdown with section labels |
| Content | Subjective learnings | Objective record of decisions/changes |
| Injection | Full content in prompt | Path only; agent searches via Read/Grep |
| Scope | Global | Global (searchable by sprint/round) |

**Recommendation:** These serve distinct purposes. `generator_learnings.md` is the LLM's own working memory (what it wants to remember). `generator_history.md` is the harness's structured record (what actually happened). They should coexist, not be unified — but `generator_learnings.md` could potentially be downsized or replaced if the history file provides sufficient structured recall.

### RQ3: Context Injection Inventory

#### Generator — Inline Context (in prompt text)

| Context | Source | Location |
|---|---|---|
| Full PRD text | `prd.read_text()` via harness | `generator.py:92` |
| Supplementary context files | `--context` flag files | `generator.py:93-94` |
| Sprint contract (full JSON) | `contract.model_dump_json()` | `generator.py:94` |
| Output directory path | `str(output_dir)` | `generator.py:95` |
| Files to produce list | `contract.files_to_produce` | `generator.py:96-97` |
| Prior critic feedback | `CriticResult` from prior round | `generator.py:63-74` |
| Prior learnings | `generator-learnings.md` content | `generator.py:77-85` |

#### Generator — Tool-Accessible Context

| Context | Mechanism |
|---|---|
| Architecture files on disk | Glob/Read in `cwd=output_dir` |
| (No explicit file paths listed) | Agent discovers files autonomously |

#### Generator — Implicit Session Context (LOST when session_id is dropped)

| Context | What it provides |
|---|---|
| Prior round tool calls | Memory of which files were written/edited |
| Prior round reasoning | Memory of architectural decisions made |
| Prior round feedback processing | Memory of how it addressed critic concerns |

**Impact of dropping session:** The PRD, contract, feedback, and learnings are all re-injected every round. The generator loses only its implicit memory of its own prior outputs and tool calls. The proposed `generator_history.md` must fill this gap.

#### Critic — Inline Context

| Context | Source | Location |
|---|---|---|
| Output directory path | `str(output_dir)` | `critic.py:50` |
| Sprint contract (full JSON) | `contract.model_dump_json()` | `critic.py:52` |
| Files to evaluate list | `contract.files_to_produce` | `critic.py:53` |
| Round number | `round_num` literal | `critic.py:54` |

#### Critic — Tool-Accessible Context

| Context | Mechanism |
|---|---|
| Architecture files | Read/Glob/Grep in `cwd=output_dir` |

#### Critic — Implicit Session Context

**None.** The critic already starts a fresh session every round. No changes needed.

### RQ4: File Naming and Location Conventions

**Workspace structure:**
```
{output_dir}/
├── contracts/
│   └── sprint-{N}.json                          (per-sprint)
├── feedback/
│   ├── sprint-{N}-round-{M}.json               (per-sprint, per-round)
│   └── sprint-{N}-round-{M}-log.json           (per-sprint, per-round)
├── decisions/                                    (created, currently empty)
├── logs/
│   └── architect-run-{YYYYMMDD-HHMMSS}.log     (per-invocation)
├── generator-learnings.md                       (global, root of output_dir)
└── [architecture .md files written by generator]

{git_root}/
└── .checkpoints/
    └── progress.json                            (global checkpoint)
```

**Convention:** Flat within typed subdirectories. Sprint/round identity encoded in filenames with hyphens, not directory nesting.

**History file placement:** Following the convention for global, cross-sprint files:
- `{output_dir}/generator-history.md` (matches `generator-learnings.md` placement)
- `{output_dir}/critic-history.md`

**Per-sprint vs. global:** Global (single file, all sprints/rounds appended) is the better fit:
1. Matches the existing `generator-learnings.md` precedent (global, root of output_dir)
2. Allows cross-sprint searching (an agent in sprint 5 can grep for a pattern from sprint 2)
3. The ticket's success criteria require grep-searchability by sprint/round/keyword — achievable in a single file with strong section headers
4. Per-sprint files would fragment the search surface and add path-construction complexity

**The `decisions/` directory** is created but unused — it could house history files, but the root of `output_dir` is more consistent with `generator-learnings.md`.

### RQ5: SDK Session Guarantees and Breakage

**What the SDK provides when `resume` is set:**
- The `claude` CLI reloads the full JSONL conversation transcript from its local session store
- The model sees all prior turns: prompts, tool calls, tool results, assistant responses
- This gives the generator implicit memory of everything it did in prior rounds

**What breaks when `session_id` is always `None`:**
- The generator loses memory of which files it wrote, what tool calls it made, and what reasoning it applied
- Since the harness re-injects PRD, contract, and feedback every round, the *input* context is preserved
- The *output* memory (what the agent produced) is the only thing lost
- This is exactly what already happens on `--resume` (harness level) — `generator_session_id` is never restored from disk

**Existing protections against session loss:**
- The crash-recovery path (`harness.py:430-431`) already resets `generator_session_id = None`
- The `--resume` path starts with `generator_session_id = None` — explicitly logged as "session context lost"
- The generator's learnings file persists across session resets
- Architecture files are on disk for the agent to re-discover via Glob/Read

**Test coverage:**
- `test_harness_retry.py:267-318` — verifies session reset after crash
- `test_client.py:308-329` — verifies `resume` cleared on `run_agent` retry

**Conclusion:** No harness-critical behavior depends on session continuity. The harness already handles the "no session" case as a normal code path. The only consequence is reduced generator efficiency in later rounds (it must re-discover its own prior work via file inspection). The proposed history files mitigate this by giving the agent a structured summary to search.

## Architecture Insights

### Key Design Principle: File-Mediated Handoff

PROJ-0003 normalizes what is already the resume behavior into the standard execution model. The insight from ADR-011 ("session context lost on resume") already proves the system can function without session continuity. PROJ-0003 makes this the only mode.

### Two Distinct "Resume" Concepts

1. **Harness-level `--resume`**: Reloads `HarnessProgress` from `.checkpoints/progress.json`. Pure file-based. No SDK session.
2. **SDK-level `resume`**: Passes `--resume <session_id>` to the `claude` CLI, loading prior conversation history. This is what PROJ-0003 eliminates.

### History Files vs. Learnings: Complementary, Not Redundant

`generator_learnings.md` is the LLM's subjective working memory (written by the agent, injected into prompt). `generator_history.md` would be the harness's objective structured record (written by Python, path-injected for tool search). They serve different roles and could coexist, though the ticket asks for reconciliation.

### ADR-004 Will Be Superseded

ADR-004 ("Generator Session Persistence") explicitly established the current pattern. PROJ-0003 overturns it. A new ADR (or an update to ADR-004 with status "Superseded") should document the new decision and rationale.

## Historical Context (from knowledge/)

### Directly Relevant ADRs
- `knowledge/adr/ADR-004-generator-session-persistence.md` — The decision PROJ-0003 supersedes. Established `session_id` reuse across rounds within a sprint.
- `knowledge/adr/ADR-011-resume-via-progress-json.md` — Documents that session context is lost on resume and the system handles it. PROJ-0003 normalizes this.
- `knowledge/adr/ADR-003-asymmetric-tool-access.md` — Generator and critic tool lists. No changes needed for history file access (agents already have Read/Grep).
- `knowledge/adr/ADR-005-dual-llm-call-patterns.md` — History appending is a harness-side operation (Python `Path.open("a")`), not an agent operation.
- `knowledge/adr/ADR-015-dual-retry-layers.md` — Agent-level retry within a single call is unaffected. Round-level retry session reset becomes a no-op.

### Related Tickets and Plans
- `knowledge/tickets/PROJ-0001.md` — Original harness implementation ticket
- `knowledge/tickets/PROJ-0002.md` — Resume/checkpoint support (fully implemented, merged)
- `knowledge/plans/2026-04-10-PROJ-0002-resume-checkpoint-support.md` — PROJ-0002 plan (provides baseline for how file-based state works)
- `knowledge/research/2026-04-10-PROJ-0002-resume-checkpoint-support.md` — PROJ-0002 research (documents `io/files.py` function inventory)

## Code References

### session_id Threading
- `deep_researcher/harness.py:303` — `generator_session_id = None` per sprint
- `deep_researcher/harness.py:361-375` — session passed to generator, captured from return
- `deep_researcher/harness.py:430-431` — session reset on exception
- `deep_researcher/harness.py:319-323` — "session context lost" on harness resume
- `deep_researcher/agents/generator.py:43` — `session_id` parameter on `run_generator()`
- `deep_researcher/agents/generator.py:105-112` — `make_agent_options(resume=session_id)`
- `deep_researcher/agents/generator.py:121-124` — `session_id` returned in `GeneratorRoundResult`
- `deep_researcher/agents/client.py:295-337` — `make_agent_options()` with `resume` parameter
- `deep_researcher/agents/client.py:455-461` — `resume` cleared on agent-level retry

### generator_learnings.md
- `deep_researcher/agents/generator.py:76-85` — read and inject into prompt
- `deep_researcher/prompts/generator_system.md:28-39` — LLM instructions to maintain the file
- `deep_researcher/io/files.py:80-83` — deleted on clean start

### Context Injection
- `deep_researcher/agents/generator.py:63-102` — full prompt construction (PRD, contract, feedback, learnings)
- `deep_researcher/agents/critic.py:49-57` — critic prompt construction (contract, files, round number)

### File I/O Patterns
- `deep_researcher/io/files.py:12-15` — `init_workspace()` creates subdirectories
- `deep_researcher/io/files.py:18-21` — `save_contract()` pattern: `contracts/sprint-{N}.json`
- `deep_researcher/io/files.py:29-34` — `save_feedback()` pattern: `feedback/sprint-{N}-round-{M}.json`
- `deep_researcher/io/files.py:42-48` — `save_progress()` atomic write pattern

### Tests
- `tests/test_harness_retry.py:267-318` — session reset after crash
- `tests/test_client.py:308-329` — `resume` cleared on retry
- `tests/test_files.py:165-184` — learnings cleanup test

## Open Questions

1. **History file content granularity:** What should each generator history entry contain? Options range from a minimal "changed files X, Y, Z in response to feedback A, B" to a detailed summary of all decisions made. The prompt will need to instruct the agent on how to produce a summary, or the harness will need to extract it from the `ResultMessage`.

2. **Who writes the history entries?** The ticket says "Generator appends a structured history entry" — is this the LLM writing via Write tool (like learnings), or the harness appending after the agent returns (like feedback JSON)? The harness-writes approach is more reliable and consistent with the feedback pattern, but the LLM-writes approach captures richer decision context.

3. **Learnings reconciliation strategy:** Keep both files? Merge learnings into history? Remove learnings in favor of history? The answer depends on whether the history file's structured entries can replace the LLM's subjective memory, or whether both perspectives are valuable.

4. **History injection method for PROJ-0003:** The ticket says history is "NOT auto-loaded into context" — the agent is given the path and can Read/Grep it. But should the harness provide a brief summary (e.g., "3 rounds completed, last score 6.5/10") inline, with the full history available via tools? This would give the agent orientation without bloating context.

5. **ADR update process:** ADR-004 needs to be superseded. Should a new ADR-021 be created, or should ADR-004 be updated in-place with a "Superseded by PROJ-0003" status?
