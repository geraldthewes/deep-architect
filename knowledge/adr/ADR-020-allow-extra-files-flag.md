# ADR-020: Permissive Extra Files via allow_extra_files Sprint Flag

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

Each sprint has a list of `primary_files` it is expected to produce (e.g., sprint 3 produces `knowledge/architecture/frontend/c2-container.md`). Should the generator be strictly limited to these files, or can it create additional supporting documents?

## Decision

Each `SprintDefinition` has an `allow_extra_files: bool` flag. When `True`, the generator may create additional files beyond `primary_files` (e.g., `frontend/auth-flow.md`, `frontend/component-tree.md`). When `False`, the sprint scope is strict.

Sprints 3-6 (container-level detail sprints) have `allow_extra_files=True`. Sprints 1-2 and 7 are strict.

## Rationale

- **Natural elaboration:** Container-level sprints (frontend, backend, database) organically produce sub-documents (auth flows, schema definitions, deployment diagrams). Blocking this would require the generator to cram everything into one file.
- **Sprint 1/2 strictness:** The C1 context and C2 overview are intentionally concise; extra files would dilute the focus.
- **Sprint 7 strictness:** ADRs have a fixed structure; extra files would create unreviewed artifacts outside the sprint contract.
- **The flag is in the contract:** The generator is informed of this constraint via the sprint contract, so it knows when to elaborate and when to stay focused.

## Consequences

- The critic does not penalize extra files when `allow_extra_files=True`; it reviews them as part of the sprint's output.
- `get_modified_files()` returns all changed files; the harness commits them regardless of whether they were in `primary_files`.
- Sprint-level file contracts (in `SprintContract.files_to_produce`) may be extended by the generator when the flag is set.

**Files:** `deep_researcher/sprints.py:12,43,52,63,82`
