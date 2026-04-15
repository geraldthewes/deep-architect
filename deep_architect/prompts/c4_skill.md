# C4 Architecture Skill Guide
<!-- Source: c4model.com + community best practices (Mermaid C4, InfoQ, WorkingSoftware) -->

This guide defines what makes a good C4 architecture diagram. Follow it to produce
diagrams that communicate clearly to both technical and non-technical stakeholders.
For Mermaid syntax rules, see the **Mermaid Flowchart Reference Guide** (appended separately).

---

## Philosophy

C4 is a zoom model. Each level zooms into the previous one:

1. **System Context** — your system in the world
2. **Container** — your system's deployable parts
3. **Component** — the internals of one container
4. **Code** — the internals of one component (almost never needed)

One diagram = one level = one audience. Never mix levels. A diagram that tries to show
everything communicates nothing. Prefer multiple focused diagrams over one crowded one.

---

## The Four Levels

### Level 1: System Context

**Purpose**: Show the system boundary, who uses it, and what external systems it depends on.

**What to include**:
- The system itself — one rectangle node
- Every user persona from the PRD — actor nodes (`(["Role\n(Actor)"])`)
- Every third-party or external integration — subroutine nodes (`[["Name\n(External)"]]`)
- Key data/interaction flows — labeled edges (`-->|"action via protocol"|`)

**What to exclude**:
- Internal containers, services, or databases — those belong in C2
- Implementation details (frameworks, languages, protocols) — wrong level
- Internal sub-systems — treat the whole product as one node

**Quality bar**: A non-technical stakeholder should be able to read this diagram and
understand *who uses the system* and *what it connects to* without any explanation.

---

### Level 2: Container Diagram

**Purpose**: Show the deployable building blocks of the system and how they communicate.

**Container definition**: A container is anything that runs independently and is deployed
separately — a web app, mobile app, API service, background worker, database, message
broker, cache, blob store, or CDN. It is NOT a library, namespace, package, or class.

**What to include**:
- Every independently deployable unit — rectangle node with technology in label,
  e.g., `api["API Server\n(Go, Docker)"]`
- Databases — cylinder nodes, e.g., `db[("PostgreSQL\n(Primary DB)")]`
- External systems from C1 that this system talks to — subroutine nodes `[["Name\n(External)"]]`
- User personas that directly interact with containers — actor nodes `(["Role\n(Actor)"])`
- All significant inter-container communication — labeled edges `-->|"action via protocol"|`

**What to exclude**:
- Internal classes, modules, or functions — those belong in C3
- Implementation details within a container (routes, schemas, SQL queries)
- Shared libraries — they are components within containers, not separate containers

**Grouping**: Wrap your system's containers in a `subgraph` block. Use nested subgraphs
only when you need to show sub-system groupings (e.g., DMZ within VPC).

**Quality bar**: A developer joining the team should be able to use this diagram to
understand the deployment topology and choose which container to work in.

---

### Level 3: Component Diagram

**Purpose**: Show the internal structure of one specific container.

**When to create**: Only when a container is complex enough that its internal organization
needs explanation. Do NOT create C3 diagrams for every container by default.

**What to include**:
- Major non-deployable building blocks within the container — rectangle nodes inside a `subgraph` boundary
- Key internal data flows and dependencies — labeled edges `-->|"label"|`
- External systems or containers the components depend on directly

**What to exclude**:
- Fine-grained classes, functions, or methods — those are Level 4
- Auto-generated code (routes from OpenAPI, schema types) — link to spec instead

---

### Level 4: Code Diagrams — Skip Them

Code diagrams (class diagrams, ER diagrams at the field level) become stale within days
of writing. They are almost never worth maintaining. If you need to document code
structure, link to the source code or use auto-generated tooling instead.

**Exception**: An ER diagram showing the logical data model (entity relationships, not
table columns) can be valuable if it genuinely reflects stable design decisions.

---

## Naming Conventions

| Element | Rule | Example |
|---------|------|---------|
| Person / Actor | Job role or user type, not "user" | `"Mobile Customer"`, `"Operations Admin"` |
| System node | Full product name | `"Payment Platform"` |
| External system node | Service brand or category | `"Stripe"`, `"SendGrid"`, `"Auth0"` |
| Container node | Function + technology hint | `"API Gateway\n(Kong)"`, `"Worker Service"` |
| Database node | Data type + technology | `"User Database\n(PostgreSQL)"` |
| Queue node | Queue name + broker | `"Job Queue\n(RabbitMQ)"` |
| Component node | Responsibility-based | `"JWT Auth Middleware"`, `"Invoice Generator"` |

**Avoid**: Generic names like `"API"`, `"Service"`, `"Backend"`, `"Database"` without
qualification. If you have more than one container of the same kind, each must have a
distinct, specific name.

---

## Relationship Quality Rules

Every edge must answer two questions: **what flows?** and **how?**

```
spa -->|"Submits orders via REST/HTTPS"| api     ✓ specific action + protocol
api -->|"Reads/writes user records via SQL"| db  ✓ specific action + technology
api -->|"Enqueues invoice jobs via AMQP"| queue  ✓ specific action + protocol

spa -->|"Uses"| api                              ✗ vague — what does it use it for?
api -->|"Calls"| db                              ✗ vague — what kind of call?
a -->|"Depends on"| b                            ✗ not a relationship label
```

**Rules**:
- Always include the technology/protocol in the edge label: `"Calls via REST/HTTPS"`
- Use active verbs: "Sends", "Queries", "Publishes", "Authenticates via"
- If two elements have multiple distinct interactions, use separate edges
- Use `<-->` for genuinely bidirectional flows (e.g., WebSocket); otherwise use two directed edges

---

## Boundary Usage

Use `subgraph` blocks for all boundaries:

- **System boundary (C2)** — `subgraph sys["System Name"]` wrapping container nodes
- **Container boundary (C3)** — `subgraph svc["Service Name"]` wrapping component nodes
- **Enterprise boundary** — nested subgraphs when multiple systems belong to one organization

**Rule**: Every element that belongs to a boundary must be defined *inside* the
`subgraph ... end` block. Nodes defined outside the block cannot be visually grouped inside it.

---

## DOs

- **Include a title** on every diagram via YAML frontmatter (`---` / `title: ...` / `---`)
- **Specify technology** in every container and component node label
- **Label relationships** with action AND technology/protocol
- **Use external node shapes** (`[["Name\n(External)"]]`) for elements outside your system boundary
- **Validate with `mmdc`** after writing any diagram — fix parse errors before moving on
- **Split crowded diagrams** — if you exceed ~12 elements, create area-specific sub-diagrams
  (e.g., `frontend/c2-container.md`, `backend/c2-container.md`)
- **Show every external dependency** from the PRD at the appropriate level
- **Use the right scope per sprint**: C1 shows only users and external systems; C2 shows
  deployable containers; C3 shows internals of one container

---

## DON'Ts

- **Don't mix abstraction levels** — no component details in a container diagram, no
  container details in a context diagram
- **Don't show external system internals** — an external system node is a black box; never
  nest elements inside a subgraph labeled as external
- **Don't model shared libraries as containers** — a library imported by multiple services
  is a component that lives inside those services, not a standalone container
- **Don't leave relationship labels vague** — "Uses", "Calls", "Depends on" are not
  acceptable as final labels
- **Don't omit the title** — diagrams without titles are ambiguous in documentation
- **Don't create C3 diagrams for every container** — only create them where the PRD or
  design complexity justifies decomposition
- **Don't use `%%{init}%%` directives** — GitHub's Mermaid renderer ignores them
- **Don't use C4 Mermaid block types** — `C4Context`, `C4Container`, and `C4Component`
  have inconsistent tooling support; always use `flowchart LR` or `flowchart TD`
- **Don't crowd a diagram with 15+ elements** without subgraph groupings
- **Don't create Level 4 code diagrams** unless specifically required by the PRD and
  you can commit to keeping them current

---

## Common Antipatterns

These are named failures to check for and avoid:

1. **Context Bleed** — actor or external system nodes appear in a C2 container diagram
   but have no edge connecting them to any container node. Either connect them or remove them.

2. **Phantom Container** — a container or component node has no inbound or outbound edge.
   Every node must participate in at least one relationship or it has no reason to exist
   in the diagram.

3. **External Internals** — an external system node has child elements defined inside a
   subgraph that is nested under it, exposing internals of a system you don't own. Model
   the boundary only; never model what's inside it.

4. **Library-as-Container** — a shared utility package, SDK, or library is modeled as
   a container. Libraries are not independently deployed; they are components that appear
   inside multiple containers.

5. **God Container** — a single container called "API", "Backend", or "Service" has 10+
   relationships going to/from it. This signals that the container should be split into
   multiple focused containers or the diagram needs area-specific sub-diagrams.

6. **Missing Technology** — a container or component node has no technology specified in
   its label: e.g., `api["API Server"]` with no tech annotation. Every container must have a
   specific technology choice from the PRD.

7. **Relationship Fog** — every edge in the diagram uses the same label: "Uses", "Calls",
   or "Depends on". This tells readers nothing. Each edge needs a specific action
   verb describing what actually flows.

8. **Level Mixing** — component-level nodes appear in a system context or container diagram
   without being grouped in a subgraph boundary marking them as internals of one container.

9. **Overcrowded Diagram** — a single diagram has more than ~15 nodes with no subgraph
   groupings. Split into multiple focused diagrams, one per subsystem or area.

10. **Title Omission** — the diagram has no `title` in its YAML frontmatter. Without a
    title, it is impossible to tell at a glance which system and level the diagram represents.
