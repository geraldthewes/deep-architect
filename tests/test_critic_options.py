"""Tests that run_critic builds options with output_format set.

output_format → --json-schema flag → injects StructuredOutput tool the model must
call to terminate, enforcing JSON output at the protocol level rather than relying
on prompt instructions alone.

CLI 2.1.104 broke this (LiteLTM proxy returned invalid_request for the injected
StructuredOutput tool). It was temporarily removed. CLI 2.1.107 confirmed working
again via scripts/test_output_format.py --approach A.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from deep_architect.agents.critic import run_critic
from deep_architect.config import AgentConfig
from deep_architect.models.contract import SprintContract, SprintCriterion


def _make_contract() -> SprintContract:
    return SprintContract(
        sprint_number=1,
        sprint_name="Test Sprint",
        criteria=[
            SprintCriterion(name="quality", description="Good quality"),
            SprintCriterion(name="completeness", description="All files present"),
            SprintCriterion(name="clarity", description="Diagrams are clear"),
        ],
        files_to_produce=["c2.md"],
    )


_MOCK_CRITIC_RESULT = {
    "scores": {"quality": 8.0, "completeness": 7.0, "clarity": 8.5},
    "feedback": [
        {"criterion": "quality", "score": 8.0, "severity": "Medium", "details": "ok"},
        {"criterion": "completeness", "score": 7.0, "severity": "Medium", "details": "ok"},
        {"criterion": "clarity", "score": 8.5, "severity": "Low", "details": "ok"},
    ],
    "overall_summary": "Looks reasonable",
}


async def test_run_critic_uses_output_format(tmp_path: Path) -> None:
    """run_critic must pass output_format to enforce structured JSON output.

    output_format → --json-schema flag → StructuredOutput tool injected into
    the CLI session. The model must call this tool to end its session, so JSON
    output is enforced at the protocol level rather than by prompt instruction.

    Confirmed working with CLI 2.1.107 + LiteLTM proxy via
    scripts/test_output_format.py --approach A.
    """
    captured: dict[str, object] = {}

    async def mock_run_agent_structured(  # type: ignore[no-untyped-def]
        options, prompt, **kwargs
    ) -> dict[str, object]:
        captured["output_format"] = options.output_format
        return _MOCK_CRITIC_RESULT

    config = AgentConfig(model="sonnet", max_turns=10, max_agent_retries=0)
    contract = _make_contract()

    with patch(
        "deep_architect.agents.critic.run_agent_structured",
        side_effect=mock_run_agent_structured,
    ):
        await run_critic(config, contract, tmp_path, round_num=1, cli_path="/usr/bin/true")

    assert captured["output_format"] is not None, (
        "Critic must use output_format (--json-schema) to enforce JSON output at the "
        "protocol level. CLI 2.1.107 + LiteLTM confirmed working."
    )
    assert captured["output_format"].get("type") == "json_schema", (
        "output_format must be a json_schema dict"
    )
