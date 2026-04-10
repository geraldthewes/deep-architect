from __future__ import annotations

import json
import shutil
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from deep_researcher.config import AgentConfig
from deep_researcher.logger import get_logger

_log = get_logger(__name__)


@dataclass
class RunStats:
    """Accumulates token and cost totals across all agent calls in a harness run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_cost_usd: float = 0.0
    num_calls: int = 0
    num_turns: int = 0
    duration_ms: int = field(default=0)

    def accumulate(self, result: ResultMessage) -> None:
        usage = result.usage or {}
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        self.cache_write_tokens += usage.get("cache_creation_input_tokens", 0)
        self.total_cost_usd += result.total_cost_usd or 0.0
        self.num_calls += 1
        self.num_turns += result.num_turns
        self.duration_ms += result.duration_ms

    def log_summary(self) -> None:
        _log.info(
            "Run totals: calls=%d turns=%d duration=%.1fs "
            "input=%d output=%d cache_read=%d cache_write=%d cost=$%.4f",
            self.num_calls,
            self.num_turns,
            self.duration_ms / 1000,
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.total_cost_usd,
        )


# Set by run_harness at the start of each run; accumulated in run_agent.
_run_stats: ContextVar[RunStats | None] = ContextVar("_run_stats", default=None)


def init_run_stats() -> RunStats:
    """Create a fresh RunStats and register it in the current context."""
    stats = RunStats()
    _run_stats.set(stats)
    return stats


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
    """Build ClaudeAgentOptions for a query."""

    def _stderr_cb(line: str) -> None:
        _log.warning("[claude stderr] %s", line)

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        allowed_tools=allowed_tools,
        model=config.model,
        max_turns=config.max_turns,
        cwd=cwd,
        cli_path=_resolve_cli_path(cli_path),
        output_format=output_format,
        resume=resume,
        stderr=_stderr_cb,
    )


async def run_agent(
    options: ClaudeAgentOptions,
    prompt: str,
) -> ResultMessage:
    """Run a query and return the ResultMessage."""
    result: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        raise RuntimeError("Agent query completed without a ResultMessage")
    if result.is_error:
        raise RuntimeError(f"Agent query failed: {result.result}")

    # Log per-call usage
    usage = result.usage or {}
    input_tokens = usage.get("input_tokens", "?")
    output_tokens = usage.get("output_tokens", "?")
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    cost = f"${result.total_cost_usd:.4f}" if result.total_cost_usd is not None else "n/a"
    _log.info(
        "Agent call complete: turns=%d duration=%.1fs "
        "input=%s output=%s cache_read=%s cache_write=%s cost=%s",
        result.num_turns,
        result.duration_ms / 1000,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        cost,
    )

    # Accumulate into run totals if a stats context is active
    stats = _run_stats.get()
    if stats is not None:
        stats.accumulate(result)

    return result


async def run_agent_text(
    options: ClaudeAgentOptions,
    prompt: str,
) -> str:
    """Run a query and return the text result."""
    result = await run_agent(options, prompt)
    return result.result or ""


async def run_agent_structured(
    options: ClaudeAgentOptions,
    prompt: str,
) -> dict[str, Any]:
    """Run a query with output_format and return parsed structured output."""
    result = await run_agent(options, prompt)
    if result.structured_output is not None:
        output: dict[str, Any] = result.structured_output
        return output
    # Fallback: parse from result text
    text = result.result or ""
    parsed: dict[str, Any] = json.loads(text)
    return parsed
