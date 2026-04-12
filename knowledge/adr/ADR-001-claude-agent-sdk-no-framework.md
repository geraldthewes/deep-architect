# ADR-001: Use Claude Agent SDK with No Agent Framework

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The harness needs an LLM agent that can call tools (Read, Write, Edit, Bash, Glob, Grep) in a real loop to write architecture files to disk. Several options were available: LangGraph, LangChain, CrewAI, or direct SDK usage.

## Decision

Use `claude-agent-sdk` (Python import: `claude_agent_sdk`) — which wraps the Claude Code CLI in a subprocess agentic loop — with no additional agent framework. Orchestration is plain async Python in `harness.py`.

## Rationale

- The generator must write files to disk using the `Write` tool; this requires a real tool-use loop, not just text generation.
- LangGraph/LangChain/CrewAI add framework boilerplate, complex state management, and lock-in without adding value here.
- The claude-agent-sdk provides exactly what is needed: an async generator of messages with tool results, a `ResultMessage` at the end, and optional structured output.
- Plain async Python is easier to test, debug, and understand than a framework abstraction layer.

## Consequences

- No framework boilerplate; the loop logic in `harness.py` is explicit and readable.
- Each agent call spawns a subprocess (the CLI), which is slightly slower than an in-process call but provides process isolation and correct env var handling.
- Tests are straightforward — no framework mocking needed.
- **Constraint:** Do not add LangGraph, LangChain, or any agent framework (enforced in CLAUDE.md).

**Files:** `deep_architect/agents/client.py`, `deep_architect/harness.py`
