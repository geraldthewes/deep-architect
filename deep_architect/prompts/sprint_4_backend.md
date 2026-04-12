# Sprint 4: Backend / Orchestration Container Guidance
<!-- Bootstrap: PRD §5.2 Sprint 4 -->

## Goal
Produce `backend/c2-container.md` and area-specific documents as needed
(e.g. `backend/api-design.md`, `backend/orchestration.md`, `backend/workers.md`).

## What to Document
- API server architecture and framework
- Business logic organization
- Background job / worker architecture
- LLM orchestration layer (if applicable)
- Internal service communication
- Error handling and retry patterns

## C4Container for Backend
Show containers within the backend boundary:
- API server / gateway
- Business logic service(s)
- Background workers / task queues
- LLM orchestration / agent framework (if applicable)
- Cache layer

## File Structure for backend/c2-container.md
1. `# Backend Container` heading
2. Narrative describing backend architecture
3. Mermaid C4Container diagram scoped to backend
4. `## API Design Principles` section
5. `## Orchestration` section (if LLM/agent workflows are in scope)
6. `## Background Processing` section

## Additional Files (create if PRD warrants)
- `backend/api-design.md`: REST/GraphQL conventions, versioning, authentication middleware
- `backend/orchestration.md`: agent loops, tool use, prompt management
- `backend/workers.md`: async task architecture, queue technology

## Quality Checklist
- [ ] All backend services from PRD are represented
- [ ] LLM/AI components have their own containers with specific framework names
- [ ] Queue/async architecture documented if PRD requires background processing
- [ ] Internal service communication patterns specified
- [ ] Error handling and retry strategy noted
