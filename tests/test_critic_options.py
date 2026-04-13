"""Tests that run_critic builds options without output_format.

CLI 2.1.104+ breaks when --json-schema is used with the LiteLTM proxy.
The StructuredOutput tool injected by --json-schema causes invalid_request on
the initial API call. The critic system prompt already provides JSON format
instructions, so output_format is not needed.
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


async def test_run_critic_does_not_use_output_format(tmp_path: Path) -> None:
    """run_critic must not pass output_format to the CLI agent options.

    output_format → --json-schema flag → StructuredOutput tool injected into
    API request → LiteLTM returns invalid_request for unknown StructuredOutput tool.

    Currently FAILS because critic.py passes output_format=json_schema_format(CriticResult).
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

    assert captured["output_format"] is None, (
        "Critic must not use output_format (--json-schema): "
        "breaks with Claude Code CLI 2.1.104 + LiteLTM proxy. "
        "The critic_system.md already instructs JSON format."
    )
