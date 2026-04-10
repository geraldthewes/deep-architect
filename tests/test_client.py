"""Tests for client.py configuration logic.

Does NOT test actual LLM calls — only the ClaudeAgentOptions construction
and helper functions.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from deep_researcher.agents.client import (
    json_schema_format,
    make_agent_options,
    resolve_model_id,
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
# resolve_model_id
# ---------------------------------------------------------------------------


def test_resolve_model_id_sonnet_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-standard")
    assert resolve_model_id("sonnet") == "claude-sonnet-standard"


def test_resolve_model_id_opus_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-standard")
    assert resolve_model_id("opus") == "claude-opus-standard"


def test_resolve_model_id_haiku_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-standard")
    assert resolve_model_id("haiku") == "claude-haiku-standard"


def test_resolve_model_id_falls_back_to_alias_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_DEFAULT_SONNET_MODEL", raising=False)
    assert resolve_model_id("sonnet") == "sonnet"


def test_resolve_model_id_passthrough_for_full_model_id() -> None:
    # A full model ID that isn't a known alias is returned unchanged.
    assert resolve_model_id("claude-sonnet-4-6-20251001") == "claude-sonnet-4-6-20251001"


# ---------------------------------------------------------------------------
# make_agent_options — max_turns always uses config.max_turns
# ---------------------------------------------------------------------------


def test_max_turns_uses_config_regardless_of_output_format() -> None:
    """make_agent_options always uses config.max_turns (no internal cap)."""
    config = AgentConfig(model="test-model", max_turns=25)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
        output_format=json_schema_format(AgentConfig),
    )
    assert opts.max_turns == 25


def test_max_turns_with_tools_uses_config() -> None:
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
    config = AgentConfig(model="test-model", max_turns=15)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
    )
    assert opts.max_turns == 15


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
