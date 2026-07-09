from __future__ import annotations

from deep_architect.coding_agents.base import CodingAgent, CodingAgentConfig


def create_agent(config: CodingAgentConfig) -> CodingAgent:
    """Factory function to create the appropriate coding agent."""
    if config.provider == "opencode":
        from deep_architect.coding_agents.opencode import OpencodeAgent  # noqa: PLC0415

        return OpencodeAgent(
            model=config.model or "standard/coder",
            timeout_seconds=config.timeout_seconds,
        )
    elif config.provider == "claude":
        return _create_claude_agent(config)
    elif config.provider == "grok":
        from deep_architect.coding_agents.grok import GrokAgent  # noqa: PLC0415

        return GrokAgent(model=config.model, timeout_seconds=config.timeout_seconds)
    else:
        raise ValueError(
            f"Unsupported agent provider: {config.provider}"
        )


def _create_claude_agent(config: CodingAgentConfig) -> CodingAgent:
    """Create a Claude SDK agent, or raise if SDK unavailable."""
    try:
        from claude_agent_sdk import (  # noqa: F401 PLC0415
            ClaudeAgentOptions,
            query,
        )
    except ImportError:
        raise ImportError(
            "claude-agent-sdk is required for claude provider. "
            "Install it with: pip install claude-agent-sdk"
        ) from None

    from deep_architect.coding_agents.claude import ClaudeSDKAgent  # noqa: PLC0415

    return ClaudeSDKAgent(
        model=config.model or "sonnet",
        permission_mode=config.permission_mode,
        disallowed_tools=config.disallowed_tools,
        timeout_seconds=config.timeout_seconds,
    )
