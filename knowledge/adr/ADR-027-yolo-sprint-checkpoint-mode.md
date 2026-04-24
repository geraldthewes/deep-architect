# ADR-027: Sprint-by-Sprint Checkpoint Mode (`--yolo` Escape Hatch)

**Status:** Accepted  
**Date:** 2026-04-24  
**Deciders:** Project design  
**Supersedes:** —  
**Related:** ADR-011 (resume via progress.json), ADR-024 (soft-fail sprint), PROJ-0010

---

## Context

Previously, `adversarial-architect` ran all 7 sprints unattended by default. If the generator produced a flawed foundation in sprint 2, sprints 3–7 would build on that error. There was no way for a user to inspect, and optionally edit, sprint output before the next sprint began.

Human review between sprints allows early course-correction and is especially valuable for the first few sprints, which establish the system context and container topology that all later sprints inherit.

## Decision

**Invert the default**: the harness now stops after every sprint, prints the files written and a resume command, and exits with code 0. The original unattended behavior is opt-in via a new `--yolo` flag.

Concretely:

- **Default (no `--yolo`)**: after each sprint's boundary commit and documentation generation, call `_print_sprint_pause()` and `return`. All state is fully persisted before the return — `progress.json`, contracts, feedback, git commits — so a subsequent `--resume` picks up cleanly at sprint N+1.
- **`--yolo`**: the pause is skipped; all 7 sprints plus the final mutual-agreement round run in one uninterrupted invocation (previous behavior).
- **`--resume` without `--yolo`**: runs the next sprint, then stops again.
- **`--resume --yolo`**: runs all remaining sprints plus final agreement unattended.

`--yolo` is **per-invocation only** — it is not persisted to `progress.json` or any checkpoint file. Each invocation independently decides whether to pause.

After sprint 7 stops in non-yolo mode, the next `--resume` (with or without `--yolo`) triggers the final mutual-agreement round automatically: the outer sprint loop finds all sprints already passed and falls through to `run_final_agreement`.

## Consequences

### Positive
- Users get a natural review point between every sprint; errors caught at sprint 1 do not propagate through 6 more sprints.
- No new state, no new resume logic — the existing `progress.json` / `--resume` mechanism from ADR-011 is reused unchanged.
- `--yolo` fully preserves the original unattended experience for CI/batch use.
- The pause point (after sprint-boundary commit and documentation) guarantees the workspace is clean and git-consistent at every stop.

### Negative
- Users who previously relied on unattended default behavior must add `--yolo` to their invocations — a breaking behavior change.
- An extra invocation is needed per sprint in the default mode, which adds manual friction for users who have high confidence in their setup.

## Implementation Notes

- `run_harness()` gains a keyword-only parameter `yolo: bool = False`, mirroring `strict`.
- `cli.py` exposes `--yolo` (default: `False`) via Typer, adjacent to `--strict`.
- The pause is inserted at the very end of the outer sprint loop body in `harness.py`, after `generate_sprint_documentation`. Sprints that hit the `continue` path (already completed on resume) never reach this code.
- Sprints that end in `failed` already `return` earlier in the loop body — the pause is never shown for failure exits.
- A module-level helper `_print_sprint_pause()` formats the Rich console output: sprint number, files to review (sourced from `SprintDefinition.primary_files`), and the resume hint.
