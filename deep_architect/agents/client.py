from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import shutil
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

import anthropic as _anthropic
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)
from pydantic import BaseModel

from deep_architect.config import AgentConfig
from deep_architect.logger import get_logger

_log = get_logger(__name__)


class TurnLimitError(RuntimeError):
    """Raised when the agent exhausts its max_turns budget."""


# Tracks the last non-empty assistant text block seen during _consume_query.
# Used by run_agent_structured as a fallback when ResultMessage.result is empty
# (e.g. the agent ended its session after a tool call, producing no final text).
# ContextVar is safe here because _consume_query and run_agent_structured always
# run in the same asyncio task — the value is set and read in the same coroutine chain.
_last_agent_text: ContextVar[str] = ContextVar("_last_agent_text", default="")

# Tool names the SDK legitimately provides.
KNOWN_TOOLS: frozenset[str] = frozenset({"Read", "Write", "Edit", "Bash", "Glob", "Grep"})
# Internal CLI tool used when --json-schema is active; not a user tool but not a hallucination.
_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"

# CLI-internal tools that the model may hallucinate but must never invoke in SDK subprocesses.
# Passed via --disallowedTools so the CLI rejects them before execution.
DISALLOWED_TOOLS: list[str] = [
    "TodoWrite",
    "TodoRead",
    "Agent",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "WebSearch",
    "WebFetch",
    "NotebookEdit",
    "NotebookRead",
    "SendMessage",
    "ToolSearch",
    "Skill",
    "Monitor",
]


def _deref_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve all $ref entries inline so the schema has no $defs.

    CLI 2.1.104+ rejects output_format schemas that contain $ref / $defs.
    This flattens Pydantic's default schema output so every reference is inlined.
    """
    defs = schema.get("$defs", {})
    if not defs:
        return schema

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref: str = node["$ref"]  # e.g. "#/$defs/CriterionScore"
                name = ref.split("/")[-1]
                return _resolve(defs[name])
            return {k: _resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [_resolve(i) for i in node]
        return node

    resolved = _resolve(schema)
    assert isinstance(resolved, dict)
    return resolved


def json_schema_format(model_class: type[BaseModel]) -> dict[str, Any]:
    """Build an output_format dict from a Pydantic model's JSON schema.

    The schema is dereffed so it contains no $defs/$ref entries, which are
    rejected by CLI 2.1.104+.
    """
    return {"type": "json_schema", "schema": _deref_schema(model_class.model_json_schema())}


# ---------------------------------------------------------------------------
# Simple structured output — pydantic-ai (no agentic loop, no tool use)
# ---------------------------------------------------------------------------

_T = TypeVar("_T", bound=BaseModel)  # noqa: UP047

# Env-var names that map model aliases to the actual model IDs at the litellm proxy.
_MODEL_ALIAS_ENV: dict[str, str] = {
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}


def resolve_model_id(alias: str) -> str:
    """Resolve a model alias (sonnet/opus/haiku) to the real model ID via env vars.

    Falls back to the alias itself if no env var is set, so a full model ID
    can also be passed directly.
    """
    env_var = _MODEL_ALIAS_ENV.get(alias)
    if env_var:
        return os.environ.get(env_var, alias)
    return alias


def _extract_json(text: str) -> str:
    """Strip markdown code fences and return the JSON portion of text."""
    stripped = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]+?)\s*```\s*$", stripped)
    if m:
        return m.group(1).strip()
    return stripped


async def run_simple_structured(  # noqa: UP047
    config: AgentConfig,
    system_prompt: str,
    prompt: str,
    output_type: type[_T],
    label: str = "Agent",
) -> _T:
    """Single structured-output call via direct Anthropic API (no tool use).

    Instructs the model to return JSON matching the output_type schema via the
    system prompt, then extracts and validates with Pydantic.  Uses the anthropic
    SDK directly rather than pydantic-ai's tool_choice mechanism, which can fail
    on litellm proxies that don't properly honour tool_choice for all models.
    """
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    model_id = resolve_model_id(config.model)

    schema_str = json.dumps(output_type.model_json_schema(), indent=2)
    json_system = (
        f"{system_prompt}\n\n"
        "## Output Format\n\n"
        "Respond with ONLY a valid JSON object matching this schema — "
        "no markdown, no explanation, no code fences:\n\n"
        f"{schema_str}"
    )

    client = _anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
    last_exc: Exception | None = None

    for attempt in range(1, 4):
        t0 = time.monotonic()
        response = await client.messages.create(
            model=model_id,
            max_tokens=4096,
            system=json_system,
            messages=[{"role": "user", "content": prompt}],
        )
        elapsed = time.monotonic() - t0

        text = "".join(b.text for b in response.content if hasattr(b, "text"))

        try:
            result = output_type.model_validate_json(_extract_json(text))
        except Exception as exc:
            last_exc = exc
            _log.warning(
                "[%s] attempt %d/3 JSON parse failed (%s) — raw: %.200s",
                label, attempt, exc, text,
            )
            continue

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        model_id = resolve_model_id(config.model)
        _log.info(
            "[%s] done: duration=%.1fs input=%d output=%d requests=%d model=%s",
            label, elapsed, input_tokens, output_tokens, attempt, model_id,
        )

        stats = _run_stats.get()
        if stats is not None:
            stats.num_calls += 1
            stats.num_turns += 1
            stats.duration_ms += int(elapsed * 1000)
            ms = stats._model(model_id)
            ms.input_tokens += input_tokens
            ms.output_tokens += output_tokens

        return result

    raise RuntimeError(
        f"[{label}] structured output failed after 3 attempts"
    ) from last_exc


def extract_input_tokens(result: ResultMessage) -> int:
    """Return total input_tokens from a ResultMessage.

    Sums across model_usage entries when present; falls back to the
    top-level usage dict.  Returns 0 if neither is populated.
    """
    model_usage: dict[str, Any] = result.model_usage or {}
    if model_usage:
        return sum(int(u.get("input_tokens", 0)) for u in model_usage.values())
    usage: dict[str, Any] = result.usage or {}
    return int(usage.get("input_tokens", 0))


@dataclass
class ModelStats:
    """Per-model token totals."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class RunStats:
    """Accumulates token and cost totals across all agent calls in a harness run."""

    total_cost_usd: float = 0.0
    num_calls: int = 0
    num_turns: int = 0
    duration_ms: int = field(default=0)
    by_model: dict[str, ModelStats] = field(default_factory=dict)

    def _model(self, name: str) -> ModelStats:
        if name not in self.by_model:
            self.by_model[name] = ModelStats()
        return self.by_model[name]

    def accumulate(self, result: ResultMessage) -> None:
        self.total_cost_usd += result.total_cost_usd or 0.0
        self.num_calls += 1
        self.num_turns += result.num_turns
        self.duration_ms += result.duration_ms

        # model_usage is {model_name: {input_tokens, output_tokens, ...}}
        model_usage: dict[str, Any] = result.model_usage or {}
        if model_usage:
            for model_name, usage in model_usage.items():
                ms = self._model(model_name)
                ms.input_tokens += usage.get("input_tokens", 0)
                ms.output_tokens += usage.get("output_tokens", 0)
                ms.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                ms.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
        else:
            # Fallback: attribute to the top-level usage under "unknown"
            usage = result.usage or {}
            ms = self._model("unknown")
            ms.input_tokens += usage.get("input_tokens", 0)
            ms.output_tokens += usage.get("output_tokens", 0)
            ms.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            ms.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)

    def log_summary(self) -> None:
        _log.info(
            "Run totals: calls=%d turns=%d duration=%.1fs cost=$%.4f",
            self.num_calls,
            self.num_turns,
            self.duration_ms / 1000,
            self.total_cost_usd,
        )
        for model_name, ms in sorted(self.by_model.items()):
            _log.info(
                "  model=%s input=%d output=%d cache_read=%d cache_write=%d",
                model_name,
                ms.input_tokens,
                ms.output_tokens,
                ms.cache_read_tokens,
                ms.cache_write_tokens,
            )


# Set by run_harness at the start of each run; accumulated in run_agent.
_run_stats: ContextVar[RunStats | None] = ContextVar("_run_stats", default=None)


def init_run_stats() -> RunStats:
    """Create a fresh RunStats and register it in the current context."""
    stats = RunStats()
    _run_stats.set(stats)
    return stats


def _tool_summary(tool: ToolUseBlock) -> str:
    """Return a short human-readable description of a tool call."""
    inp = tool.input
    name: str = tool.name
    if name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", "")
        return f"{name} {path}"
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"Bash: {cmd[:60]}{'...' if len(cmd) > 60 else ''}"
    if name in ("Grep", "Glob"):
        pattern: str = inp.get("pattern", inp.get("glob", "")) or ""
        return f"{name}: {pattern}"
    return name


def _resolve_cli_path(override: str | None = None) -> str:
    """Resolve the claude CLI binary path.

    Uses the system-installed binary by default to ensure ANTHROPIC_BASE_URL
    and other env vars are respected (the bundled SDK binary may ignore them).
    """
    if override:
        return override
    path = shutil.which("claude")
    if not path:
        raise FileNotFoundError(
            "claude CLI not found in PATH. Install Claude Code or set cli_path in config."
        )
    return path


def make_agent_options(
    config: AgentConfig,
    system_prompt: str,
    *,
    allowed_tools: list[str],
    cwd: str | None = None,
    cli_path: str | None = None,
    output_format: dict[str, Any] | None = None,
    resume: str | None = None,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for an agentic query (generator or critic).

    For simple structured output without tools, use run_simple_structured()
    instead — it calls the Anthropic API directly and avoids the CLI turn-loop.
    """

    def _stderr_cb(line: str) -> None:
        _log.error("[claude stderr] %s", line)

    # An empty allowed_tools list is falsy, so the SDK would NOT pass --allowedTools,
    # leaving all default tools unrestricted.  Use tools=[] instead, which maps to
    # --tools "" and actually disables all tools.
    sdk_tools: list[str] | None = [] if not allowed_tools else None

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        tools=sdk_tools,
        allowed_tools=allowed_tools,
        disallowed_tools=DISALLOWED_TOOLS,
        model=config.model,
        max_turns=config.max_turns,
        cwd=cwd,
        cli_path=_resolve_cli_path(cli_path),
        output_format=output_format,
        resume=resume,
        stderr=_stderr_cb,
        # Disable extended thinking for SDK subprocesses — it makes every response
        # very slow and expensive, which is counterproductive in an automation loop.
        # The --settings flag merges with ~/.claude/settings.json, so this only
        # overrides alwaysThinkingEnabled without touching other user settings.
        settings='{"alwaysThinkingEnabled": false}',
    )


async def _consume_query(
    prompt: str,
    options: ClaudeAgentOptions,
    label: str,
    context_window: int | None,
    last_known_input_tokens: int,
    inactivity_seconds: float | None = None,
    event_buffer: collections.deque[str] | None = None,
) -> tuple[ResultMessage, int, int, int]:
    """Consume one query generator and return (result, turn_count, tool_count, text_block_count).

    Raises RuntimeError if the query completes without a ResultMessage or reports an error.

    If *inactivity_seconds* is set, the deadline is reset after every message from the SDK.
    The timeout only fires when no message has arrived within that window — i.e. the
    connection appears stalled.  A long-running but actively-streaming agent will never
    be interrupted.
    """
    result: ResultMessage | None = None
    turn_count = 0
    tool_count = 0
    text_block_count = 0
    # Reset last-text tracker for this query so a stale value from a prior
    # call in the same task never leaks into the fallback path.
    _last_agent_text.set("")

    # Drive the SDK generator manually so we can apply a per-step timeout.
    # asyncio.wait_for() wraps each __anext__() call individually, which means
    # the deadline resets after every message.  The timeout only fires when no
    # message has arrived within the window — i.e. the connection is stalled —
    # and never interrupts an agent that is actively producing output.
    gen = query(prompt=prompt, options=options).__aiter__()
    while True:
        try:
            message = (
                await asyncio.wait_for(gen.__anext__(), timeout=inactivity_seconds)
                if inactivity_seconds is not None
                else await gen.__anext__()
            )
        except StopAsyncIteration:
            break
        except TimeoutError:
            raise  # let run_agent's TimeoutError handler deal with it
        except Exception as exc:
            # If the CLI exited because the turn limit was reached, surface a
            # clear error rather than the opaque "exit code 1" message.
            if options.max_turns is not None and turn_count >= options.max_turns:
                raise TurnLimitError(
                    f"Turn limit reached (max_turns={options.max_turns}, "
                    f"turns_completed={turn_count})"
                ) from exc
            raise
        if isinstance(message, AssistantMessage):
            turn_count += 1

            if message.error is not None:
                _log.warning(
                    "[%s] turn=%d API error: %s", label, turn_count, message.error
                )

            # Build context-usage suffix for log lines.
            # The CLI's stream-json sets input_tokens=0 on intermediate
            # turns; real counts only arrive in the final ResultMessage.
            # Fall back to last_known_input_tokens (from the previous
            # round) so the suffix is meaningful from round 2 onward.
            ctx_suffix = ""
            msg_usage = message.usage or {}
            turn_tokens = msg_usage.get("input_tokens") or 0
            display_tokens = turn_tokens or last_known_input_tokens
            approx = "" if turn_tokens else ">="
            if display_tokens and context_window is not None:
                pct = display_tokens / context_window * 100
                ctx_suffix = (
                    f" (ctx {approx}{display_tokens:,}/{context_window:,} {pct:.0f}%)"
                )
            elif display_tokens:
                ctx_suffix = f" (ctx {approx}{display_tokens:,})"

            for block in message.content:
                if isinstance(block, TextBlock):
                    text_block_count += 1
                    if block.text.strip():
                        _last_agent_text.set(block.text)
                    snippet = block.text[:200].replace("\n", " ")
                    if len(block.text) > 200:
                        snippet += "..."
                    log_fn = _log.warning if message.error is not None else _log.debug
                    log_fn(
                        "[%s] turn=%d text (%d chars): %s",
                        label, turn_count, len(block.text), snippet,
                    )
                elif isinstance(block, ToolUseBlock):
                    tool_count += 1
                    if block.name == _STRUCTURED_OUTPUT_TOOL:
                        _log.info(
                            "[%s] turn=%d StructuredOutput (keys: %s)%s",
                            label, turn_count, list(block.input.keys()),
                            ctx_suffix,
                        )
                    elif block.name not in KNOWN_TOOLS:
                        _log.warning(
                            "[%s] turn=%d unexpected tool call: %s (input keys: %s)%s",
                            label, turn_count, block.name,
                            list(block.input.keys()), ctx_suffix,
                        )
                    else:
                        _log.info(
                            "[%s] turn=%d %s%s",
                            label, turn_count, _tool_summary(block),
                            ctx_suffix,
                        )
                    if event_buffer is not None and block.name != _STRUCTURED_OUTPUT_TOOL:
                        event_buffer.append(f"turn={turn_count} {_tool_summary(block)}")

        elif isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            if info.status == "rejected":
                _log.error(
                    "[%s] Rate limit REJECTED (type=%s resets_at=%s)",
                    label, info.rate_limit_type, info.resets_at,
                )
            elif info.status == "allowed_warning":
                util_pct = (
                    f"{info.utilization * 100:.0f}%"
                    if info.utilization is not None
                    else "?"
                )
                _log.warning(
                    "[%s] Rate limit warning (type=%s utilization=%s)",
                    label, info.rate_limit_type, util_pct,
                )

        elif isinstance(message, ResultMessage):
            result = message

        else:
            _log.debug(
                "[%s] unhandled SDK message type: %s", label, type(message).__name__
            )

    if result is None:
        raise RuntimeError("Agent query completed without a ResultMessage")
    if result.is_error:
        raise RuntimeError(f"Agent query failed: {result.result}")

    return result, turn_count, tool_count, text_block_count


async def run_agent(
    options: ClaudeAgentOptions,
    prompt: str,
    label: str = "Agent",
    max_retries: int = 0,
    context_window: int | None = None,
    last_known_input_tokens: int = 0,
    timeout_seconds: float | None = None,
) -> ResultMessage:
    """Run a query and return the ResultMessage.

    On a process-level failure (e.g. the CLI crashes because the model called a
    disallowed tool), the query is retried up to *max_retries* times with a
    fresh session (resume=None) so the corrupted subprocess state is discarded.

    If *context_window* is provided, per-turn log lines include the context
    utilisation as a percentage.  The CLI's stream-json reports input_tokens=0
    on intermediate AssistantMessage turns; pass *last_known_input_tokens* from
    the previous round's ResultMessage to show a meaningful baseline until real
    per-turn counts become available.

    If *timeout_seconds* is set, it acts as an **inactivity** timeout: the clock
    resets after every message received from the SDK.  The timeout fires only when
    no message arrives within that window, indicating a stalled connection (e.g.
    after laptop hibernation).  A busy agent producing output every few seconds
    will never be interrupted regardless of total run time.
    A timed-out attempt is retried like any other failure (with a fresh session).
    """
    last_exc: Exception | None = None
    stderr_lines: list[str] = []
    # Ring buffer of the last 10 tool calls — survives across retries so we
    # always have the most recent context at the point of failure.
    last_events: collections.deque[str] = collections.deque(maxlen=10)

    # Wrap the SDK stderr callback to also buffer lines for error reporting.
    # This lets us re-emit them with full attempt context when an exception is
    # caught, even if the real-time callback already fired.
    _orig_stderr = options.stderr

    def _buffered_stderr(line: str) -> None:
        stderr_lines.append(line)
        if _orig_stderr:
            _orig_stderr(line)

    options = replace(options, stderr=_buffered_stderr)

    for attempt in range(1, max_retries + 2):
        stderr_lines.clear()
        try:
            result, turn_count, tool_count, text_block_count = await _consume_query(
                prompt, options, label, context_window, last_known_input_tokens,
                inactivity_seconds=timeout_seconds,
                event_buffer=last_events,
            )

        except TurnLimitError as exc:
            # Log context then re-raise immediately — retrying with a fresh
            # session won't help; the task simply needs more turns.
            if last_events:
                _log.error(
                    "[%s] Last %d event(s) before turn limit:",
                    label, len(last_events),
                )
                for evt in last_events:
                    _log.error("[%s]   %s", label, evt)
            _log.error(
                "[%s] %s — not retrying (increase max_turns=%d in ~/.deep-architect.toml)",
                label, exc, options.max_turns or 0,
            )
            last_exc = exc
            raise

        except TimeoutError:
            _log.warning(
                "[%s] attempt %d/%d TIMED OUT — no response for %.0fs"
                " (connection may be stalled, e.g. after hibernation)",
                label, attempt, max_retries + 1, timeout_seconds,
            )
            last_exc = TimeoutError(f"Agent timed out after {timeout_seconds}s")
            options = replace(options, resume=None)
            continue

        except Exception as exc:
            last_exc = exc
            # Give the SDK stderr reader task time to drain remaining lines.
            # asyncio.sleep(0) yields only one cycle; 0.25s is enough for the
            # async reader to flush whatever the subprocess wrote before dying.
            await asyncio.sleep(0.25)
            # Log pre-crash tool-call context first so it appears near the error.
            if last_events:
                _log.error(
                    "[%s] Last %d event(s) before crash:",
                    label, len(last_events),
                )
                for evt in last_events:
                    _log.error("[%s]   %s", label, evt)
            if stderr_lines:
                _log.error(
                    "[%s] attempt %d/%d — CLI stderr (%d line(s)):",
                    label, attempt, max_retries + 1, len(stderr_lines),
                )
                for line in stderr_lines:
                    _log.error("[%s] stderr: %s", label, line)
            # Best-effort extraction of structured fields (stripped by SDK bug
            # #798/#800 in v0.1.58, but preserved here for future SDK releases).
            exit_code = getattr(exc, "exit_code", None)
            sdk_stderr = getattr(exc, "stderr", None)
            if exit_code is not None:
                _log.error("[%s] exit_code=%s", label, exit_code)
            if sdk_stderr and sdk_stderr != "Check stderr output for details":
                _log.error("[%s] SDK stderr: %s", label, sdk_stderr)
            if attempt <= max_retries:
                _log.error(
                    "[%s] attempt %d/%d failed: %s — retrying with fresh session",
                    label, attempt, max_retries + 1, exc,
                )
                options = replace(options, resume=None)
                continue
            raise

        # Turn-level summary
        _log.info(
            "[%s] summary: turns=%d (sdk=%d) tool_calls=%d text_blocks=%d",
            label, turn_count, result.num_turns, tool_count, text_block_count,
        )

        # Per-call usage
        cost = f"${result.total_cost_usd:.4f}" if result.total_cost_usd is not None else "n/a"
        _log.info(
            "[%s] done: duration=%.1fs cost=%s",
            label,
            result.duration_ms / 1000,
            cost,
        )
        model_usage: dict[str, Any] = result.model_usage or {}
        if model_usage:
            for model_name, usage in sorted(model_usage.items()):
                _log.info(
                    "[%s]   model=%s input=%s output=%s cache_read=%s cache_write=%s",
                    label,
                    model_name,
                    usage.get("input_tokens", "?"),
                    usage.get("output_tokens", "?"),
                    usage.get("cache_read_input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                )
        else:
            usage = result.usage or {}
            _log.info(
                "[%s]   model=%s input=%s output=%s cache_read=%s cache_write=%s",
                label,
                options.model,
                usage.get("input_tokens", "?"),
                usage.get("output_tokens", "?"),
                usage.get("cache_read_input_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
            )

        # Accumulate into run totals if a stats context is active
        stats = _run_stats.get()
        if stats is not None:
            stats.accumulate(result)

        return result

    # Unreachable: the loop always either returns or re-raises, but satisfies mypy.
    assert last_exc is not None
    raise last_exc


async def run_agent_text(
    options: ClaudeAgentOptions,
    prompt: str,
    label: str = "Agent",
    max_retries: int = 0,
    context_window: int | None = None,
    last_known_input_tokens: int = 0,
    timeout_seconds: float | None = None,
) -> str:
    """Run a query and return the text result."""
    result = await run_agent(
        options, prompt, label=label, max_retries=max_retries,
        context_window=context_window, last_known_input_tokens=last_known_input_tokens,
        timeout_seconds=timeout_seconds,
    )
    return result.result or ""


async def run_agent_structured(
    options: ClaudeAgentOptions,
    prompt: str,
    label: str = "Agent",
    max_retries: int = 0,
    context_window: int | None = None,
    last_known_input_tokens: int = 0,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run a query with output_format and return parsed structured output."""
    result = await run_agent(
        options, prompt, label=label, max_retries=max_retries,
        context_window=context_window, last_known_input_tokens=last_known_input_tokens,
        timeout_seconds=timeout_seconds,
    )
    if result.structured_output is not None:
        output: dict[str, Any] = result.structured_output
        return output
    # Fallback: parse from result text (strip code fences if present).
    # If result.result is empty (agent ended after a tool call with no final
    # text turn), fall back to the last non-empty text block seen during the
    # session — captured by _consume_query via _last_agent_text.
    text = result.result or _last_agent_text.get() or ""
    extracted = _extract_json(text)
    if not extracted:
        raise ValueError(
            f"[{label}] Agent returned no structured output and no parseable text. "
            "The agent likely ended its session after a tool call without emitting a "
            "final JSON response. Check that the system prompt instructs the model to "
            "end with a JSON object as its last message."
        )
    parsed: dict[str, Any] = json.loads(extracted)
    return parsed
