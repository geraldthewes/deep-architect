# Sprint 6: Edge Functions / Deployment / Auth / Observability Guidance
<!-- Bootstrap: PRD §5.2 Sprint 6 + bmad-create-architecture/steps/step-06-structure.md -->

## Goal
Produce `edge-functions/c2-container.md` and `deployment.md` covering cross-cutting concerns:
authentication, authorization, scaling, and observability.

## What to Document

### Edge Functions
- CDN edge functions and their purpose
- Request routing and middleware
- Rate limiting and throttling at edge
- Geographic distribution considerations

### Deployment Architecture
- Container orchestration (Nomad, Kubernetes, ECS, etc.)
- CI/CD pipeline stages
- Environment strategy (dev/staging/prod)
- Infrastructure-as-code tooling

### Authentication & Authorization
- Auth provider and protocol (OAuth2, OIDC, API keys)
- Token storage and refresh strategy
- RBAC/ABAC model
- API gateway auth enforcement

### Scaling
- Horizontal scaling approach
- Auto-scaling triggers and limits
- Database connection pooling
- Cache warming strategy

### Observability
- Logging aggregation approach
- Metrics collection (Prometheus, CloudWatch, etc.)
- Distributed tracing
- Alerting strategy

## Quality Checklist
- [ ] Deployment topology matches infrastructure from PRD
- [ ] Auth flow is end-to-end (browser → edge → API → database)
- [ ] Observability covers logs, metrics, and traces
- [ ] Scaling limits are quantified where possible
- [ ] Edge function responsibilities are distinct from API server responsibilities
