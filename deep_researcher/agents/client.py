from __future__ import annotations

import json
import shutil
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

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

from deep_researcher.config import AgentConfig
from deep_researcher.logger import get_logger

_log = get_logger(__name__)


# Tool names the SDK legitimately provides. Anything else is a hallucinated tool call.
KNOWN_TOOLS: frozenset[str] = frozenset({"Read", "Write", "Edit", "Bash", "Glob", "Grep"})


def json_schema_format(model_class: type[BaseModel]) -> dict[str, Any]:
    """Build an output_format dict from a Pydantic model's JSON schema."""
    return {"type": "json_schema", "schema": model_class.model_json_schema()}


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


# Max turns for tool-less structured output calls (--json-schema without --allowedTools).
# The --json-schema protocol needs ≥2 turns (initial response + StructuredOutput call),
# so this must be > 1.  It is deliberately lower than config.max_turns to prevent
# criterion-named tool-call loops when the model ignores the no-tools constraint.
STRUCTURED_OUTPUT_MAX_TURNS: int = 8


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
    """Build ClaudeAgentOptions for a query."""

    def _stderr_cb(line: str) -> None:
        _log.warning("[claude stderr] %s", line)

    # An empty allowed_tools list is falsy, so the SDK would NOT pass --allowedTools,
    # leaving all default tools unrestricted.  Use tools=[] instead, which maps to
    # --tools "" and actually disables all tools.
    sdk_tools: list[str] | None = [] if not allowed_tools else None

    # Structured output calls WITHOUT tools need a few turns for the --json-schema
    # protocol (model response + StructuredOutput tool call), but must be capped to
    # prevent hallucinated-tool retry storms.  When tools ARE provided (e.g. run_critic),
    # keep the full config.max_turns so the agent can read files first.
    if output_format is not None and not allowed_tools:
        max_turns = STRUCTURED_OUTPUT_MAX_TURNS
    else:
        max_turns = config.max_turns

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        tools=sdk_tools,
        allowed_tools=allowed_tools,
        model=config.model,
        max_turns=max_turns,
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
) -> ResultMessage:
    """Run a query and return the ResultMessage."""
    result: ResultMessage | None = None
    turn_count = 0
    tool_count = 0
    text_block_count = 0

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            turn_count += 1

            if message.error is not None:
                _log.warning("[%s] turn=%d API error: %s", label, turn_count, message.error)

            for block in message.content:
                if isinstance(block, TextBlock):
                    text_block_count += 1
                    snippet = block.text[:200].replace("\n", " ")
                    if len(block.text) > 200:
                        snippet += "..."
                    _log.debug(
                        "[%s] turn=%d text (%d chars): %s",
                        label, turn_count, len(block.text), snippet,
                    )
                elif isinstance(block, ToolUseBlock):
                    tool_count += 1
                    if block.name not in KNOWN_TOOLS:
                        _log.warning(
                            "[%s] turn=%d unexpected tool call: %s (input keys: %s)",
                            label, turn_count, block.name, list(block.input.keys()),
                        )
                    else:
                        _log.info("[%s] turn=%d %s", label, turn_count, _tool_summary(block))

        elif isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            if info.status == "rejected":
                _log.error(
                    "[%s] Rate limit REJECTED (type=%s resets_at=%s)",
                    label, info.rate_limit_type, info.resets_at,
                )
            elif info.status == "allowed_warning":
                util_pct = f"{info.utilization * 100:.0f}%" if info.utilization is not None else "?"
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


async def run_agent_text(
    options: ClaudeAgentOptions,
    prompt: str,
    label: str = "Agent",
) -> str:
    """Run a query and return the text result."""
    result = await run_agent(options, prompt, label=label)
    return result.result or ""


async def run_agent_structured(
    options: ClaudeAgentOptions,
    prompt: str,
    label: str = "Agent",
) -> dict[str, Any]:
    """Run a query with output_format and return parsed structured output."""
    result = await run_agent(options, prompt, label=label)
    if result.structured_output is not None:
        output: dict[str, Any] = result.structured_output
        return output
    # Fallback: parse from result text
    text = result.result or ""
    parsed: dict[str, Any] = json.loads(text)
    return parsed
