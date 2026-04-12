from __future__ import annotations

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


def json_schema_format(model_class: type[BaseModel]) -> dict[str, Any]:
    """Build an output_format dict from a Pydantic model's JSON schema."""
    return {"type": "json_schema", "schema": model_class.model_json_schema()}


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
    name = tool.name
    if name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", "")
        return f"{name} {path}"
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"Bash: {cmd[:60]}{'...' if len(cmd) > 60 else ''}"
    if name in ("Grep", "Glob"):
        return f"{name}: {inp.get('pattern', inp.get('glob', ''))}"
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
        _log.warning("[claude stderr] %s", line)

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


async def run_agent(
    options: ClaudeAgentOptions,
    prompt: str,
    label: str = "Agent",
    max_retries: int = 0,
    context_window: int | None = None,
    last_known_input_tokens: int = 0,
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
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 2):
        result: ResultMessage | None = None
        turn_count = 0
        tool_count = 0
        text_block_count = 0

        try:
            async for message in query(prompt=prompt, options=options):
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

            if result is None:
                raise RuntimeError("Agent query completed without a ResultMessage")
            if result.is_error:
                raise RuntimeError(f"Agent query failed: {result.result}")

        except Exception as exc:
            last_exc = exc
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
) -> str:
    """Run a query and return the text result."""
    result = await run_agent(
        options, prompt, label=label, max_retries=max_retries,
        context_window=context_window, last_known_input_tokens=last_known_input_tokens,
    )
    return result.result or ""


async def run_agent_structured(
    options: ClaudeAgentOptions,
    prompt: str,
    label: str = "Agent",
    max_retries: int = 0,
    context_window: int | None = None,
    last_known_input_tokens: int = 0,
) -> dict[str, Any]:
    """Run a query with output_format and return parsed structured output."""
    result = await run_agent(
        options, prompt, label=label, max_retries=max_retries,
        context_window=context_window, last_known_input_tokens=last_known_input_tokens,
    )
    if result.structured_output is not None:
        output: dict[str, Any] = result.structured_output
        return output
    # Fallback: parse from result text
    text = result.result or ""
    parsed: dict[str, Any] = json.loads(text)
    return parsed
