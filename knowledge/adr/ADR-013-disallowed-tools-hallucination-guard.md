# ADR-013: Disallowed Tools List as Hallucination Guard

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The Claude CLI has many tools available (Agent, TaskCreate, TodoWrite, WebSearch, etc.). Even though the harness specifies `allowed_tools`, LLMs sometimes hallucinate tool calls for tools they know about but are not in their allowed list.

## Decision

In addition to `allowed_tools`, the harness passes a `disallowed_tools` list to every agent call. This list explicitly names tools that should never be used:

`["Agent", "ExitPlanMode", "EnterPlanMode", "TaskCreate", "TaskUpdate", "TaskList", "TodoWrite", "WebFetch", "WebSearch", "NotebookEdit"]`

## Rationale

- **Defense in depth:** `allowed_tools` is a whitelist; `disallowed_tools` is a blacklist. Both layers together prevent the model from calling real-but-inappropriate tools.
- **Fast failure:** If the model calls a disallowed tool, the CLI rejects it immediately and triggers a retry rather than silently allowing it.
- **Real risk:** Without this, the generator could call `WebSearch` to look up external resources, or `TaskCreate` to spawn sub-tasks — neither of which is desired in a tightly controlled architecture generation loop.

## Consequences

- When the model calls a disallowed tool, the agent call raises an error, triggering the agent retry logic (ADR-015).
- `DISALLOWED_TOOLS` is a module-level constant in `client.py`, passed to all `make_agent_options()` calls.
- Tests verify that `disallowed_tools` is present in generated agent options.
- The list should be updated if new Claude CLI tools are added that could be harmful in this context.

**Files:** `deep_architect/agents/client.py:38-54,280`, `tests/test_client.py:216-237`
