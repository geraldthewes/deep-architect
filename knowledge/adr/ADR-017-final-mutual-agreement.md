# ADR-017: Final Mutual Agreement Round After All Sprints

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

After all 7 sprints pass their individual exit criteria, the harness could consider the run complete. Alternatively, it could run a final holistic review of the complete architecture.

## Decision

After all 7 sprints complete, run `run_final_agreement()`: both the generator and critic independently inspect the **full set of architecture files** and output the string `"READY_TO_SHIP"` if they are satisfied with the complete architecture. Both use read-only tools (`["Read", "Glob", "Grep"]`).

If either agent does not output `"READY_TO_SHIP"`, the harness logs a warning but does not fail the run.

## Rationale

- **Holistic verification:** Individual sprints are scoped (e.g., sprint 3 focuses on the frontend container). The final round gives both agents a chance to evaluate the architecture as a whole — including consistency across sprints, completeness of ADRs, and cross-cutting concerns.
- **Mutual agreement:** Having both agents independently signal readiness mirrors the human workflow: architect and reviewer both sign off before a design is shipped.
- **Non-blocking warning:** If agreement fails, it is a signal to the user that they may want to review manually. The architecture files are still committed and usable.

## Consequences

- Adds one extra LLM call (two agents, read-only) at the end of each run — relatively cheap.
- The `final_agreement.md` prompt instructs both agents to review the full architecture directory.
- A warning in the output log flags when `"READY_TO_SHIP"` is not returned, giving users a clear signal to review.

**Files:** `deep_architect/harness.py:107-151`, `deep_architect/prompts/final_agreement.md`
