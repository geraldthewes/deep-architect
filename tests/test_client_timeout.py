"""Tests for per-agent timeout logic in run_agent().

All tests use very short timeouts (≤0.05s) and mock query() so they run in
milliseconds without touching the network or spawning any subprocesses.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from deep_architect.agents.client import make_agent_options, run_agent, run_agent_structured
from deep_architect.config import AgentConfig

_FAKE_CLI = "/usr/bin/true"


def _fake_result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        is_error=False,
        duration_ms=100,
        duration_api_ms=100,
        num_turns=1,
        session_id="sess-test",
        result="done",
    )


async def _hanging_query(**_kwargs: object) -> AsyncIterator[object]:
    """Async generator that hangs indefinitely — triggers asyncio.wait_for cancellation."""
    await asyncio.sleep(9999)
    yield  # never reached


async def _fast_query(**_kwargs: object) -> AsyncIterator[object]:
    """Async generator that immediately yields a successful ResultMessage."""
    yield _fake_result()


# ---------------------------------------------------------------------------
# Timeout triggers retry with fresh session
# ---------------------------------------------------------------------------


async def test_timeout_triggers_retry() -> None:
    """asyncio.TimeoutError on first attempt causes a retry; second attempt succeeds."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)
    call_count = 0

    async def _hang_then_succeed(**_kwargs: object) -> AsyncIterator[object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(9999)
            yield  # never reached
        else:
            yield _fake_result()

    with patch("deep_architect.agents.client.query", side_effect=_hang_then_succeed):
        result = await run_agent(
            opts, "prompt", label="test", max_retries=1, timeout_seconds=0.01
        )

    assert call_count == 2
    assert result.session_id == "sess-test"


# ---------------------------------------------------------------------------
# Timeout clears resume on retry
# ---------------------------------------------------------------------------


async def test_timeout_clears_resume_on_retry() -> None:
    """When a timeout triggers a retry, the resume field is cleared on the next attempt."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config, "system", allowed_tools=[], cli_path=_FAKE_CLI, resume="prior-session"
    )
    captured_resumes: list[str | None] = []

    async def _record_and_maybe_hang(**kwargs: object) -> AsyncIterator[object]:
        captured_resumes.append(kwargs.get("options").resume)  # type: ignore[union-attr]
        if len(captured_resumes) == 1:
            await asyncio.sleep(9999)
            yield  # never reached
        else:
            yield _fake_result()

    with patch("deep_architect.agents.client.query", side_effect=_record_and_maybe_hang):
        await run_agent(
            opts, "prompt", label="test", max_retries=1, timeout_seconds=0.01
        )

    assert captured_resumes[0] == "prior-session"
    assert captured_resumes[1] is None


# ---------------------------------------------------------------------------
# Exhausted timeouts raise TimeoutError
# ---------------------------------------------------------------------------


async def test_timeout_exhausts_retries_and_raises() -> None:
    """When all attempts time out, asyncio.TimeoutError is propagated to the caller."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)

    with patch("deep_architect.agents.client.query", side_effect=_hanging_query):
        with pytest.raises(TimeoutError):
            await run_agent(
                opts, "prompt", label="test", max_retries=1, timeout_seconds=0.01
            )


# ---------------------------------------------------------------------------
# No timeout applied when timeout_seconds=None
# ---------------------------------------------------------------------------


async def test_no_timeout_when_none() -> None:
    """With timeout_seconds=None, asyncio.wait_for is not used and a fast query succeeds."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)

    with patch("deep_architect.agents.client.query", side_effect=_fast_query):
        result = await run_agent(opts, "prompt", label="test", timeout_seconds=None)

    assert result.session_id == "sess-test"


# ---------------------------------------------------------------------------
# run_agent_structured propagates timeout_seconds
# ---------------------------------------------------------------------------


async def test_run_agent_structured_timeout_triggers_retry() -> None:
    """Timeout in run_agent_structured() retries and succeeds on second attempt."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(
        config, "system", allowed_tools=[], cli_path=_FAKE_CLI,
        output_format={"type": "json_schema", "schema": {"type": "object"}},
    )
    call_count = 0

    async def _hang_then_structured(**_kwargs: object) -> AsyncIterator[object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(9999)
            yield  # never reached
        else:
            yield ResultMessage(
                subtype="success",
                is_error=False,
                duration_ms=50,
                duration_api_ms=50,
                num_turns=1,
                session_id="sess-struct",
                result='{"key": "value"}',
                structured_output={"key": "value"},
            )

    with patch("deep_architect.agents.client.query", side_effect=_hang_then_structured):
        raw = await run_agent_structured(
            opts, "prompt", label="test", max_retries=1, timeout_seconds=0.01
        )

    assert call_count == 2
    assert raw == {"key": "value"}


# ---------------------------------------------------------------------------
# AgentConfig.agent_timeout_seconds default
# ---------------------------------------------------------------------------


def test_agent_config_default_timeout() -> None:
    """AgentConfig.agent_timeout_seconds class-level default is None.

    Role-specific defaults (3600s for generator, 1800s for critic) are applied
    by HarnessConfig._apply_role_defaults, not by the field default itself.
    """
    config = AgentConfig(model="sonnet")
    assert config.agent_timeout_seconds is None


def test_agent_config_timeout_can_be_none() -> None:
    """agent_timeout_seconds can be set to None to disable the timeout."""
    config = AgentConfig(model="sonnet", agent_timeout_seconds=None)
    assert config.agent_timeout_seconds is None


def test_default_generator_timeout_is_60_minutes() -> None:
    """The default generator config uses a 60-minute timeout."""
    from deep_architect.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.generator.agent_timeout_seconds == 3600.0


def test_default_critic_timeout_is_30_minutes() -> None:
    """The default critic config uses a 30-minute timeout."""
    from deep_architect.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.critic.agent_timeout_seconds == 1800.0


# ---------------------------------------------------------------------------
# CancelledError from SDK cleanup converted to TimeoutError (regression tests)
#
# The claude-agent-sdk's query.close() cleanup acquires an anyio lock via
# checkpoint_if_cancelled, which raises a fresh CancelledError from anyio's
# own cancel scope.  That CancelledError has a different cancel_id than the
# one asyncio.wait_for registered, so wait_for does NOT convert it to
# TimeoutError — it escapes.  CancelledError is a BaseException, not
# Exception, so neither of _consume_query's except clauses catches it.
# The fix: explicitly catch CancelledError inside the wait_for block and
# convert to TimeoutError when the outer task has no pending external cancels.
# ---------------------------------------------------------------------------


async def _hangs_then_raises_cancelled_on_cleanup(**_kwargs: object) -> AsyncIterator[object]:
    """Generator that hangs, then raises a fresh CancelledError in cleanup.

    This mimics the claude-agent-sdk's query.close() path: anyio's
    checkpoint_if_cancelled re-raises with a new "cancel scope" message,
    bypassing asyncio.wait_for's TimeoutError conversion.
    """
    try:
        await asyncio.sleep(9999)
        yield  # never reached
    except asyncio.CancelledError:
        raise asyncio.CancelledError("Cancelled by cancel scope 0xdeadbeef") from None


async def test_sdk_cleanup_cancelled_error_is_retried() -> None:
    """CancelledError from SDK cleanup on timeout is converted to TimeoutError, enabling retry."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)
    call_count = 0

    async def _bad_cleanup_then_succeed(**_kwargs: object) -> AsyncIterator[object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            async for msg in _hangs_then_raises_cancelled_on_cleanup():
                yield msg
        else:
            yield _fake_result()

    with patch("deep_architect.agents.client.query", side_effect=_bad_cleanup_then_succeed):
        result = await run_agent(
            opts, "prompt", label="test", max_retries=1, timeout_seconds=0.01
        )

    assert call_count == 2
    assert result.session_id == "sess-test"


async def test_sdk_cleanup_cancelled_error_exhausted_raises_timeout_error() -> None:
    """When all retries see cleanup CancelledError, TimeoutError (not CancelledError) is raised."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)

    with patch(
        "deep_architect.agents.client.query",
        side_effect=_hangs_then_raises_cancelled_on_cleanup,
    ):
        with pytest.raises(TimeoutError):
            await run_agent(
                opts, "prompt", label="test", max_retries=1, timeout_seconds=0.01
            )


async def test_external_cancellation_not_misclassified_as_timeout() -> None:
    """An external Task.cancel() must propagate CancelledError, not be swallowed as TimeoutError."""
    config = AgentConfig(model="test-model", max_turns=5)
    opts = make_agent_options(config, "system", allowed_tools=[], cli_path=_FAKE_CLI)

    async def _run() -> object:
        with patch("deep_architect.agents.client.query", side_effect=_hanging_query):
            return await run_agent(
                opts, "prompt", label="test", timeout_seconds=9999
            )

    loop = asyncio.get_running_loop()
    task = loop.create_task(_run())
    # Cancel the outer task immediately — simulates SIGTERM / KeyboardInterrupt.
    loop.call_soon(task.cancel)

    with pytest.raises(asyncio.CancelledError):
        await task
