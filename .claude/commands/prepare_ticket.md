---
description: Gather requirements and context before technical research phase
model: sonnet
---

# Prepare Ticket for Planning

You are tasked with helping the user prepare a ticket with business requirements and context BEFORE the technical research phase. This is the first step in the workflow:

**Workflow**: `prepare_ticket` → `create_plan_generic` → `implement_plan`

Your goal is to gather high-level requirements, scope, and context that will guide the subsequent research and planning phases. Do NOT perform deep technical analysis here - that's for `create_plan_generic`.

## Your Role

- Gather business requirements and user needs
- Identify reference implementations to research later
- Define scope boundaries clearly
- Document constraints and acceptance criteria
- Prepare "research questions" for the planning phase

You are collecting WHAT to build and WHY, not yet HOW to build it.

## Ticket ID Format

Tickets should follow the project's naming convention:
- **Format**: `[PROJECT-PREFIX]-[NUMBER]`
- **Examples**: `PROJ-0042`, `FEAT-0123`, `BUG-0001`
- **Common prefixes**:
  - `PROJ-` - General project tickets
  - Custom prefixes can be defined per project

## Ticket File Format

When creating a new ticket file, use this exact format:

```markdown
---
id: [TICKET-ID]
title: [Title]
status: spec
ticket_type: feat
priority: medium
created_at: [ISO 8601 timestamp]
updated_at: [ISO 8601 timestamp]
---

# [Title]

> **Workflow Status**: Requirements gathering in progress...

[Content sections as gathered during the preparation process]
```

## Ticket Type (Conventional Commits)

The `ticket_type` field classifies the type of work using the Conventional Commits taxonomy:

| Type | Description | Auto-infer keywords |
|------|-------------|---------------------|
| `feat` | New feature | "add", "create", "implement", "introduce", "new" |
| `fix` | Bug fix | "fix", "bug", "patch", "resolve", "correct" |
| `ci` | CI/CD changes | "ci", "pipeline", "deploy", "workflow", "github action" |
| `docs` | Documentation | "doc", "readme", "guide", "wiki", "comment" |
| `refactor` | Code refactoring | "refactor", "restructure", "reorganize", "clean up", "simplify" |
| `test` | Tests | "test", "spec", "coverage", "assert" |
| `chore` | Maintenance | "chore", "update dep", "bump", "upgrade", "maintenance", "config" |

**Default**: `feat` (when no keyword match from title)

Auto-infer by case-insensitive match of the title against the keywords above. If multiple types match, prefer the first match in table order. Always confirm the inferred type with the user.

## Initial Response

When this command is invoked:

1. **Parse command parameters**:
   - If a ticket file path was provided (e.g., `knowledge/tickets/PROJ-0007.md`):
     - Check if the file exists
     - If exists: Read it immediately and continue with preparation
     - If doesn't exist: Create a new file with the ticket format above
   - If a summary/description is provided (e.g., `/prepare_ticket Add new feature X`):
     - Extract the summary text after the command
     - Use this as the ticket title
     - Auto-infer `ticket_type` from the title using the keyword table in the Ticket Type section
     - Automatically determine next ticket number (Step 1a below)
   - If no parameters provided:
     - Automatically determine next ticket number (Step 1a below)
     - Ask the user for the title: "What's the ticket title? (Brief, descriptive summary)"
     - After receiving the title, auto-infer `ticket_type` from it

1a. **Auto-determine next ticket number**:
   ```bash
   # Find the highest existing ticket number
   ls -1 knowledge/tickets/ 2>/dev/null | grep -E 'PROJ-[0-9]+\.md' | sort -V | tail -1
   ```
   - Extract the number from the last ticket (e.g., PROJ-0040 → 40)
   - Increment by 1 (40 + 1 = 41)
   - Format as PROJ-XXXX (e.g., PROJ-0041)
   - If no tickets exist, start with PROJ-0001

2. **Verify the knowledge/tickets/ directory exists**:
   - If it doesn't exist, create it first
   - Use Bash: `mkdir -p knowledge/tickets`

3. **Confirm ticket creation and ticket type**:
   ```
   Creating new ticket: [TICKET-ID]
   Title: [summary from prompt or user-provided]
   Ticket type: [auto-inferred type] ([description from table])

   Conventional Commits types:
     feat     - New feature
     fix      - Bug fix
     ci       - CI/CD changes
     docs     - Documentation
     refactor - Code refactoring
     test     - Tests
     chore    - Maintenance

   Is this ticket type correct? (Press Enter to confirm, or type a different type)
   ```

   Wait for user response. If the user provides a different type, use that instead.

4. **Create the ticket file with proper format**:
   - Use Write tool to create the file with YAML frontmatter
   - Set current timestamp for created_at and updated_at
   - Initialize with title and workflow status
   - Set status to `spec` (ready for specification)
   - Set `ticket_type` to the user-confirmed value from Step 3

5. **Present the preparation checklist**:
```
I'll help you prepare this ticket with requirements and context. This is the FIRST STEP before technical research and planning.

Current ticket: [ticket-id]
Title: [ticket-title]
Ticket type: [ticket-type] ([description])

This is a requirements gathering session. I'll collect:
- Business context and user needs
- High-level scope and boundaries
- Reference implementations to study later
- Success criteria

After this, you'll run `/create_plan_generic` to do the technical research and detailed planning.

## Requirements Checklist

### 1. Problem & Business Context
- [ ] What problem are we solving?
- [ ] Why is this important?
- [ ] Who needs this?
- [ ] What's the business value?

### 2. Reference Implementation (To Research Later)
- [ ] Is there an existing implementation to model after?
- [ ] Where is it located? (e.g., apps/example-app, packages/feature-x)
- [ ] What aspects should we mimic?

### 3. Scope & Requirements
- [ ] What are the must-have features?
- [ ] What are the nice-to-have features?
- [ ] What's explicitly out of scope?

### 4. Constraints & Context
- [ ] What systems does this integrate with?
- [ ] Are there technical constraints? (performance, security, etc.)
- [ ] Preferred technologies/frameworks?

### 5. Success Criteria
- [ ] What does "done" look like?
- [ ] How will we verify it works?
- [ ] What are the key acceptance criteria?

### 6. Research Questions
- [ ] What do we need to research in the planning phase?
- [ ] What technical unknowns need investigation?

Let's start with the business context. What problem are we solving?
```

## Workflow for New vs Existing Tickets

### Creating a New Ticket

When creating a new ticket from scratch:

1. **Ask for ticket ID**:
   ```
   What ticket number should I create? (e.g., PROJ-0042)
   Or provide the full path to an existing ticket file.
   ```

2. **Verify/create directory**:
   ```bash
   mkdir -p knowledge/tickets
   ```

3. **Ask for title**:
   ```
   What's the ticket title? (Brief, descriptive summary)
   ```

4. **Create initial file**:
   ```bash
   # Get current timestamp in ISO 8601 format
   timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
   ```

   Use Write tool to create `knowledge/tickets/[TICKET-ID].md` with:
   ```markdown
   ---
   id: [TICKET-ID]
   title: [User-provided title]
   status: spec
   ticket_type: [confirmed ticket type]
   priority: medium
   created_at: [timestamp]
   updated_at: [timestamp]
   ---

   # [User-provided title]

   > **Workflow Status**: Requirements gathering in progress...

   [Sections will be filled in during the preparation process]
   ```

5. **Proceed to requirements gathering** (Step 1 below)

### Updating an Existing Ticket

When the user provides an existing ticket file path:

1. **Read the existing file** to understand current state
2. **Proceed to requirements gathering** (Step 1 below)
3. **Update sections** using Edit tool as information is gathered

## Process Steps

### Step 1: Gather Problem Context

Ask the user about the problem statement:

```
## 1. Problem Statement & Context

Let me understand what we're building:

a) What problem are we solving?
   [Wait for user response]

b) Why is this needed? What's the business value?
   [Wait for user response]

c) Who is the user or stakeholder for this feature?
   [Wait for user response]
```

After gathering responses, summarize and confirm understanding before proceeding.

### Step 2: Identify Reference Implementation

Note the reference for later research (don't analyze deeply yet):

```
## 2. Reference Implementation (For Later Research)

a) Is there an existing implementation we should model after?
   [Wait for user response]

b) Where is it located?
   Example: "apps/feature-name" or "packages/module-x" or "services/backend-api"
   [Wait for user response]

c) What specific aspects should we mimic from it?
   - Overall architecture/structure?
   - Specific patterns or conventions?
   - Integration approach?
   - API design patterns?
   - Testing strategy?
   - Configuration management?
   [Wait for user response]

Note: We'll do detailed analysis of the reference implementation during the `/create_plan_generic` research phase.
```

**IMPORTANT**: Do NOT analyze the reference implementation deeply here. Just note its location and what aspects to mimic. The deep technical analysis will happen in `create_plan_generic`.

### Step 3: Define Requirements & Scope

Get specific about what to build:

```
## 3. Requirements & Scope

Let's define what we're building:

a) What are the MUST-HAVE features? (Core requirements)
   [Wait for user response]

b) What are the NICE-TO-HAVE features? (If time permits)
   [Wait for user response]

c) What is explicitly OUT OF SCOPE? (To prevent scope creep)
   [Wait for user response]

Example format:
- Must-have: Core feature functionality with basic validation
- Nice-to-have: Advanced analytics and reporting
- Out of scope: Mobile app support, third-party integrations
```

### Step 4: Constraints & Context

Gather high-level technical context (not deep technical details):

```
## 4. Constraints & Context

a) What systems does this integrate with?
   [Wait for user response]

b) Are there preferred technologies or frameworks?
   [Wait for user response]

c) Any important constraints?
   - Performance requirements?
   - Security considerations?
   - Platform/compatibility requirements?
   [Wait for user response]

Note: Detailed technical analysis will happen during the planning phase.
```

### Step 5: Define Acceptance Criteria

Make success measurable:

```
## 5. Acceptance Criteria

How will we verify this is complete?

a) What should the end state look like?
   [Wait for user response]

b) What are the specific verification steps?
   Example:
   - [ ] Core functionality works in production environment
   - [ ] All automated tests pass
   - [ ] Performance meets requirements (e.g., response time < 200ms)
   - [ ] Feature can handle edge cases
   - [ ] Documentation is updated
   [Wait for user response]

c) Are there any manual testing steps required?
   [Wait for user response]
```

### Step 6: Define Research Questions

Identify what needs to be investigated during planning:

```
## 6. Research Questions for Planning Phase

What technical questions should we research during `/create_plan_generic`?

Examples:
- How does the reference implementation handle [specific functionality]?
- What's the current architecture/pattern for [related features]?
- How should we integrate with [existing systems]?
- What are the existing conventions for [component type]?
- Are there performance/scalability implications to investigate?
- What libraries/frameworks are used for similar features?
- How is error handling typically done?
- What testing approaches are used for similar components?

[Wait for user response]

Note: These questions will guide the research tasks during plan creation.
```

### Step 7: Gather Related Resources

Collect all relevant links and documentation:

```
## 7. Related Resources

Any related resources to reference?

a) Related tickets or issues?
   [Wait for user response]

b) Research documents or design docs?
   [Wait for user response]

c) External documentation or specifications?
   [Wait for user response]
```

### Step 8: Write Requirements Document

After gathering all information:

1. **Summarize all collected information**:
```
Based on our discussion, here's the requirements summary:

## Problem & Business Context
[What we're solving and why]

## Reference Implementation (To Research)
**Location**: [path/to/reference]
**Aspects to mimic**: [list]

## Requirements
**Must-have:**
- [List]

**Nice-to-have:**
- [List]

**Out of scope:**
- [List]

## Constraints & Context
[High-level constraints and integrations]

## Success Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] [Criterion 3]

## Research Questions for Planning Phase
- [Question 1]
- [Question 2]
- [Question 3]

## Related Resources
- [Links and references]

Does this capture everything? Any additions or changes?
```

2. **Wait for user confirmation**

3. **Create or update the ticket file** with the requirements:

   - If file exists: Use Edit tool to update sections
   - If new file: Use Write tool to create complete file
   - Always use ISO 8601 timestamp format (e.g., `2026-02-10T15:30:00Z`)
   - Ensure YAML frontmatter is valid and complete

```markdown
---
id: [TICKET-ID]
title: [Title]
status: spec
ticket_type: [confirmed ticket type]
priority: medium
created_at: [ISO 8601 timestamp]
updated_at: [ISO 8601 timestamp]
---

# [Title]

> **Workflow Status**: Requirements gathered ✓ → Ready for `/create_plan_generic` to research and plan

## Problem Statement & Business Context

**What problem are we solving?**
[Problem description]

**Why is this important?**
[Business value and user need]

**Who needs this?**
[User/stakeholder]

## Reference Implementation (To Research)

**Location**: `[path/to/reference]`

**Aspects to mimic**:
- [Architecture/structure]
- [Specific patterns]
- [Integration approach]

> Note: Detailed analysis of reference will happen during planning phase

## Requirements & Scope

### Must-Have Features
- [Feature 1]
- [Feature 2]
- [Feature 3]

### Nice-to-Have Features
- [Feature 1]
- [Feature 2]

### Out of Scope
- [Item 1]
- [Item 2]

## Constraints & Context

**Integration Points**: [Systems to integrate with]

**Preferred Technologies**: [Tech stack preferences]

**Constraints**:
- [Performance requirements]
- [Security considerations]
- [Platform requirements]

## Success Criteria

### Automated Verification
- [ ] [Automated test/check 1]
- [ ] [Automated test/check 2]

### Manual Verification
- [ ] [Manual test step 1]
- [ ] [Manual test step 2]

**End State**: [Description of what "done" looks like]

## Research Questions for Planning Phase

The following should be investigated during `/create_plan_generic`:
- [Technical question 1]
- [Technical question 2]
- [Technical question 3]

## Related Resources

- Related tickets: [ticket-ids]
- Research documents: [paths]
- External docs: [urls]

## Notes

[Any additional assumptions or context]
```

4. **Present next steps**:
```
✓ Ticket requirements gathered successfully!

Location: `knowledge/tickets/[TICKET-ID].md`

Next steps:
1. Review the requirements to ensure completeness
2. Run `/create_plan_generic knowledge/tickets/[TICKET-ID].md` to start the research and planning phase
3. During planning, the reference implementation will be analyzed and technical decisions will be made

Ready to proceed with planning phase?
```

## Important Guidelines

1. **Handle File Creation Properly**:
   - Always check if `knowledge/tickets/` directory exists
   - Create directory with `mkdir -p` if needed
   - Automatically determine next ticket number by checking existing tickets
   - Use summary from command prompt as title if provided (e.g., `/prepare_ticket Add feature X`)
   - If no summary provided, ask for title
   - Auto-infer `ticket_type` from the title and confirm with the user before creating the file
   - Use ISO 8601 timestamp format (e.g., `2026-02-10T15:30:00Z`)
   - Use Write tool for new files, Edit tool for updates
   - Validate YAML frontmatter is properly formatted

2. **Stay High-Level**:
   - Focus on WHAT to build, not HOW to build it
   - Gather business requirements, not technical solutions
   - Note references to research, don't analyze them yet
   - Identify research questions, don't answer them

2. **Be Interactive & Fast**:
   - Don't rush but don't over-analyze
   - This should take 10-15 minutes, not hours
   - Wait for complete responses
   - Ask clarifying questions
   - Confirm understanding at each step

3. **Avoid Deep Technical Analysis**:
   - Do NOT spawn codebase research tasks
   - Do NOT read implementation files
   - Do NOT analyze architecture patterns
   - Save all technical work for `/create_plan_generic`

4. **Be Specific About Requirements**:
   - Avoid vague requirements
   - Push for concrete acceptance criteria
   - Get measurable success metrics
   - Define clear boundaries (in-scope vs out-of-scope)

5. **Separate Concerns**:
   - Keep must-haves separate from nice-to-haves
   - Distinguish automated from manual verification
   - Separate business requirements from technical constraints
   - Note research questions separately

6. **Document Business Context**:
   - Capture the "why" not just the "what"
   - Record user needs and business value
   - Link to related resources
   - Identify what needs research

## Example Interactions

### Example 1: Creating a New Ticket with Auto-Numbering

```
User: /prepare_ticket Add async task processing system

Agent: [Checks existing tickets, finds PROJ-0040 is latest]
       [Auto-infers ticket_type from "Add" keyword → feat]

Creating new ticket: PROJ-0041
Title: Add async task processing system
Ticket type: feat (New feature)

Is this ticket type correct? (Press Enter to confirm, or type a different type)

[User confirms]

✓ Created knowledge/tickets/PROJ-0041.md

Now let's gather the requirements. First, let's understand the business context.

What problem are we solving?
[Wait for response: "Users need a way to X, but currently have to do Y manually"]

[... continues through the checklist ...]

[After gathering all info]:
I've documented the requirements in knowledge/tickets/PROJ-0041.md

Next, run `/create_plan_generic knowledge/tickets/PROJ-0041.md` to analyze the reference
implementation and create a detailed technical plan.
```

### Example 1b: Creating a New Ticket Without Summary

```
User: /prepare_ticket

Agent: [Checks existing tickets, finds PROJ-0040 is latest]

Creating new ticket: PROJ-0041

What's the ticket title? (Brief, descriptive summary)

User: Add async task processing system

Agent: [Auto-infers ticket_type from "Add" keyword → feat]

Ticket type: feat (New feature)

Is this ticket type correct? (Press Enter to confirm, or type a different type)

[User confirms]

✓ Created knowledge/tickets/PROJ-0041.md

Now let's gather the requirements. First, let's understand the business context.

What problem are we solving?
[Wait for response: "Users need a way to X, but currently have to do Y manually"]

[... continues through the checklist ...]

[After gathering all info]:
I've documented the requirements in knowledge/tickets/PROJ-0041.md

Next, run `/create_plan_generic knowledge/tickets/PROJ-0041.md` to analyze the reference
implementation and create a detailed technical plan.
```

### Example 2: Updating an Existing Ticket

```
User: /prepare_ticket knowledge/tickets/PROJ-0039.md

Agent: Reading existing ticket PROJ-0039...

I found the ticket "Tauri tray application for daemon management"
Current status: triage
Ticket type: feat (New feature)

Let me help you prepare this ticket with full requirements. Let's start with the business context.

What problem are we solving?
[Wait for response: "Users need a way to X, but currently have to do Y manually"]

Why is this important?
[Wait for response: "This manual process takes hours and is error-prone"]

Who needs this feature?
[Wait for response: "Internal team members and external API users"]

Now, about the reference implementation at apps/existing-feature:
- What aspects should we mimic? The overall architecture? The data flow? Integration patterns?
[Wait for response: "The overall architecture and how it handles async operations"]

Let's define the must-have features...
[Continue through checklist...]

[After gathering all info]:
I've documented the requirements in knowledge/tickets/PROJ-XXXX.md

Next, run `/create_plan_generic knowledge/tickets/PROJ-XXXX.md` to analyze the reference
implementation and create a detailed technical plan.
```

## Ticket Status Transitions

After preparation:
- Update ticket status: `triage` → `spec`
- Ticket is now ready for `/create_plan_generic` research phase

## Common Pitfalls to Avoid

1. **Don't skip directory creation** - Always ensure `knowledge/tickets/` exists
2. **Don't forget to ask for ticket ID/title** - Get these upfront for new tickets
3. **Don't use invalid timestamps** - Use ISO 8601 format only
4. **Don't do technical analysis** - Save it for the planning phase
5. **Don't read implementation files** - Just note their location
6. **Don't accept vague requirements** - Push for specificity
7. **Don't forget out-of-scope** - Explicitly define boundaries
8. **Don't miss acceptance criteria** - Make success measurable
9. **Don't ignore research questions** - Document what needs investigation
10. **Don't assume knowledge** - Ask questions when unclear
11. **Don't skip ticket type confirmation** - Always confirm the auto-inferred ticket type with the user

## Success Criteria for This Command

A well-prepared ticket should have:

**Business Context**:
- [ ] Clear problem statement (what and why)
- [ ] Identified user/stakeholder
- [ ] Documented business value

**Scope & Requirements**:
- [ ] Concrete must-have features
- [ ] Optional nice-to-have features
- [ ] Explicitly defined out-of-scope items
- [ ] Measurable acceptance criteria
- [ ] Ticket type identified and confirmed (conventional commits taxonomy)

**Reference & Research**:
- [ ] Reference implementation location noted (not analyzed)
- [ ] Aspects to mimic identified
- [ ] Research questions documented

**Context & Constraints**:
- [ ] Integration points identified
- [ ] Technical constraints documented
- [ ] Related resources linked

**Ready for Next Phase**:
- [ ] Ticket status updated to `spec`
- [ ] Ready for `/create_plan_generic` to begin research and planning
- [ ] No deep technical analysis done yet

Remember: This is requirements gathering, not solution design. Keep it fast and focused on business needs.
