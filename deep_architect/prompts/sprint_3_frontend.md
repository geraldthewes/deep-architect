# Sprint 3: Frontend Container Guidance
<!-- Bootstrap: PRD §5.2 Sprint 3 + bmad-create-architecture/steps/step-05-patterns.md -->

## Goal
Produce `frontend/c2-container.md` and any area-specific documents warranted by the PRD
(e.g. `frontend/auth.md`, `frontend/routing.md`, `frontend/state-management.md`).

## What to Document
- Frontend framework and rendering strategy
- Component hierarchy and key pages/views
- State management approach
- Authentication flow (if applicable)
- API communication pattern
- Build and deployment approach

## Flowchart for Frontend
Show containers within the frontend boundary:
- Browser/client application container
- Any server-side rendering container
- CDN / static asset delivery
- Key external services the frontend calls directly

## File Structure for frontend/c2-container.md
1. `# Frontend Container` heading
2. Narrative describing the frontend architecture approach
3. Mermaid flowchart diagram scoped to the frontend
4. `## Key Design Decisions` section
5. `## Integration Points` section (how frontend talks to backend)

## Additional Files (create if PRD warrants)
- `frontend/auth.md`: authentication flows, token storage, session management
- `frontend/routing.md`: navigation structure, route protection
- `frontend/state-management.md`: state approach, data fetching patterns
- `frontend/schemas.md`: TypeScript types, API response shapes

## Quality Checklist
- [ ] Framework choice matches C2 overview
- [ ] All major UI features from PRD have corresponding containers/components
- [ ] Auth flow documented if authentication is required
- [ ] API communication pattern specified (REST client, GraphQL client, etc.)
- [ ] Rendering strategy justified (SSR/CSR/SSG rationale)
