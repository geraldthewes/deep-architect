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

## C4Container Mermaid Template
```
C4Container
  title Container Diagram for [System Name]

  Person(user, "User", "Description")

  System_Boundary(sys, "System Name") {
    Container(web, "Web App", "React/Next.js", "User interface")
    Container(api, "API Server", "FastAPI/Python", "Business logic")
    Container(db, "Database", "PostgreSQL", "Persistent data store")
  }

  System_Ext(ext, "External System", "Purpose")

  Rel(user, web, "Uses", "HTTPS")
  Rel(web, api, "Calls", "REST/HTTPS")
  Rel(api, db, "Reads/Writes", "SQL")
  Rel(api, ext, "Calls", "REST/HTTPS")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

## File Structure for c2-container.md
1. `# C2 Container Overview` heading
2. Narrative (3–5 sentences) covering the overall architecture approach
3. Mermaid C4Container diagram
4. `## Technology Decisions` section with rationale for each major choice
5. `## Container Relationships` section describing key interactions

## Quality Checklist
- [ ] All containers from PRD architecture section are represented
- [ ] Technology strings are specific (not just "some database")
- [ ] Relationships have meaningful labels
- [ ] External systems from C1 are referenced
- [ ] Technology decisions include brief rationale
