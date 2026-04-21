"""Tests verifying circuit-breaker kwargs are wired through run_generator and run_agent_structured.

These tests guard against the ADR-026 partial-wiring regression where generator.py and
run_agent_structured were missing the circuit_breaker_state / failure_threshold /
base_backoff / max_backoff parameters that harness.py expected to pass.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import BaseModel

from deep_architect.agents.circuit_breaker import CircuitBreakerState
from deep_architect.agents.client import (
    make_agent_options,
    run_agent_structured,
    run_simple_structured,
)
from deep_architect.agents.generator import run_generator
from deep_architect.config import AgentConfig
from deep_architect.models.contract import SprintContract, SprintCriterion
from deep_architect.sprints import SPRINTS

_FAKE_CLI = "/usr/bin/true"


def _make_agent_config() -> AgentConfig:
    return AgentConfig(model="test-model", max_turns=1, max_agent_retries=0)


def _make_contract() -> SprintContract:
    return SprintContract(
        sprint_number=1,
        sprint_name="C1 System Context",
        files_to_produce=["c1-context.md"],
        criteria=[
            SprintCriterion(name="a", description="A criterion"),
            SprintCriterion(name="b", description="B criterion"),
            SprintCriterion(name="c", description="C criterion"),
        ],
    )


# ---------------------------------------------------------------------------
# run_generator circuit-breaker kwargs
# ---------------------------------------------------------------------------


async def test_run_generator_forwards_circuit_breaker_state(tmp_path: Path) -> None:
    """Circuit-breaker kwargs passed to run_generator must reach run_agent."""
    captured: dict[str, object] = {}

    async def _mock_run_agent(options: object, prompt: str, **kwargs: object) -> object:
        captured.update(kwargs)
        result = MagicMock()
        result.session_id = None
        return result

    state = CircuitBreakerState()
    with patch("deep_architect.agents.generator.run_agent", side_effect=_mock_run_agent):
        await run_generator(
            _make_agent_config(),
            SPRINTS[0],
            _make_contract(),
            "# PRD",
            None,
            tmp_path,
            1,
            circuit_breaker_state=state,
            failure_threshold=3,
            base_backoff=2.0,
            max_backoff=30.0,
        )

    assert captured.get("circuit_breaker_state") is state
    assert captured.get("failure_threshold") == 3
    assert captured.get("base_backoff") == 2.0
    assert captured.get("max_backoff") == 30.0


async def test_run_generator_defaults_circuit_breaker_to_none(tmp_path: Path) -> None:
    """run_generator with no circuit-breaker kwargs forwards None to run_agent."""
    captured: dict[str, object] = {}

    async def _mock_run_agent(options: object, prompt: str, **kwargs: object) -> object:
        captured.update(kwargs)
        result = MagicMock()
        result.session_id = None
        return result

    with patch("deep_architect.agents.generator.run_agent", side_effect=_mock_run_agent):
        await run_generator(
            _make_agent_config(),
            SPRINTS[0],
            _make_contract(),
            "# PRD",
            None,
            tmp_path,
            1,
        )

    assert captured.get("circuit_breaker_state") is None


# ---------------------------------------------------------------------------
# run_agent_structured circuit-breaker kwargs
# ---------------------------------------------------------------------------


async def test_run_agent_structured_forwards_circuit_breaker_state() -> None:
    """Circuit-breaker kwargs passed to run_agent_structured must reach run_agent."""
    captured: dict[str, object] = {}

    async def _mock_run_agent(options: object, prompt: str, **kwargs: object) -> object:
        captured.update(kwargs)
        result = MagicMock()
        result.structured_output = {"approved": True}
        result.result = None
        result.session_id = None
        return result

    config = _make_agent_config()
    opts = make_agent_options(config, "sys", allowed_tools=[], cli_path=_FAKE_CLI)
    state = CircuitBreakerState()

    with patch("deep_architect.agents.client.run_agent", side_effect=_mock_run_agent):
        await run_agent_structured(
            opts,
            "prompt",
            circuit_breaker_state=state,
            failure_threshold=4,
            base_backoff=1.5,
            max_backoff=45.0,
        )

    assert captured.get("circuit_breaker_state") is state
    assert captured.get("failure_threshold") == 4
    assert captured.get("base_backoff") == 1.5
    assert captured.get("max_backoff") == 45.0


async def test_run_agent_structured_defaults_circuit_breaker_to_none() -> None:
    """run_agent_structured with no circuit-breaker kwargs forwards None to run_agent."""
    captured: dict[str, object] = {}

    async def _mock_run_agent(options: object, prompt: str, **kwargs: object) -> object:
        captured.update(kwargs)
        result = MagicMock()
        result.structured_output = {"ok": True}
        result.result = None
        result.session_id = None
        return result

    config = _make_agent_config()
    opts = make_agent_options(config, "sys", allowed_tools=[], cli_path=_FAKE_CLI)

    with patch("deep_architect.agents.client.run_agent", side_effect=_mock_run_agent):
        await run_agent_structured(opts, "prompt")

    assert captured.get("circuit_breaker_state") is None


# ---------------------------------------------------------------------------
# run_simple_structured max_tokens cap
# ---------------------------------------------------------------------------


class _Simple(BaseModel):
    value: int


def _make_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    return response


@patch("deep_architect.agents.client._anthropic.AsyncAnthropic")
async def test_run_simple_structured_uses_16384_max_tokens(mock_cls: MagicMock) -> None:
    """run_simple_structured must request max_tokens=16384 to avoid truncating long responses."""
    mock_cls.return_value.messages.create = AsyncMock(
        return_value=_make_response('{"value": 1}')
    )
    config = AgentConfig(model="test-model", max_turns=5)
    await run_simple_structured(config, "sys", "prompt", _Simple, label="test")

    call_kwargs = mock_cls.return_value.messages.create.call_args.kwargs
    assert call_kwargs["max_tokens"] == 16384
