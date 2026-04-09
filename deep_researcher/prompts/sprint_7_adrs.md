# Sprint 7: ADRs + Cross-Cutting Concerns Guidance
<!-- Bootstrap: PRD §5.2 Sprint 7 + bmad-create-architecture/architecture-decision-template.md -->

## Goal
Generate Architecture Decision Records (ADRs) in `decisions/` covering major architectural
choices, and document non-functional requirements and cross-cutting concerns.

## ADR Format

Each ADR file should follow this template:

```markdown
# ADR-NNN: [Decision Title]

**Status**: Accepted | Superseded | Deprecated
**Date**: YYYY-MM-DD

## Context

What is the situation that requires a decision? What are the forces at play?

## Decision

What was decided? State it clearly in one sentence, then elaborate.

## Rationale

Why was this option chosen over alternatives?
List the alternatives considered and why they were rejected.

## Consequences

**Positive**:
- ...

**Negative / Trade-offs**:
- ...

**Risks**:
- ...
```

## ADRs to Generate

Generate ADRs for the major decisions made across all sprints, including:
- Primary database choice
- Frontend framework choice
- API design approach (REST vs GraphQL)
- Authentication strategy
- LLM/AI framework choice (if applicable)
- Deployment platform choice
- Any significant non-obvious trade-offs from the PRD

Use sequential numbering: `ADR-001.md`, `ADR-002.md`, etc.

## Cross-Cutting Concerns Document

Also produce `decisions/cross-cutting.md` covering:
- Security: OWASP considerations, secrets management
- Performance: SLA targets, caching strategy, N+1 query prevention
- Reliability: retry policies, circuit breakers, graceful degradation
- Maintainability: code organization, testing strategy, documentation standards
- Compliance: data residency, GDPR considerations (if applicable)

## Quality Checklist
- [ ] Every major technology choice from Sprints 1–6 has a corresponding ADR
- [ ] Each ADR includes at least two alternatives considered
- [ ] Consequences section is honest about trade-offs
- [ ] Cross-cutting concerns document is complete and specific
- [ ] ADR status fields are accurate
