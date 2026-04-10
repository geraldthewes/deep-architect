# ADR-005: Two LLM Call Patterns — Pydantic-AI for Contracts, SDK for File Operations

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The harness makes several different types of LLM calls:
1. Contract proposal (Generator proposes acceptance criteria for a sprint)
2. Contract review (Critic validates the proposed contract)
3. File generation (Generator writes architecture files to disk)
4. File critique (Critic inspects and scores files, returns structured feedback)
5. Ping-pong detection (Check if feedback has stagnated)

These operations have very different requirements for tool use and structured output.

## Decision

- **No-tool, structured output calls** (contracts, ping-pong): Use `pydantic-ai` directly via `run_simple_structured()`. No subprocess spawned.
- **Tool-using agent calls** (file generation, file critique): Use `claude-agent-sdk` via `run_agent()` / `run_agent_structured()`. Spawns the Claude CLI subprocess.

## Rationale

- Contract negotiation is a pure JSON operation with a known schema (`SprintContract`, `CriticResult`). No file I/O is needed. Using `pydantic-ai` directly avoids subprocess overhead and is significantly cheaper and faster.
- File generation requires real tool use (`Write`, `Edit`, etc.). Only the agent SDK provides this via a multi-turn loop.
- File critique requires both tool use (to inspect files) and structured output (to return scored feedback). The agent SDK supports `output_format` with a JSON schema for this case.
- Mixing frameworks for different purposes is justified when the use cases are genuinely distinct — not mixing for variety but for optimization.

## Consequences

- Three distinct code paths in `client.py`:
  1. `run_simple_structured()` — pydantic-ai, no tools
  2. `run_agent_text()` — SDK, tools, text result
  3. `run_agent_structured()` — SDK, tools, JSON schema result
- Cost-optimized: simple structured calls are cheaper; agent calls are only used when tools are needed.
- Do not use `result.data`, `result.output`, or `result_type=` — these are old pydantic-ai API. Use the current pydantic-ai API.

**Files:** `deep_researcher/agents/client.py:97-138,296-490`, `deep_researcher/agents/generator.py`, `deep_researcher/agents/critic.py`
