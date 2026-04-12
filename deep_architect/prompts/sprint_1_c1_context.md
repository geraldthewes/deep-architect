# Sprint 1: C1 System Context Guidance
<!-- Bootstrap: PRD §5.2 Sprint 1 + bmad-create-architecture/steps/step-02-context.md -->

## Goal
Produce `c1-context.md`: the C4 Level 1 System Context diagram showing the system boundary,
its primary users, and all external dependencies.

## What to Extract from the PRD
- Primary users and their roles (→ `Person` nodes)
- External systems the product integrates with (→ `System_Ext` nodes)
- The core system itself (→ `System` node)
- Key data flows and integration points (→ relationship labels)

## C4Context Mermaid Template
```
C4Context
  title System Context for [System Name]

  Person(user, "User Role", "Brief description")
  System(sys, "System Name", "What it does")
  System_Ext(ext, "External System", "Purpose")

  Rel(user, sys, "Uses", "HTTPS")
  Rel(sys, ext, "Calls", "REST API")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

## File Structure for c1-context.md
1. `# C1 System Context` heading
2. Brief narrative (2–3 sentences) explaining the system's purpose
3. Mermaid C4Context diagram
4. `## Relationships` section describing each arrow

## Quality Checklist
- [ ] Every external system from the PRD is represented
- [ ] All user personas from the PRD appear as Person nodes
- [ ] Relationship labels are specific (not just "uses")
- [ ] Technology strings on external systems where known
- [ ] UpdateLayoutConfig present at end of diagram
