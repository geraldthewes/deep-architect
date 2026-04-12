# ADR-022: Per-Agent Timeout and Graceful SIGTERM Handling

**Status:** Accepted  
**Date:** 2026-04-12  
**Deciders:** Project design

---

## Context

When a laptop hibernates mid-run, the OS suspends the Python process. On wake, the `claude` CLI
subprocess resumes with a stale TCP connection to the Anthropic API. The `async for message in
query(...)` loop in `run_agent()` has no deadline, so it hangs indefinitely — there is no timeout
at the agent call level, only a soft wall-clock check between rounds (`timeout_hours` in
`ThresholdConfig`). Additionally, SIGTERM (sent by systemd, container orchestrators, or OS
hibernate handlers) causes an unclean exit with no progress checkpoint and no user-facing message.

## Decision

**1. Per-attempt timeout on agent calls (`AgentConfig.agent_timeout_seconds`)**

Extract the `async for message in query(...)` body into a private coroutine `_consume_query()`,
then wrap each attempt in `asyncio.wait_for(_consume_query(...), timeout=timeout_seconds)`. A
`TimeoutError` is caught specifically, logged with a hibernation hint, and handled identically to
any other failure: the session is cleared and the attempt is retried. Each attempt gets its own
fresh timeout window, so recovery after a network interruption works on the next attempt.

Defaults: generator = 3600 s (60 min), critic = 1800 s (30 min). Set to `null` in TOML to disable.

**2. SIGTERM → SIGINT redirect in `cli.py`**

A `signal.signal(signal.SIGTERM, ...)` handler installed before `asyncio.run()` re-sends SIGINT
to the current process. `asyncio.run()` already installs its own SIGINT handler that cancels all
running tasks cleanly; this redirect means SIGTERM is handled identically. A `try/except
KeyboardInterrupt` wrapper catches the resulting interrupt and prints a `--resume` hint.

## Rationale

- **Why `asyncio.wait_for()` on `_consume_query()` rather than wrapping at the harness level?**  
  Every agentic call flows through `run_agent()`. Centralising the timeout there means all call
  sites (generator, critic, final-agreement) are protected without changes at each one. The
  extraction into `_consume_query()` is required because `asyncio.wait_for()` requires a coroutine,
  not an async generator.

- **Why redirect SIGTERM to SIGINT rather than a custom handler?**  
  `asyncio.run()` already handles SIGINT correctly: it cancels tasks, waits for cleanup, and
  propagates `KeyboardInterrupt`. Re-implementing that logic in a SIGTERM handler would be
  duplicative and error-prone. The redirect is two lines and reuses the existing mechanism.

- **Why not add a `CancelledError` handler in `harness.py`?**  
  Progress is already saved atomically at the start of every round (via `save_progress()`) and
  after each sprint boundary. The window where progress is not saved is small (within a single
  agent call). On `--resume`, the harness re-runs the incomplete round from the beginning, which
  is correct and safe. Adding a `CancelledError` handler would require indenting the entire sprint
  loop and adds complexity for minimal benefit.

- **Why separate defaults for generator and critic?**  
  The generator has `max_turns=50` and writes files; the critic has `max_turns=30` and is
  read-only. They have structurally different workloads. Giving each a separate default allows
  users to tune them independently in TOML.

## Consequences

- Stale network connections (e.g. after hibernation or proxy restart) are detected within
  `agent_timeout_seconds` and retried automatically with a fresh session.
- A `kill` followed by `--resume` is the recovery path for any hung run.
- SIGTERM now exits cleanly with a user-facing message instead of an abrupt traceback.
- Seven new tests in `tests/test_client_timeout.py` cover timeout triggering retry, clearing
  resume on retry, exhausting retries, no-op when disabled, structured output timeout, and
  default config values.

**Files:** `deep_architect/agents/client.py`, `deep_architect/agents/generator.py`,
`deep_architect/agents/critic.py`, `deep_architect/cli.py`, `deep_architect/config.py`,
`tests/test_client_timeout.py`
