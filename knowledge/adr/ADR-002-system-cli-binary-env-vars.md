# ADR-002: Resolve Claude CLI from System PATH; Auth/Endpoint via Environment Variables

**Status:** Accepted  
**Date:** 2026-04-10  
**Deciders:** Project design

---

## Context

The `claude-agent-sdk` ships a bundled CLI binary. The harness needs to use custom LLM endpoints (LiteLLM proxy at `ANTHROPIC_BASE_URL`) and requires environment variable passthrough for auth tokens and model aliases.

## Decision

Always resolve the `claude` binary from the system PATH via `shutil.which("claude")`. Never rely on the bundled SDK binary. Auth, endpoint URL, and model aliases are configured entirely via environment variables — not in the TOML config file.

Relevant environment variables:
- `ANTHROPIC_BASE_URL` — LiteLLM or custom proxy endpoint
- `ANTHROPIC_AUTH_TOKEN` — API key / master key
- `ANTHROPIC_DEFAULT_SONNET_MODEL` — resolves `"sonnet"` alias
- `ANTHROPIC_DEFAULT_OPUS_MODEL` — resolves `"opus"` alias
- `ANTHROPIC_DEFAULT_HAIKU_MODEL` — resolves `"haiku"` alias
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` — suppress telemetry

## Rationale

- The bundled SDK binary may ignore `ANTHROPIC_BASE_URL`, breaking on-prem proxy setups.
- The system-installed CLI binary respects all environment variables.
- Storing secrets (API keys, endpoint URLs) in the TOML config file is a security anti-pattern; environment variables are the standard (12-factor app).
- Model aliases in environment variables allow the same TOML to work against different backends without editing.

## Consequences

- End users must have `claude` installed and in their PATH.
- The `cli_path` TOML config key can override this as a fallback for non-standard installations.
- If an env var alias is unset, `resolve_model_id()` passes the value through unchanged, so full model IDs also work directly in the TOML.
- The TOML config controls only behavior (model aliases, max turns, thresholds) — never infrastructure.

**Files:** `deep_architect/agents/client.py:235-248`, `deep_architect/config.py`
