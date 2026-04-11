"""Tests for client.py configuration logic.

Does NOT test actual LLM calls — only the ClaudeAgentOptions construction,
helper functions, and retry logic (with mocked query() / AsyncAnthropic).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from deep_researcher.agents.client import (
    DISALLOWED_TOOLS,
    _extract_json,
    json_schema_format,
    make_agent_options,
    resolve_model_id,
    run_agent,
    run_simple_structured,
)
from deep_researcher.config import AgentConfig

# Sentinel CLI path — avoids shutil.which("claude") during tests.
_FAKE_CLI = "/usr/bin/true"


# ---------------------------------------------------------------------------
# Helpers for run_agent retry tests
# ---------------------------------------------------------------------------


def _fake_result() -> ResultMessage:
    """Build a minimal ResultMessage that run_agent considers successful."""
    return ResultMessage(
        subtype="success",
        is_error=False,
        duration_ms=100,
        duration_api_ms=100,
        num_turns=1,
        session_id="sess-test",
        result="done",
    )


async def _make_query_that_raises(exc: Exception) -> AsyncIterator[object]:
    """Async generator that immediately raises exc."""
    raise exc
    yield  # makes it an async generator


async def _make_query_that_yields_result(result: ResultMessage) -> AsyncIterator[object]:
    """Async generator that yields a ResultMessage then ends."""
    yield result


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


# ---------------------------------------------------------------------------
# make_agent_options — disallowed_tools
# ---------------------------------------------------------------------------


def test_disallowed_tools_passed_through() -> None:
    """make_agent_options always passes DISALLOWED_TOOLS to the SDK options."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=["Read", "Write"],
        cli_path=_FAKE_CLI,
    )
    assert opts.disallowed_tools == DISALLOWED_TOOLS


def test_disallowed_tools_present_with_empty_allowed_tools() -> None:
    """DISALLOWED_TOOLS is passed even when allowed_tools is empty."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config,
        "system",
        allowed_tools=[],
        cli_path=_FAKE_CLI,
    )
    assert opts.disallowed_tools == DISALLOWED_TOOLS


# ---------------------------------------------------------------------------
# run_agent — retry behaviour
# ---------------------------------------------------------------------------


async def test_run_agent_no_retry_by_default() -> None:
    """With default max_retries=0, a failing query raises immediately."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)
    call_count = 0

    async def _failing_query(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        raise Exception("CLI crashed")
        yield

    with patch("deep_researcher.agents.client.query", side_effect=_failing_query):
        with pytest.raises(Exception, match="CLI crashed"):
            await run_agent(opts, "prompt", label="test")

    assert call_count == 1


async def test_run_agent_retries_on_failure() -> None:
    """run_agent succeeds on the second attempt when max_retries=1."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)
    result = _fake_result()
    call_count = 0

    async def _query_fail_then_succeed(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("CLI crashed")
            yield
        else:
            yield result

    with patch("deep_researcher.agents.client.query", side_effect=_query_fail_then_succeed):
        returned = await run_agent(opts, "prompt", label="test", max_retries=1)

    assert call_count == 2
    assert returned.session_id == "sess-test"


async def test_run_agent_exhausts_retries_and_raises() -> None:
    """run_agent re-raises after max_retries+1 total attempts."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)
    call_count = 0

    async def _always_fail(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        raise Exception("always fails")
        yield

    with patch("deep_researcher.agents.client.query", side_effect=_always_fail):
        with pytest.raises(Exception, match="always fails"):
            await run_agent(opts, "prompt", label="test", max_retries=2)

    assert call_count == 3  # 1 initial + 2 retries


async def test_run_agent_clears_resume_on_retry() -> None:
    """On retry, resume is cleared so the corrupted session is discarded."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config, "system", allowed_tools=[], cli_path=_FAKE_CLI, resume="session-abc"
    )
    result = _fake_result()
    captured_resumes: list[str | None] = []

    async def _record_and_maybe_fail(**kwargs):  # type: ignore[no-untyped-def]
        captured_resumes.append(kwargs.get("options").resume)
        if len(captured_resumes) == 1:
            raise Exception("first call fails")
            yield
        else:
            yield result

    with patch("deep_researcher.agents.client.query", side_effect=_record_and_maybe_fail):
        await run_agent(opts, "prompt", label="test", max_retries=1)

    assert captured_resumes[0] == "session-abc"
    assert captured_resumes[1] is None


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_returns_plain_text_unchanged() -> None:
    raw = '{"value": 1, "label": "hi"}'
    assert _extract_json(raw) == raw


def test_extract_json_strips_json_code_fence() -> None:
    fenced = '```json\n{"value": 1, "label": "hi"}\n```'
    assert _extract_json(fenced) == '{"value": 1, "label": "hi"}'


def test_extract_json_strips_bare_code_fence() -> None:
    fenced = '```\n{"value": 1, "label": "hi"}\n```'
    assert _extract_json(fenced) == '{"value": 1, "label": "hi"}'


def test_extract_json_strips_surrounding_whitespace() -> None:
    padded = '  \n{"value": 1}\n  '
    assert _extract_json(padded) == '{"value": 1}'


# ---------------------------------------------------------------------------
# run_simple_structured — mocked anthropic client (no real LLM calls)
# ---------------------------------------------------------------------------


class _Dummy(BaseModel):
    value: int
    label: str


def _make_response(text: str) -> MagicMock:
    """Build a minimal mock anthropic Message with a single text block."""
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    return response


@patch("deep_researcher.agents.client._anthropic.AsyncAnthropic")
async def test_run_simple_structured_success(mock_cls: MagicMock) -> None:
    """Valid JSON response is parsed and returned as the output type."""
    mock_cls.return_value.messages.create = AsyncMock(
        return_value=_make_response('{"value": 42, "label": "hello"}')
    )
    config = AgentConfig(model="test-model", max_turns=5)
    result = await run_simple_structured(config, "sys", "prompt", _Dummy, label="test")
    assert result.value == 42
    assert result.label == "hello"


@patch("deep_researcher.agents.client._anthropic.AsyncAnthropic")
async def test_run_simple_structured_strips_code_fence(mock_cls: MagicMock) -> None:
    """JSON wrapped in a code fence is extracted and parsed correctly."""
    fenced = '```json\n{"value": 7, "label": "fenced"}\n```'
    mock_cls.return_value.messages.create = AsyncMock(
        return_value=_make_response(fenced)
    )
    config = AgentConfig(model="test-model", max_turns=5)
    result = await run_simple_structured(config, "sys", "prompt", _Dummy, label="test")
    assert result.value == 7
    assert result.label == "fenced"


@patch("deep_researcher.agents.client._anthropic.AsyncAnthropic")
async def test_run_simple_structured_retries_on_bad_json(mock_cls: MagicMock) -> None:
    """A non-JSON first response is retried; a valid second response succeeds."""
    mock_cls.return_value.messages.create = AsyncMock(
        side_effect=[
            _make_response("Sorry, I cannot help with that."),
            _make_response('{"value": 3, "label": "retry"}'),
        ]
    )
    config = AgentConfig(model="test-model", max_turns=5)
    result = await run_simple_structured(config, "sys", "prompt", _Dummy, label="test")
    assert result.value == 3
    assert mock_cls.return_value.messages.create.call_count == 2


@patch("deep_researcher.agents.client._anthropic.AsyncAnthropic")
async def test_run_simple_structured_raises_after_three_failures(
    mock_cls: MagicMock,
) -> None:
    """RuntimeError is raised when all 3 attempts return unparseable JSON."""
    mock_cls.return_value.messages.create = AsyncMock(
        return_value=_make_response("not json at all")
    )
    config = AgentConfig(model="test-model", max_turns=5)
    with pytest.raises(RuntimeError, match="structured output failed after 3 attempts"):
        await run_simple_structured(config, "sys", "prompt", _Dummy, label="test")
    assert mock_cls.return_value.messages.create.call_count == 3
