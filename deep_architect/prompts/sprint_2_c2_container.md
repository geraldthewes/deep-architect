# Sprint 2: C2 Container Overview Guidance
<!-- Bootstrap: PRD §5.2 Sprint 2 + bmad-create-architecture/steps/step-04-decisions.md -->

## Goal
Produce `c2-container.md`: the overall C4 Level 2 Container diagram showing all major
containers and their relationships, plus a technology stack decision summary.

## Decision Categories to Cover
- **Data**: Primary database, caching, object storage choices
- **Auth**: Authentication mechanism and provider
- **API**: REST vs GraphQL vs gRPC, API gateway
- **Frontend**: Framework, rendering strategy (SSR/CSR/SSG)
- **Infrastructure**: Cloud provider, containerization, orchestration

## Flowchart Mermaid Template
```
---
title: C2 Container Diagram for [System Name]
---
flowchart LR
    user(["User\n(Actor)"])

    subgraph sys["System Name"]
        web["Web App\n(React/Next.js, Docker)"]
        api["API Server\n(FastAPI/Python, Docker)"]
        db[("Database\n(PostgreSQL)")]
    end

    ext[["External System\n(External)"]]

    user -->|"Uses via HTTPS"| web
    web -->|"Calls via REST/HTTPS"| api
    api -->|"Reads/Writes via SQL"| db
    api -->|"Calls via REST/HTTPS"| ext
```

## File Structure for c2-container.md
1. `# C2 Container Overview` heading
2. Narrative (3–5 sentences) covering the overall architecture approach
3. Mermaid flowchart diagram
4. `## Technology Decisions` section with rationale for each major choice
5. `## Container Relationships` section describing key interactions

## Quality Checklist
- [ ] All containers from PRD architecture section are represented
- [ ] Technology strings are specific (not just "some database")
- [ ] Relationships have meaningful labels
- [ ] External systems from C1 are referenced
- [ ] Technology decisions include brief rationale
- [ ] YAML frontmatter `title:` present at top of diagram block
