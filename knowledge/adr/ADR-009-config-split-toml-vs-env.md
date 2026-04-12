# ADR-009: TOML Config for Behavior; Environment Variables for Auth and Endpoints

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The harness needs configuration for: API authentication, LLM endpoint URL, model aliases, max turns, retry counts, and scoring thresholds. All of these could go in a single TOML file, all in environment variables, or split between them.

## Decision

**`~/.deep-architect.toml`** controls behavior:
- Model aliases (`generator.model = "sonnet"`, `critic.model = "opus"`)
- Agent settings (`max_turns`, `max_agent_retries`, `max_round_retries`, `max_rounds_per_sprint`)
- Scoring thresholds (`min_score`, `ping_pong_similarity_threshold`, `consecutive_passing_rounds`)

**Environment variables** control infrastructure:
- `ANTHROPIC_BASE_URL` — endpoint URL (LiteLLM proxy, direct API, etc.)
- `ANTHROPIC_AUTH_TOKEN` — API key / master key
- `ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL` — resolve model aliases to actual model IDs
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` — suppress telemetry

## Rationale

- **Secrets never in files:** Storing API keys in `~/.deep-architect.toml` is a security risk (checked into git accidentally, readable by other processes). Environment variables are the standard (12-factor app principle).
- **Infrastructure config is environment-specific:** The endpoint URL changes between dev/staging/prod and between users (on-prem vs. cloud). Environment variables are the right place for this.
- **Behavior config is user-specific:** Scoring thresholds and model choices are per-user preferences, best expressed in a readable config file that can be version-controlled.
- **TOML over YAML:** TOML is simpler, has no indentation pitfalls, and is well-suited for flat config files.

## Consequences

- `load_config()` raises `FileNotFoundError` with a clear message if `~/.deep-architect.toml` is missing.
- `resolve_model_id("sonnet")` looks up `ANTHROPIC_DEFAULT_SONNET_MODEL` env var; if unset, returns `"sonnet"` unchanged (so full model IDs work directly in TOML).
- Users must set environment variables separately (e.g., in `.bashrc`, `direnv`, or a Nomad job spec).
- The `.deep-architect.toml.template` documents all TOML keys but never includes auth/endpoint fields.

**Files:** `deep_architect/config.py`, `deep_architect/agents/client.py:68-94`, `.deep-architect.toml.template`
