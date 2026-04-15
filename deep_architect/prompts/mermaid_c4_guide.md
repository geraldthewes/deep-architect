# Mermaid Flowchart Reference Guide

This reference defines the flowchart conventions for all C4 architecture diagrams.
Use `flowchart` diagrams exclusively — do **NOT** use `C4Context`, `C4Container`,
`C4Component`, or any other C4 Mermaid block types. Those have inconsistent tooling
support and frequently fail to render in VS Code and common Markdown viewers.

## Diagram Type

Always open with `flowchart LR` (left-to-right) for context and container diagrams.
Use `flowchart TD` (top-down) when a vertical flow better fits the content (e.g.,
request-response chains, database tiers).

```
flowchart LR
    ...
```

---

## Title

Add a title via YAML frontmatter before the `flowchart` keyword:

```
---
title: C1 System Context for Payment Platform
---
flowchart LR
    ...
```

This renders in VS Code, GitHub, and all tools that support standard Mermaid.

---

## Node Shape Conventions

Use these shapes consistently to convey element type:

| C4 concept | Mermaid shape | Syntax example |
|---|---|---|
| Person / Actor | Rounded stadium | `user(["End User\n(Actor)"])` |
| Internal system / container / component | Rectangle | `api["API Server\n(Go, Docker)"]` |
| External system / service | Subroutine | `stripe[["Stripe\n(External)"]]` |
| Database / data store | Cylinder | `db[("PostgreSQL\n(Primary DB)")]` |
| Queue / message bus | Flag | `q>"Job Queue\n(Redis Streams)"]` |
| Boundary / zone | Subgraph | `subgraph sys["System Name"]` ... `end` |

---

## Label Rules

- All node labels use **double quotes** inside the shape brackets: `alias["text"]`
- Use `\n` for line breaks inside labels — **never** use `<br>`, `&lt;br&gt;`, or any HTML tag
- Use parentheses to annotate element type or technology: `"API Server\n(Go, Docker)"`
- The only character that needs escaping inside a label: `"` → `#quot;`
- Alphanumeric characters, spaces, `-`, `_`, `.`, `,`, `!`, `?`, `/`, `@`, `:`, `(`, `)` are safe

**Correct**:
```
api["API Server\n(Go, Docker)"]
db[("User Database\n(PostgreSQL 15)")]
stripe[["Stripe\n(External Payment)"]]
```

**Wrong — do not do this**:
```
api["API Server<br>(Go, Docker)"]        %% HTML tag — will fail to render
api["API Server&lt;br&gt;(Go)"]         %% HTML entity — also wrong
```

---

## Boundaries (Subgraphs)

Use `subgraph` to group related nodes. This replaces `System_Boundary`, `Container_Boundary`,
and `Enterprise_Boundary` from C4 macro syntax.

```
subgraph sys["Payment Platform"]
    web["Web App\n(React/Next.js)"]
    api["API Server\n(Go)"]
    db[("PostgreSQL\n(Primary DB)")]
end
```

**Rules**:
- Define nodes **inside** the `subgraph ... end` block, not before it
- Nodes defined outside any subgraph are treated as external elements
- Nest subgraphs only when you need to show sub-boundaries (e.g., DMZ within VPC)

---

## Relationships (Edges)

Use labeled directional arrows:

```
api -->|"Reads/Writes via SQL"| db
user -->|"Uses via HTTPS"| web
api -->|"Charges card via REST"| stripe
```

- Use `-->|"label"|` for directed relationships
- Include the technology/protocol in the label: `"Calls via REST/HTTPS"`, `"Publishes via AMQP"`
- Avoid vague labels — "Uses", "Calls", "Depends on" are not acceptable as final labels
- Use `<-->` for genuinely bidirectional flows (e.g., WebSockets); otherwise use two directed edges

---

## Complete Examples

### C1 System Context

```
---
title: C1 System Context for Payment Platform
---
flowchart LR
    user(["Mobile Customer\n(Actor)"])
    ops(["Operations Admin\n(Actor)"])

    sys["Payment Platform\n(Internal System)"]

    stripe[["Stripe\n(External)"]]
    sendgrid[["SendGrid\n(External)"]]

    user -->|"Submits payments via HTTPS"| sys
    ops -->|"Manages via HTTPS"| sys
    sys -->|"Processes charges via REST"| stripe
    sys -->|"Sends receipts via SMTP"| sendgrid
```

### C2 Container Diagram

```
---
title: C2 Container Diagram for Payment Platform
---
flowchart LR
    user(["Mobile Customer\n(Actor)"])

    subgraph sys["Payment Platform"]
        web["Web App\n(React/Next.js, Docker)"]
        api["API Server\n(Go, Docker)"]
        worker["Background Worker\n(Go, Docker)"]
        db[("PostgreSQL\n(Primary DB)")]
        cache["Redis Cache\n(Redis 7)"]
    end

    stripe[["Stripe\n(External)"]]

    user -->|"Uses via HTTPS"| web
    web -->|"Calls via REST/HTTPS"| api
    api -->|"Reads/Writes via SQL"| db
    api -->|"Reads/Writes via Redis protocol"| cache
    api -->|"Enqueues jobs via Redis Streams"| worker
    worker -->|"Charges cards via REST"| stripe
```

---

## Structured Error Recovery Procedure

When `mmdc -i <file> -o /tmp/validate.svg` exits non-zero, follow these steps:

### Step 1 — Capture the full error
```bash
mmdc -i <absolute-file-path> -o /tmp/validate.svg 2>&1 | head -40
```

### Step 2 — Read the error output
The error names a line number and the unexpected token:
```
Error: Parse error on line 8:
...api["API Server<br>(Go)"]
--------------------------^
Expecting 'SQE', got 'INVALID'
```

### Step 3 — Read that line in the file
Use the Read tool to view the file at the reported line number.

### Step 4 — Identify the failure cause (check in order)
1. **HTML in label** — Does the label contain `<br>`, `<span>`, or any HTML tag? Replace with `\n`
2. **Unclosed subgraph** — Is there a `subgraph` block without a matching `end`? Add it.
3. **Wrong quote style** — Does a label use single quotes instead of double quotes? Fix to double.
4. **Unescaped quote** — Does a label string contain a literal `"`? Replace with `#quot;`
5. **Bad arrow syntax** — Does the file contain `->` (single dash)? Fix to `-->`
6. **Mismatched brackets** — Does a node have `["text"` without the closing `]`? Fix it.

### Step 5 — Fix the one reported line only
Use the Edit tool to change only the specific line. Do NOT rewrite the whole diagram.

### Step 6 — Re-validate
```bash
mmdc -i <absolute-file-path> -o /tmp/validate.svg 2>&1 | head -40
```
If it exits non-zero again, repeat from Step 2 with the new error. If it exits 0, continue.

---

## Pre-writing Checklist

Run through this before writing the final diagram to a file:

- [ ] Opens with `flowchart LR` or `flowchart TD` — NOT `C4Context`, `C4Container`, or `C4Component`
- [ ] Title is set via YAML frontmatter (`---` / `title: ...` / `---`)
- [ ] All labels use double quotes and `\n` for line breaks (no `<br>` or HTML entities)
- [ ] All nodes inside a `subgraph` are defined within `subgraph ... end`, not before it
- [ ] Every relationship has a descriptive label including technology/protocol
- [ ] External systems use the subroutine shape `[["Name\n(External)"]]`
- [ ] Databases use the cylinder shape `[("Name\n(Tech)")]`
- [ ] Persons/actors use the stadium shape `(["Name\n(Role)"])`
- [ ] No orphan nodes — every node has at least one edge
