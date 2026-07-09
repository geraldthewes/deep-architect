# Grok Coding Agent Backend for review-action Implementation Plan

## Overview

Add **Grok Build (xAI)** as a third coding-agent backend to `review-action`, alongside the
existing Claude and opencode providers. As part of this work, extract each coding agent into
its own Python module (new `deep_architect/coding_agents/` package) and make the per-agent-call
timeout configurable via `~/.deep-architect.toml` instead of hardcoded.

## Current State Analysis

All coding-agent code currently lives inside the 1,634-line `deep_architect/review_action_harness.py`:

- **`CodingAgent` Protocol** (`review_action_harness.py:101-121`) — two async methods, both
  returning `bool`: `apply_fix(file_path, existing_code, suggested_code, context, original_content)`
  and `fix_check_failures(files, failure_report, context)`.
- **`OpencodeAgent`** (`review_action_harness.py:129-452`) — shells out via `subprocess.run`
  to the opencode binary (`OPENCODE_BIN` env var, hardcoded fallback path) with
  `run --format json --dangerously-skip-permissions` plus two `--file` attachments (prompt
  template + feedback). Success = `_parse_opencode_ndjson()` (`:455-486`) finding a
  `{"type": "result", "is_error": false}` event, then `_file_reflects_fix()` disk verification.
  **Timeout is hardcoded at 120s** (`:228`, `:429`). `self.model` is stored but never passed
  to the subprocess (only logged).
- **`ClaudeSDKAgent`** (`review_action_harness.py:1270-1433`) — delegates to
  `deep_architect/agents/client.py` (`make_agent_options` + `run_agent`), `MAX_TURNS = 10`,
  **`timeout_seconds=300.0` hardcoded constructor default** (`:1289`). Its prompts explicitly
  say "Do not commit the change" (harness commits separately).
- **`create_agent()` factory** (`review_action_harness.py:1229-1238`) — the single provider
  dispatch point: `"opencode"` → `OpencodeAgent`, `"claude"` → `_create_claude_agent()`,
  else `ValueError`.
- **`AgentConfig` dataclass** (`review_action_harness.py:84-93`) — `provider: str = "opencode"`
  plus model/retry fields. *Name-collides* with the unrelated Pydantic
  `deep_architect.config.AgentConfig` (`config.py:9-14`).
- **`--provider` CLI flag** (`review_action_harness.py:1458-1463`) — `choices=["opencode", "claude"]`.
  Provider comes only from this flag (`main()` `:1580`: `args.provider or "opencode"`);
  there is no TOML provider key.
- **Model resolution in `main()`** (`:1579`): `model = args.model or harness_config.generator.model`
  — i.e. falls back to the TOML generator model (e.g. `"sonnet"`), which is meaningless to grok.
- **The fix loop is provider-agnostic**: `_process_single_finding()` (`:768-1090`) and
  `process_findings()` only call the two protocol methods. The quality-check loop, bounded by
  `thresholds.check_max_fix_iterations`, feeds failure reports back via `fix_check_failures()`
  and fail-closes with `git_restore_files()`.
- **Neither existing provider uses structured output in the fix loop** — both reduce to a
  boolean success signal plus the shared `_file_reflects_fix()` disk check (`:489-541`).
  "Parity" for Grok therefore means: agentic tool use + boolean success + disk verification.
- **Tests** (`tests/test_review_action_harness.py`) — `TestOpencodeAgent` (`:314`) mocks
  `deep_architect.review_action_harness.subprocess.run`; `TestClaudeSDKAgent` (`:467`),
  `TestCreateAgent` (`:589`), protocol-conformance tests (`:807-822`).

### Verified Grok Build CLI facts (grok 0.2.93, tested locally 2026-07-09)

- Binary at `/home/gerald/.grok/bin/grok`; installed via `curl -fsSL https://x.ai/cli/install.sh | bash`.
- Headless single-turn: `-p, --single <PROMPT>` or `--prompt-file <PATH>` (file variant of the
  same single-turn input — grok has no opencode-style `--file` attachments).
- `--output-format json` (success, exit 0) emits **one JSON object**:
  ```json
  {"text": "...", "stopReason": "EndTurn", "sessionId": "...", "requestId": "...", "thought": "..."}
  ```
- Error case: **exit code 1**, stdout gets `{"type":"error","message":"..."}`, stderr gets a
  human-readable `Error: ...` line.
- `--output-format streaming-json` emits token-level NDJSON (`{"type":"thought","data":"The"}`
  per token) — far too noisy; plain `json` is the right choice.
- `--max-turns <N>` **exists** (web docs missed it), as do `--permission-mode <MODE>` with
  `bypassPermissions` among its values, `--always-approve`, `-m/--model <MODEL>`, `--cwd <CWD>`.
- Auth: cached `grok login` session (this machine is logged in via grok.com) **or**
  `XAI_API_KEY` env var for headless/CI — a pay-as-you-go console.x.ai key; no consumer
  subscription required. Env-var-only auth matches the project convention.
- Available models on this account: `grok-composer-2.5-fast` (grok's own default), `grok-build`.
- Invalid `-m` value fails fast with the error JSON above.

### Key Discoveries:
- The only integration points for a new provider: a class implementing the two-method protocol,
  one factory branch, one `choices=` entry, README rows (`review_action_harness.py:1229`, `:1460`).
- Grok's success/error contract is simpler than opencode's NDJSON: exit code 0 + single JSON
  object vs. scanning an event stream.
- The opencode prompt template (`deep_architect/resources/prompt_template.md`) instructs the
  agent to **commit** its change; `ClaudeSDKAgent`'s prompts forbid committing. Grok must follow
  the Claude convention (no commit) — the harness owns commits, and an agent-side commit would
  break the fail-closed `git_restore_files()` logic in the quality-check loop.
- Test patch targets must move with the classes: `@patch("deep_architect.review_action_harness.subprocess.run")`
  becomes `@patch("deep_architect.coding_agents.opencode.subprocess.run")` etc. Test behavior
  and assertions stay identical; the ticket's "tests pass unmodified" criterion is amended to
  "tests pass with only import/patch-path updates, no behavioral changes" (agreed with Gerald
  during planning, as a consequence of the requested module split).

## Desired End State

- `uv run review-action feedback/ --provider grok [--model grok-build]` applies fixes through
  the Grok Build CLI with the same fix-loop behavior (bounded retries, quality-check gate,
  fail-closed restore, atomic commits) as the existing providers.
- Coding agents live in `deep_architect/coding_agents/` — one module per agent, shared pieces
  in `base.py`, dispatch in `factory.py`. `review_action_harness.py` contains only
  orchestration (finding parsing, fix loop, status persistence, CLI).
- Per-agent-call timeout is configurable via `[thresholds] coding_agent_timeout` in
  `~/.deep-architect.toml`; no timeout literals inside agent call sites.
- `ruff`, `mypy`, `pytest`, `bandit` all pass; README documents the Grok backend.

## What We're NOT Doing

- No Grok support in the generator/critic harness (sprints 1–7 loop) — `agents/client.py`,
  `harness.py` are untouched.
- No Grok support in `llm_judge.py` / quality-check judging — the judge stays on the Anthropic API.
- No `xai-sdk` dependency / direct-API integration — decided during planning: the CLI subprocess
  route wins (the SDK route would require hand-building an agentic tool loop; API-key auth for
  the CLI is verified, removing the subscription-gating concern).
- No TOML `provider` key — provider selection stays CLI-flag-only, as today.
- No behavior changes to `OpencodeAgent`/`ClaudeSDKAgent` beyond the config-driven timeout
  (which defaults to their current values).
- No fixing of pre-existing quirks (opencode's unused `self.model` in argv, its commit-instructing
  prompt template) — noted, not changed.
- No `--output-format streaming-json` parsing.

## Implementation Approach

Three phases, each independently verifiable: (1) a pure extract-move refactor establishing the
`coding_agents` package with zero behavior change, (2) the config-driven timeout threaded through
the factory, (3) the new `GrokAgent` + CLI wiring + tests + docs. Phase 1 must be fully green
before Phase 2 begins so any regression is attributable.

---

## Phase 1: Extract `coding_agents` package (pure refactor, no behavior change)

### Overview
Move the protocol, both agent classes, shared helpers, and the factory out of
`review_action_harness.py` into a new `deep_architect/coding_agents/` package. Rename the
local `AgentConfig` dataclass to `CodingAgentConfig` to kill the name collision with
`deep_architect.config.AgentConfig`.

### Changes Required:

#### 1. New package `deep_architect/coding_agents/`

**File**: `deep_architect/coding_agents/base.py`
Moved from `review_action_harness.py`, verbatim except the dataclass rename:
- `CodingAgent` Protocol (from `:101-121`)
- `AgentConfig` dataclass (from `:84-93`) → renamed `CodingAgentConfig`; field changes:
  `model: str | None = "standard/coder"` becomes `model: str | None = None` and a new
  `timeout_seconds: float | None = None` field is added **in Phase 2** (Phase 1 keeps fields
  identical apart from the rename)
- `_file_reflects_fix()` (from `:489-541`) — shared by opencode/claude/grok `apply_fix`

**File**: `deep_architect/coding_agents/opencode.py`
Moved verbatim:
- `OpencodeAgent` (from `:129-452`) including `_load_prompt_template()`
- `_parse_opencode_ndjson()` (from `:455-486`)
- module-level `logger = get_logger(__name__)`; hoist the function-local
  `import os/tempfile` statements to module level

**File**: `deep_architect/coding_agents/claude.py`
Moved verbatim:
- `ClaudeSDKAgent` (from `:1270-1433`), keeping the lazy
  `from deep_architect.agents.client import ...` imports inside the methods (they guard
  against `claude_agent_sdk` being unavailable)

**File**: `deep_architect/coding_agents/factory.py`
Moved:
- `create_agent()` (from `:1229-1238`) — parameter type becomes `CodingAgentConfig`
- `_create_claude_agent()` (from `:1241-1262`) — its internal
  `from deep_architect.review_action_harness import ClaudeSDKAgent` becomes
  `from deep_architect.coding_agents.claude import ClaudeSDKAgent`

**File**: `deep_architect/coding_agents/__init__.py`
```python
from deep_architect.coding_agents.base import CodingAgent, CodingAgentConfig
from deep_architect.coding_agents.claude import ClaudeSDKAgent
from deep_architect.coding_agents.factory import create_agent
from deep_architect.coding_agents.grok import GrokAgent  # added in Phase 3
from deep_architect.coding_agents.opencode import OpencodeAgent

__all__ = [...]
```

#### 2. Slim down the harness
**File**: `deep_architect/review_action_harness.py`
**Changes**: delete the moved code; replace with
`from deep_architect.coding_agents import CodingAgent, CodingAgentConfig, create_agent`.
`_process_single_finding`, `process_findings`, status persistence, `parse_args`, `print_summary`,
`main()` stay. `main()`'s `AgentConfig(...)` construction (`:1592-1598`) becomes
`CodingAgentConfig(...)`. Drop now-unused imports (`subprocess` stays only if still used —
it isn't after the move; remove it and the `json` import if orphaned).

#### 3. Split the tests
**File**: `tests/test_coding_agents.py` (new)
Move from `tests/test_review_action_harness.py`, assertions unchanged:
- `TestOpencodeAgent` — patch target becomes `deep_architect.coding_agents.opencode.subprocess.run`
- `TestClaudeSDKAgent` — patch targets for `make_agent_options`/`run_agent` move to
  `deep_architect.coding_agents.claude.*` equivalents (match however the current tests patch them)
- `TestFileReflectsFix`, `TestCreateAgent`, the protocol-conformance tests
- `AgentConfig(provider=...)` constructions become `CodingAgentConfig(provider=...)`

**File**: `tests/test_review_action_harness.py`
Keeps parsing/status/process-loop tests; updates imports of `OpencodeAgent`/`create_agent` to
`deep_architect.coding_agents`. The `process_findings` tests that instantiate `OpencodeAgent()`
directly keep working via the new import.

### Success Criteria:

#### Automated Verification:
- [x] `uv run ruff check deep_architect/ tests/` passes
- [x] `uv run mypy deep_architect/` passes
- [x] `uv run python -m pytest tests/ -v` passes with the **same test count** as before the move
- [x] `uv run bandit -r deep_architect/ -ll` passes
- [x] `grep -c "class OpencodeAgent\|class ClaudeSDKAgent\|def create_agent" deep_architect/review_action_harness.py` returns 0

#### Manual Verification:
- [x] `uv run review-action feedback/ --dry-run` still runs end-to-end (smoke test that the CLI wiring survived the move)

**Implementation Note**: Pause here for confirmation before Phase 2.

---

## Phase 2: Config-driven coding-agent timeout

### Overview
Replace the hardcoded 120s (opencode) / 300s (claude) timeouts with a single optional
`[thresholds] coding_agent_timeout` config key. Default `None` preserves each agent's current
default exactly, honoring "must not change existing backend behavior".

### Changes Required:

#### 1. Config key
**File**: `deep_architect/config.py`
**Changes**: add to `ThresholdConfig` (after `check_command_timeout`, `:33`):
```python
# review-action: per coding-agent call timeout in seconds
# (None = per-agent default: opencode 120, claude 300, grok 300)
coding_agent_timeout: float | None = None
```

**File**: `.deep-architect.toml.template`
**Changes**: document the new key in the `[thresholds]` section alongside
`check_max_fix_iterations` / `check_command_timeout`.

#### 2. Per-agent defaults + constructor wiring
**File**: `deep_architect/coding_agents/opencode.py`
```python
OPENCODE_DEFAULT_TIMEOUT: float = 120.0

class OpencodeAgent:
    def __init__(self, model: str = "standard/coder", opencode_bin: str | None = None,
                 timeout_seconds: float | None = None) -> None:
        ...
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else OPENCODE_DEFAULT_TIMEOUT
        )
```
Both `subprocess.run(..., timeout=120)` sites use `timeout=self.timeout_seconds`.

**File**: `deep_architect/coding_agents/claude.py`
**Changes**: `timeout_seconds: float | None = None` in `__init__`; resolve against
`CLAUDE_DEFAULT_TIMEOUT: float = 300.0` the same way (replacing the current `= 300.0` default).

#### 3. Factory + main() wiring
**File**: `deep_architect/coding_agents/base.py`
**Changes**: add `timeout_seconds: float | None = None` to `CodingAgentConfig`.

**File**: `deep_architect/coding_agents/factory.py`
**Changes**: pass `timeout_seconds=config.timeout_seconds` to every agent constructor.

**File**: `deep_architect/review_action_harness.py`
**Changes**: in `main()`, populate
`CodingAgentConfig(..., timeout_seconds=harness_config.thresholds.coding_agent_timeout)`.

#### 4. Tests
**File**: `tests/test_config.py`
- default is `None`; a TOML with `coding_agent_timeout = 240.0` parses to `240.0`

**File**: `tests/test_coding_agents.py`
- `OpencodeAgent()` default resolves to `120.0`; explicit `timeout_seconds=42.0` is passed to
  `subprocess.run` (assert via the existing mock's `call_args`)
- `ClaudeSDKAgent()` default resolves to `300.0`
- `create_agent(CodingAgentConfig(provider="opencode", timeout_seconds=42.0))` produces an
  agent with `timeout_seconds == 42.0`

### Success Criteria:

#### Automated Verification:
- [x] All four quality gates pass (`ruff`, `mypy`, `pytest`, `bandit`)
- [x] `grep -n "timeout=120" deep_architect/coding_agents/` returns nothing
- [x] New config tests pass: `uv run python -m pytest tests/test_config.py tests/test_coding_agents.py -v`

#### Manual Verification:
- [x] With no `coding_agent_timeout` in `~/.deep-architect.toml`, behavior is unchanged (defaults logged/observed) — verified via `test_default_init` unit tests confirming `OpencodeAgent()` resolves to 120.0s and `ClaudeSDKAgent()` to 300.0s when `timeout_seconds` is omitted, matching pre-Phase-2 hardcoded values.

---

## Phase 3: `GrokAgent` + CLI wiring + docs

### Overview
Add the new backend in its own module, wire the `--provider grok` choice and model pass-through,
add the test suite, and document it.

### Changes Required:

#### 1. The agent
**File**: `deep_architect/coding_agents/grok.py` (new)

```python
GROK_DEFAULT_TIMEOUT: float = 300.0
GROK_DEFAULT_MAX_TURNS: int = 10  # mirrors ClaudeSDKAgent.MAX_TURNS


class GrokAgent:
    """Grok Build (xAI CLI) implementation of CodingAgent using subprocess.

    Shells out to the grok binary in headless single-turn mode
    (--prompt-file + --output-format json) and parses the single JSON
    result object. Auth is inherited from the environment: a cached
    `grok login` session or the XAI_API_KEY env var.
    """

    def __init__(
        self,
        model: str | None = None,
        grok_bin: str | None = None,
        timeout_seconds: float | None = None,
        max_turns: int = GROK_DEFAULT_MAX_TURNS,
    ) -> None:
        self.model = model  # None → grok's own configured default model
        self.grok_bin = grok_bin or os.environ.get("GROK_BIN", "grok")
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else GROK_DEFAULT_TIMEOUT
        )
        self.max_turns = max_turns

    def _build_command(self, prompt_file: str) -> list[str]:
        cmd = [
            self.grok_bin,
            "--prompt-file", prompt_file,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--max-turns", str(self.max_turns),
        ]
        if self.model:
            cmd += ["-m", self.model]
        return cmd
```

`apply_fix()` — same structure as `OpencodeAgent.apply_fix` but with **one** temp `.md` file
(grok's `--prompt-file` is the whole single-turn prompt; there are no attachment flags).
The prompt content follows `ClaudeSDKAgent`'s wording, NOT the opencode template
(which instructs committing — see Key Discoveries):

```
You are a precise code editing assistant. Apply the following code change
exactly as specified. Do not make any other changes. Do not run git or
commit the change — that is handled separately.

**File**: {absolute_file_path}

**Existing Code**:
```...```

**Suggested Code**:
```...```

**Context**: {context}

Make the change and confirm it was applied correctly.
```

Flow: write temp file → `subprocess.run(self._build_command(prompt_file), capture_output=True,
text=True, timeout=self.timeout_seconds)` (no `cwd=`/`env=` — inherit, like opencode) →
`_parse_grok_json(result.returncode, result.stdout, result.stderr)` → on success,
`_file_reflects_fix(absolute_file_path, suggested_code, original_content)`.
Exception handling mirrors opencode exactly: `FileNotFoundError` (binary missing),
`subprocess.TimeoutExpired`, generic `Exception` — log via `logger.error` and return `False`;
`finally` unlinks the temp file.

`fix_check_failures()` — same subprocess pattern; prompt content mirrors
`ClaudeSDKAgent.fix_check_failures`'s wording (modified-files list, original review context,
failure report, "do not commit"). Success = `_parse_grok_json(...)` only — no
`_file_reflects_fix`, matching the documented asymmetry (the harness's check rerun is the
real verification).

`_parse_grok_json()` — module-level function, written against the empirically verified contract:

```python
def _parse_grok_json(returncode: int, raw_stdout: str, raw_stderr: str) -> bool:
    """Parse grok --output-format json output; True if the agent completed.

    Verified contract (grok 0.2.93):
      success → exit 0, stdout is one JSON object with text/stopReason/sessionId
      failure → exit 1, stdout is {"type": "error", "message": "..."}
    """
    if returncode != 0:
        message = "unknown error"
        try:
            event = json.loads(raw_stdout.strip() or "{}")
            if event.get("type") == "error":
                message = str(event.get("message", message))
        except json.JSONDecodeError:
            pass
        if message == "unknown error" and raw_stderr.strip():
            message = raw_stderr.strip().splitlines()[-1][:200]
        logger.error("GrokAgent: failed (returncode=%d): %s", returncode, message)
        return False
    try:
        result = json.loads(raw_stdout.strip())
        logger.debug(
            "GrokAgent: completed (stopReason=%s, sessionId=%s)",
            result.get("stopReason"), result.get("sessionId"),
        )
    except json.JSONDecodeError:
        logger.warning("GrokAgent: exit 0 but non-JSON stdout — trusting exit code")
    return True
```

#### 2. Factory + CLI wiring
**File**: `deep_architect/coding_agents/factory.py`
**Changes**: add branch:
```python
elif config.provider == "grok":
    return GrokAgent(model=config.model, timeout_seconds=config.timeout_seconds)
```

**File**: `deep_architect/review_action_harness.py`
**Changes**:
- `parse_args()`: `choices=["opencode", "claude", "grok"]`; help text mentions all three
- `main()` model resolution — grok must not inherit the TOML generator model (`"sonnet"` is not
  a grok model ID):
```python
provider = args.provider or "opencode"
if provider == "grok":
    model = args.model  # None → grok's own default model
else:
    model = args.model or harness_config.generator.model
```

**File**: `deep_architect/coding_agents/__init__.py`
**Changes**: export `GrokAgent`.

#### 3. Tests
**File**: `tests/test_coding_agents.py`
**Changes**: add `TestGrokAgent`, mirroring `TestOpencodeAgent`'s shape (patch target
`deep_architect.coding_agents.grok.subprocess.run`); mock stdout uses the **real** verified
JSON shapes:
- `test_default_init` — `grok_bin == "grok"`, `model is None`, `timeout_seconds == 300.0`
- `test_grok_bin_env_var` — `GROK_BIN` respected (via `monkeypatch.setenv`)
- `test_custom_model_in_argv` / `test_no_model_omits_flag` — assert `-m` presence/absence in
  `mock_run.call_args`
- `test_apply_fix_success` — mock returncode 0 +
  `{"text": "done", "stopReason": "EndTurn", "sessionId": "s", "requestId": "r"}`
- `test_apply_fix_error_json` — returncode 1 + `{"type": "error", "message": "boom"}` → `False`
- `test_apply_fix_timeout` / `test_apply_fix_binary_not_found` — `subprocess.TimeoutExpired` /
  `FileNotFoundError` → `False`
- `test_fix_check_failures_success` / `_failure` — same pattern, no file verification
- `test_timeout_passed_to_subprocess` — `timeout_seconds=42.0` appears in `call_args.kwargs`
- factory: `test_create_grok_agent` — `create_agent(CodingAgentConfig(provider="grok", model="grok-build"))`
  returns a `GrokAgent`
- protocol: `test_grok_agent_implements_protocol` — `isinstance(GrokAgent(), CodingAgent)`

Also add `_parse_grok_json` unit tests (success object, error object, exit-0-non-JSON,
exit-1-empty-stdout-with-stderr) — pure-function tests, no mocking needed.

**File**: `tests/test_review_action_harness.py`
**Changes**: add a `parse_args` test asserting `--provider grok` is accepted and an unknown
provider is rejected (argparse `SystemExit`).

#### 4. Documentation
**File**: `README.md`
**Changes**, matching the existing style exactly:
- `### CLI Options` table (`:505`): `--provider <name>` row description →
  ``Agent provider (`opencode`, `claude`, or `grok`)``
- `### Example` (`:515`): add
  ```bash
  # Use Grok Build (xAI) instead of opencode
  uv run review-action feedback/ --provider grok --model grok-build
  ```
- New `### Grok Build backend` subsection under `## Review Action Harness` (after
  `### Quality checks`), documenting: install
  (`curl -fsSL https://x.ai/cli/install.sh | bash`), auth (interactive `grok login` **or**
  `export XAI_API_KEY=...` for headless — env vars only, never TOML), `GROK_BIN` override
  (default: `grok` on PATH), model selection (`--model grok-build`; omitted → grok's own
  configured default), and that the timeout is governed by `[thresholds] coding_agent_timeout`
- `## Troubleshooting`: bold entry **`grok binary not found`** — install command + `GROK_BIN`,
  matching the `opencode binary not found` entry style (`:406-410`)
- `[thresholds]` config documentation: add `coding_agent_timeout`

### Success Criteria:

#### Automated Verification:
- [x] `uv run ruff check deep_architect/ tests/` passes
- [x] `uv run mypy deep_architect/` passes
- [x] `uv run python -m pytest tests/ -v` passes (all pre-existing + new tests — 426 total)
- [x] `uv run bandit -r deep_architect/ -ll` passes
- [x] `uv run review-action --help` shows `--provider {opencode,claude,grok}`

#### Manual Verification:
- [ ] End-to-end: `uv run review-action <dir> --provider grok` against a real VALID finding
  applies the fix, passes quality checks, and creates a commit (parity with opencode/claude)
- [ ] `--provider grok --model grok-build` passes `-m grok-build`; omitting `--model` uses
  grok's default (visible in grok session logs / `grok sessions`)
- [ ] Spot-check `--provider opencode` and `--provider claude` still behave exactly as before
- [ ] Grok does NOT create its own commits (the harness's commit is the only one per finding)

---

## Testing Strategy

### Unit Tests:
- `_parse_grok_json` pure-function tests against the four verified output shapes
- `GrokAgent` subprocess-boundary tests (mock `subprocess.run` — the established convention;
  no LLM calls mocked, per project rule)
- Factory dispatch across all three providers + `ValueError` for unknown
- Protocol conformance for all three agents
- Config: `coding_agent_timeout` default/override round-trip

### Integration Tests:
- None automated (project convention: no live LLM calls in unit tests). The `process_findings`
  loop tests continue to exercise the full fix loop with an `OpencodeAgent` + mocked subprocess.

### Manual Testing Steps:
1. Create a throwaway git repo with a trivial VALID finding markdown in `feedback/`
2. `uv run review-action feedback/ --provider grok --verbose` — confirm fix applied, checks run,
   single harness commit created
3. Repeat with `--provider opencode` and `--provider claude` — confirm unchanged behavior
4. Set `coding_agent_timeout = 10.0` in `~/.deep-architect.toml`, run a grok fix that exceeds it
   — confirm clean timeout error + `error` status in the finding file

## Performance Considerations

None material — one subprocess per fix attempt, same as opencode. Grok's default timeout (300s)
is deliberately more generous than opencode's 120s because `-p` mode runs a full agentic loop;
`--max-turns 10` bounds runaway sessions the same way `ClaudeSDKAgent.MAX_TURNS` does.

## Migration Notes

- No data migration. `~/.deep-architect.toml` files without `coding_agent_timeout` keep current
  behavior (per-agent defaults).
- Internal import paths change (`deep_architect.review_action_harness.OpencodeAgent` →
  `deep_architect.coding_agents.OpencodeAgent`); the console-script entry point
  (`review-action` → `review_action_harness:main`) is unaffected.

## References

- Original ticket: `knowledge/tickets/PROJ-0014.md`
- Precedent implementation: `deep_architect/review_action_harness.py:129-452` (`OpencodeAgent`),
  `:1229-1262` (factory), `:1458-1463` (`--provider` flag)
- Claude prompt wording to mirror: `deep_architect/review_action_harness.py:1317-1331`, `:1395-1407`
- Test conventions: `tests/test_review_action_harness.py:314-432` (`TestOpencodeAgent`)
- Grok Build docs: https://docs.x.ai/build/overview, https://docs.x.ai/build/cli/headless-scripting
- Empirical CLI verification: grok 0.2.93, 2026-07-09 (output shapes recorded in this plan)
