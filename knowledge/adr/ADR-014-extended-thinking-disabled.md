# ADR-014: Extended Thinking Disabled for All Agent Calls

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

Claude models support "extended thinking" mode, which produces more thorough reasoning at the cost of significantly higher latency and token usage. The harness makes many LLM calls across 7 sprints and multiple rounds.

## Decision

Extended thinking is disabled for all agent subprocess calls by passing:
```
settings='{"alwaysThinkingEnabled": false}'
```
to `make_agent_options()`.

## Rationale

- **Cost and latency:** In an adversarial loop with 2-4 rounds per sprint × 7 sprints, extended thinking would multiply cost and latency by a large factor. A single run could become prohibitively expensive.
- **Quality is maintained by the loop:** The adversarial structure (generator + critic iterating) achieves high quality through multiple passes and structured feedback, not through single-call deep reasoning.
- **Prompts are well-engineered:** The generator and critic prompts are detailed enough that standard reasoning is sufficient.

## Consequences

- All agent calls use standard (non-extended) thinking.
- If quality issues arise that could benefit from deeper reasoning, the right fix is improving the prompts or adding sprint iterations, not enabling extended thinking globally.
- The setting is passed at the `ClaudeAgentOptions` level, so it cannot be overridden per-call without modifying `make_agent_options()`.

**Files:** `deep_architect/agents/client.py:288-292`
