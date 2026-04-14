# C4 Architecture Skill Guide
<!-- Source: c4model.com + community best practices (Mermaid C4, InfoQ, WorkingSoftware) -->

This guide defines what makes a good C4 architecture diagram. Follow it to produce
diagrams that communicate clearly to both technical and non-technical stakeholders.
For Mermaid syntax rules, see the **Mermaid C4 Reference Guide** (appended separately).

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

### Level 1: System Context (`C4Context`)

**Purpose**: Show the system boundary, who uses it, and what external systems it depends on.

**What to include**:
- The system itself — one `System` node
- Every user persona from the PRD — `Person` nodes (internal), `Person_Ext` (external)
- Every third-party or external integration — `System_Ext` nodes
- Key data/interaction flows — `Rel` with meaningful labels

**What to exclude**:
- Internal containers, services, or databases — those belong in C2
- Implementation details (frameworks, languages, protocols) — wrong level
- Internal sub-systems — treat the whole product as one System node

**Quality bar**: A non-technical stakeholder should be able to read this diagram and
understand *who uses the system* and *what it connects to* without any explanation.

---

### Level 2: Container Diagram (`C4Container`)

**Purpose**: Show the deployable building blocks of the system and how they communicate.

**Container definition**: A container is anything that runs independently and is deployed
separately — a web app, mobile app, API service, background worker, database, message
broker, cache, blob store, or CDN. It is NOT a library, namespace, package, or class.

**What to include**:
- Every independently deployable unit — `Container`, `ContainerDb`, `ContainerQueue`
- Technology in every container's tech parameter — `"Go"`, `"PostgreSQL"`, `"Redis"`, etc.
- External systems from C1 that this system talks to — `System_Ext`
- User personas that directly interact with containers — `Person`
- All significant inter-container communication — `Rel` with label and technology

**What to exclude**:
- Internal classes, modules, or functions — those belong in C3
- Implementation details within a container (routes, schemas, SQL queries)
- Shared libraries — they are Components within containers, not separate Containers

**Grouping**: Wrap your system's containers in a `System_Boundary`. Use separate
`Container_Boundary` blocks only when you need to show sub-system groupings.

**Quality bar**: A developer joining the team should be able to use this diagram to
understand the deployment topology and choose which container to work in.

---

### Level 3: Component Diagram (`C4Component`)

**Purpose**: Show the internal structure of one specific container.

**When to create**: Only when a container is complex enough that its internal organization
needs explanation. Do NOT create C3 diagrams for every container by default.

**What to include**:
- Major non-deployable building blocks within the container — `Component` nodes
- Key internal data flows and dependencies — `Rel`
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
| Person | Job role or user type, not "user" | `"Mobile Customer"`, `"Operations Admin"` |
| System | Full product name | `"Payment Platform"` |
| System_Ext | Service brand or category | `"Stripe"`, `"SendGrid"`, `"Auth0"` |
| Container | Function + technology hint | `"API Gateway (Kong)"`, `"Worker Service"` |
| ContainerDb | Data type + technology | `"User Database (PostgreSQL)"` |
| ContainerQueue | Queue name + broker | `"Job Queue (RabbitMQ)"` |
| Component | Responsibility-based | `"JWT Auth Middleware"`, `"Invoice Generator"` |

**Avoid**: Generic names like `"API"`, `"Service"`, `"Backend"`, `"Database"` without
qualification. If you have more than one container of the same kind, each must have a
distinct, specific name.

---

## Relationship Quality Rules

Every `Rel` must answer two questions: **what flows?** and **how?**

```
Rel(spa, api, "Submits orders", "REST/HTTPS")       ✓ specific action + protocol
Rel(api, db,  "Reads/writes user records", "SQL")   ✓ specific action + technology
Rel(api, queue, "Enqueues invoice jobs", "AMQP")    ✓ specific action + protocol

Rel(spa, api, "Uses")                               ✗ vague — what does it use it for?
Rel(api, db,  "Calls")                              ✗ vague — what kind of call?
Rel(a, b, "Depends on")                             ✗ not a relationship label
```

**Rules**:
- Always include the technology/protocol as the fourth argument where known
- Use active verbs: "Sends", "Queries", "Publishes", "Authenticates via"
- If two elements have multiple distinct interactions, use separate `Rel` calls
- Bidirectional communication should use `BiRel` only when the data flow genuinely
  goes both directions simultaneously (e.g., WebSocket); otherwise use two `Rel` calls

---

## Boundary Usage

- `System_Boundary` — wraps your system's containers in C2 diagrams
- `Container_Boundary` — wraps components inside a container in C3 diagrams
- `Enterprise_Boundary` — wraps multiple systems within an organization
- `Boundary` — generic; use only when the above don't fit

**Rule**: Every element must be defined *inside* the boundary block's `{ }` if it
belongs to that boundary. You cannot declare an element before the boundary and then
reference it inside — that is a parse error.

---

## DOs

- **Include a `title`** on every diagram — `title System Context for Payment Platform`
- **Specify technology** in every Container and Component tech parameter
- **Label relationships** with action AND technology/protocol
- **Use `_Ext` variants** (`System_Ext`, `Person_Ext`, `Container_Ext`) for elements
  outside your system boundary
- **Validate with `mmdc`** after writing any diagram — fix parse errors before moving on
- **Split crowded diagrams** — if you exceed ~12 elements, create area-specific sub-diagrams
  (e.g., `frontend/c2-container.md`, `backend/c2-container.md`)
- **Show every external dependency** from the PRD at the appropriate level
- **Match diagram type to sprint scope**: C1→`C4Context`, C2→`C4Container`, C3→`C4Component`

---

## DON'Ts

- **Don't mix abstraction levels** — no Components in a C4Container diagram, no
  Container details in a C4Context diagram
- **Don't show external system internals** — `System_Ext` is a black box; never nest
  sub-elements inside it
- **Don't model shared libraries as Containers** — a library that is imported by multiple
  services is a Component that lives inside those services, not a standalone Container
- **Don't leave relationship labels vague** — "Uses", "Calls", "Depends on" are not
  acceptable as final labels
- **Don't omit the `title`** — diagrams without titles are ambiguous in documentation
- **Don't create C3 diagrams for every container** — only create them where the PRD or
  design complexity justifies decomposition
- **Don't use `%%{init}%%` directives** — GitHub's Mermaid renderer ignores them
- **Don't use `-->` or `->` arrows** in C4 blocks — use `Rel()` macros only
- **Don't crowd a diagram with 15+ elements** without sub-boundaries to group them
- **Don't create Level 4 code diagrams** unless specifically required by the PRD and
  you can commit to keeping them current

---

## Common Antipatterns

These are named failures to check for and avoid:

1. **Context Bleed** — Person or external System nodes appear in a C2 Container diagram
   but have no `Rel` connecting them to any Container. Either connect them or remove them.

2. **Phantom Container** — a Container or Component has no inbound or outbound `Rel`.
   Every node must participate in at least one relationship or it has no reason to exist
   in the diagram.

3. **External Internals** — a `System_Ext` or `Container_Ext` node has child elements
   defined inside it, exposing internals of a system you don't own. Model the boundary
   only; never model what's inside it.

4. **Library-as-Container** — a shared utility package, SDK, or library is modeled as
   a Container. Libraries are not independently deployed; they are Components that appear
   inside multiple Containers.

5. **God Container** — a single Container called "API", "Backend", or "Service" has 10+
   relationships going to/from it. This signals that the container should be split into
   multiple focused containers or the diagram needs area-specific sub-diagrams.

6. **Missing Technology** — a Container or Component has an empty, generic, or placeholder
   tech string: `""`, `"various"`, `"TBD"`, `"unknown"`. Every container must have a
   specific technology choice from the PRD.

7. **Relationship Fog** — every `Rel` in the diagram uses the same label: "Uses", "Calls",
   or "Depends on". This tells readers nothing. Each relationship needs a specific action
   verb describing what actually flows.

8. **Level Mixing** — Component-level nodes appear in a C4Context or C4Container diagram
   without being wrapped in a `Container_Boundary`. Elements must match the diagram's
   abstraction level.

9. **Overcrowded Diagram** — a single diagram has more than ~15 elements with no
   sub-boundaries to group them. Split into multiple focused diagrams, one per
   subsystem or area.

10. **Title Omission** — the diagram has no `title` statement. Without a title, it is
    impossible to tell at a glance which system and level the diagram represents.
