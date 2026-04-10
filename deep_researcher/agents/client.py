from __future__ import annotations

import json
import shutil
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from deep_researcher.config import AgentConfig


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
