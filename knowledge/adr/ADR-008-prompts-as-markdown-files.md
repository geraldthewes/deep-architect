# ADR-008: Prompts Stored as .md Files, Loaded at Runtime

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The harness uses 13+ prompt templates for system prompts, sprint-specific instructions, and contract negotiation. These could be embedded in Python source, stored in a database, or kept as external files.

## Decision

All prompt templates are stored as `.md` files in `deep_researcher/prompts/`. They are loaded at runtime via `load_prompt(name, **kwargs)` which performs `str.format_map(**kwargs)` for variable substitution.

The full list must match `EXPECTED_PROMPTS` in `tests/test_prompts.py` — if any file is missing, tests fail.

## Rationale

- **Editability:** Prompts are living documents. Users can refine generator or critic behavior by editing the `.md` file without touching Python and without reinstalling.
- **Readability:** Markdown is readable as documentation; Python string literals with long prompts are unwieldy.
- **Version control:** Prompt changes show up as clean diffs in git, making it easy to track prompt evolution.
- **Separation of concerns:** Prompt content is data, not code. Keeping it in files rather than Python strings reflects this.

## Consequences

- All 13 prompt files must exist. A missing file causes `FileNotFoundError` at runtime (and test failure at test time).
- Variable substitution uses `str.format_map` — prompt files use `{variable_name}` syntax for interpolation.
- Users can fork the repo and customize prompts for their domain (e.g., different C4 conventions for embedded systems) without modifying Python.
- The test `test_prompts.py` acts as a guard against accidentally deleting or renaming a prompt file.

**Files:** `deep_researcher/prompts/__init__.py`, `deep_researcher/prompts/*.md`, `tests/test_prompts.py`
