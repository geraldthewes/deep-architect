# ADR-028: XDG Base Directory Config Path

**Status:** Accepted
**Date:** 2026-07-09
**Deciders:** Project design
**Supersedes:** —
**Related:** ADR-009 (TOML config vs. env split)

---

## Context

`load_config()` resolved its default path as `~/.deep-architect.toml` — a flat dotfile directly in `$HOME`. That doesn't follow the XDG Base Directory Specification, the de facto Linux convention for user config files (`$XDG_CONFIG_HOME/<app-name>/config.toml`, defaulting to `~/.config/<app-name>/config.toml`). ADR-009 established the TOML-vs-env split but didn't scrutinize the path itself.

## Decision

Move the default config path to `~/.config/deep-architect/config.toml` (respecting `$XDG_CONFIG_HOME` when set), with a **backward-compatible fallback** to the legacy `~/.deep-architect.toml` path:

1. `$XDG_CONFIG_HOME/deep-architect/config.toml` if set and the file exists
2. `~/.config/deep-architect/config.toml` if it exists
3. `~/.deep-architect.toml` if it exists (legacy — a deprecation warning is logged pointing at the new path)
4. None exist → `FileNotFoundError` naming the new XDG path as the one to create

An explicit `--config <path>` flag (in both `adversarial-architect` and `review-action`) always wins and skips this resolution entirely — unchanged from before.

There is no scheduled removal of the legacy path; it remains supported indefinitely, just deprecated in messaging.

## Rationale

- **Convention compliance:** `~/.config/<app>/` is what users and tooling (dotfile managers, backup scripts) expect on Linux.
- **No forced migration:** existing users with `~/.deep-architect.toml` are not broken by this change — their file keeps working, with a nudge in the logs.
- **Single source of truth:** the resolution logic lives only in `deep_architect/config.py`; `review_action_harness.py` previously duplicated the old default-path line and now calls the shared resolver instead.

## Consequences

- `.deep-architect.toml.template` renamed to `config.toml.template`; its header comment now points at `~/.config/deep-architect/config.toml`.
- `load_config()`'s `FileNotFoundError` message and both CLIs' `--config` help text now reference the new path.
- New test coverage in `tests/test_config.py` for the resolution order (XDG env, `~/.config` fallback, legacy fallback with deprecation log, not-found message) — the default-path branch had no prior coverage.
- ADR-009's `~/.deep-architect.toml` references describe the TOML-vs-env split, which is unaffected by this change; the path itself is superseded by this ADR.

**Files:** `deep_architect/config.py`, `deep_architect/cli.py`, `deep_architect/review_action_harness.py`, `config.toml.template`, `tests/test_config.py`
