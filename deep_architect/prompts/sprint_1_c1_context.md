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

## Flowchart Mermaid Template
```
---
title: C1 System Context for [System Name]
---
flowchart LR
    user(["User Role\n(Actor)"])
    sys["System Name\n(Internal System)"]
    ext[["External System\n(External)"]]

    user -->|"Uses via HTTPS"| sys
    sys -->|"Calls REST API"| ext
```

## File Structure for c1-context.md
1. `# C1 System Context` heading
2. Brief narrative (2–3 sentences) explaining the system's purpose
3. Mermaid flowchart diagram
4. `## Relationships` section describing each arrow

## Quality Checklist
- [ ] Every external system from the PRD is represented
- [ ] All user personas from the PRD appear as Person nodes
- [ ] Relationship labels are specific (not just "uses")
- [ ] Technology strings on external systems where known
- [ ] YAML frontmatter `title:` present at top of diagram block
