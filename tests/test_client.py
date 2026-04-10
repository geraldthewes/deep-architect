"""Tests for make_agent_options configuration logic.

Does NOT test actual LLM calls — only the ClaudeAgentOptions construction.
"""
from __future__ import annotations

from pydantic import BaseModel

from deep_researcher.agents.client import (
    STRUCTURED_OUTPUT_MAX_TURNS,
    json_schema_format,
    make_agent_options,
)
from deep_researcher.config import AgentConfig

# Sentinel CLI path — avoids shutil.which("claude") during tests.
_FAKE_CLI = "/usr/bin/true"


# ---------------------------------------------------------------------------
# json_schema_format
# ---------------------------------------------------------------------------


def test_json_schema_format_wraps_pydantic_schema() -> None:
    class Foo(BaseModel):
        x: int
        y: str

    fmt = json_schema_format(Foo)
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    assert "properties" in schema
    assert "x" in schema["properties"]
    assert "y" in schema["properties"]


# ---------------------------------------------------------------------------
# make_agent_options — max_turns
# ---------------------------------------------------------------------------


def test_max_turns_no_tools_with_output_format_uses_structured_cap() -> None:
    """allowed_tools=[] + output_format → capped at STRUCTURED_OUTPUT_MAX_TURNS."""
    config = AgentConfig(model="test-model", max_turns=30)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
        output_format=json_schema_format(AgentConfig),
    )
    assert opts.max_turns == STRUCTURED_OUTPUT_MAX_TURNS
    # Cap must be < 30 (prevents runaway loops) and > 1 (protocol needs ≥2 turns).
    assert STRUCTURED_OUTPUT_MAX_TURNS > 1
    assert STRUCTURED_OUTPUT_MAX_TURNS < 30


def test_max_turns_with_tools_and_output_format_uses_config() -> None:
    """allowed_tools non-empty + output_format → config.max_turns (critic needs tool turns)."""
    config = AgentConfig(model="test-model", max_turns=20)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=["Read", "Grep"],
        cli_path=_FAKE_CLI,
        output_format=json_schema_format(AgentConfig),
    )
    assert opts.max_turns == 20


def test_max_turns_no_output_format_uses_config() -> None:
    """No output_format → config.max_turns regardless of allowed_tools."""
    config = AgentConfig(model="test-model", max_turns=15)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
    )
    assert opts.max_turns == 15


def test_max_turns_with_tools_no_output_format_uses_config() -> None:
    config = AgentConfig(model="test-model", max_turns=50)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=["Read", "Write", "Bash"],
        cli_path=_FAKE_CLI,
    )
    assert opts.max_turns == 50


# ---------------------------------------------------------------------------
# make_agent_options — sdk_tools / allowed_tools
# ---------------------------------------------------------------------------


def test_sdk_tools_is_empty_list_when_allowed_tools_empty() -> None:
    """Empty allowed_tools → tools=[] (CLI gets --tools '' to disable all tools)."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
    )
    assert opts.tools == []
    assert opts.allowed_tools == []


def test_sdk_tools_is_none_when_allowed_tools_given() -> None:
    """Non-empty allowed_tools → tools=None (no --tools flag; CLI uses --allowedTools)."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=["Read", "Grep"],
        cli_path=_FAKE_CLI,
    )
    assert opts.tools is None
    assert opts.allowed_tools == ["Read", "Grep"]


# ---------------------------------------------------------------------------
# make_agent_options — output_format passthrough
# ---------------------------------------------------------------------------


def test_output_format_passed_through() -> None:
    class MySchema(BaseModel):
        result: str

    fmt = json_schema_format(MySchema)
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
        output_format=fmt,
    )
    assert opts.output_format == fmt


def test_output_format_none_by_default() -> None:
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
    )
    assert opts.output_format is None
