# ADR-007: Seven Fixed Sprints with Pre-Defined C4 Progression

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The harness must produce a complete C4 architecture document. The number and scope of sprints could be variable (user-configurable) or fixed by design.

## Decision

The harness is fixed at **7 sprints**, each producing a specific C4 artifact:

| # | Name | Output |
|---|------|--------|
| 1 | C1 System Context | System context diagram + narrative |
| 2 | C2 Container Overview | Container overview diagram |
| 3 | Frontend Container | Frontend container detail |
| 4 | Backend/Orchestration | Backend container detail |
| 5 | Database + Knowledge Base | Data layer detail |
| 6 | Edge/Deploy/Auth/Observability | Operational concerns |
| 7 | ADRs + Cross-Cutting Concerns | Architecture decisions + cross-cutting |

## Rationale

- **Predictability:** Fixed sprints mean every run produces the same set of artifacts. Users know exactly what they will get.
- **No decision fatigue:** The 7-sprint structure encodes BMAD C4 methodology. Variable sprints would require users to configure what to produce — defeating the purpose of an autonomous harness.
- **Battle-tested scope:** 7 sprints covers the full C4 stack from system context down to operational concerns and ADRs. Fewer would miss critical detail; more would add redundancy.
- **Simplicity:** Fixed count means `progress.total = 7` and the loop is a simple `for sprint in SPRINTS`.

## Consequences

- Adding a sprint requires: (1) entry in `SPRINTS` list, (2) new prompt `.md` file, (3) update `EXPECTED_PROMPTS` in tests, (4) update progress total.
- **Constraint (CLAUDE.md):** "Do not add more than 7 sprints without a strong reason — they are fixed by design."
- Users cannot add sprints via config; changes require code modification (by design — sprints are not a plugin point).

**Files:** `deep_researcher/sprints.py`, `deep_researcher/prompts/sprint_*.md`, `deep_researcher/harness.py`
