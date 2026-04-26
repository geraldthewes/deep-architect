# ADR-008: Prompts Stored as .md Files, Loaded at Runtime

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The harness uses 19+ prompt templates for system prompts, sprint-specific instructions, and contract negotiation. These could be embedded in Python source, stored in a database, or kept as external files.

## Decision

All prompt templates are stored as `.md` files in `deep_architect/prompts/`. They are loaded at runtime via `load_prompt(name, **kwargs)` which performs `str.format_map(**kwargs)` for variable substitution.

The full list must match `EXPECTED_PROMPTS` in `tests/test_prompts.py` — if any file is missing, tests fail.

## Rationale

- **Editability:** Prompts are living documents. Users can refine generator or critic behavior by editing the `.md` file without touching Python and without reinstalling.
- **Readability:** Markdown is readable as documentation; Python string literals with long prompts are unwieldy.
- **Version control:** Prompt changes show up as clean diffs in git, making it easy to track prompt evolution.
- **Separation of concerns:** Prompt content is data, not code. Keeping it in files rather than Python strings reflects this.

## Consequences

- All prompt files must exist. A missing file causes `FileNotFoundError` at runtime (and test failure at test time). The authoritative list is `EXPECTED_PROMPTS` in `tests/test_prompts.py`.
- Variable substitution uses `str.format_map` — prompt files use `{variable_name}` syntax for interpolation. Not all prompts require substitution; `load_prompt()` skips `format_map` when no kwargs are passed.
- Each `SprintDefinition` carries a `prompt_name` field. `run_generator()` loads that prompt via `load_prompt(sprint.prompt_name)` and injects it as a `## Sprint Guidance` section into the generator user prompt each round. This is the primary mechanism for delivering per-sprint-specific rules (e.g., ADR naming for sprint 7) to the generator.
- Users can fork the repo and customize prompts for their domain (e.g., different C4 conventions for embedded systems) without modifying Python.
- The test `test_prompts.py` acts as a guard against accidentally deleting or renaming a prompt file; `test_all_sprint_prompt_names_resolve` additionally verifies every sprint's `prompt_name` resolves correctly.

**Files:** `deep_architect/prompts/__init__.py`, `deep_architect/prompts/*.md`, `tests/test_prompts.py`
