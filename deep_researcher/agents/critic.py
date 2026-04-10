from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from deep_researcher.agents.client import (
    make_agent_options,
    run_agent_structured,
    run_agent_text,
)
from deep_researcher.config import AgentConfig
from deep_researcher.models.contract import SprintContract
from deep_researcher.models.feedback import CriticResult, PingPongResult
from deep_researcher.prompts import load_prompt

# Tools the critic can use: read-only inspection (no Write/Edit)
CRITIC_TOOLS = ["Read", "Bash", "Glob", "Grep"]


def _json_schema_format(model_class: type[BaseModel]) -> dict[str, Any]:
    """Build an output_format dict from a Pydantic model's JSON schema."""
    return {
        "type": "json_schema",
        "schema": model_class.model_json_schema(),
    }


async def review_contract(
    config: AgentConfig,
    proposal_json: str,
    *,
    cli_path: str | None = None,
) -> str:
    """Critic reviews the proposed contract. Returns 'APPROVED' or revised JSON."""
    prompt = load_prompt("contract_review", contract_json=proposal_json)
    system_prompt = load_prompt("critic_system")
    options = make_agent_options(
        config,
        system_prompt,
        allowed_tools=[],  # No tools needed for contract review
        cli_path=cli_path,
    )
    result = await run_agent_text(options, prompt)
    return result.strip()


async def run_critic(
    config: AgentConfig,
    contract: SprintContract,
    output_dir: Path,
    round_num: int,
    *,
    cli_path: str | None = None,
) -> CriticResult:
    """Run the Critic against the current architecture files."""
    files_list = "\n".join(f"- {f}" for f in contract.files_to_produce)
    prompt = (
        f"Evaluate the architecture files in {output_dir} against the sprint contract.\n\n"
        f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
        f"## Files to Evaluate\n{files_list}\n\n"
        f"This is Round {round_num}. Use Read, Glob, and Grep to inspect each file. "
        "Score every criterion in the contract. "
        "Return a CriticResult JSON object."
    )

    system_prompt = load_prompt("critic_system")
    options = make_agent_options(
        config,
        system_prompt,
        allowed_tools=CRITIC_TOOLS,
        cwd=str(output_dir),
        cli_path=cli_path,
        output_format=_json_schema_format(CriticResult),
    )

    raw = await run_agent_structured(options, prompt)
    return CriticResult.model_validate(raw)


async def check_ping_pong(
    config: AgentConfig,
    current: CriticResult,
    previous: CriticResult,
    *,
    cli_path: str | None = None,
) -> PingPongResult:
    """Use critic LLM to detect ping-pong / diminishing returns."""
    prompt = load_prompt(
        "ping_pong_check",
        previous_summary=previous.overall_summary,
        current_summary=current.overall_summary,
    )

    system_prompt = load_prompt("critic_system")
    options = make_agent_options(
        config,
        system_prompt,
        allowed_tools=[],  # No tools needed for ping-pong check
        cli_path=cli_path,
        output_format=_json_schema_format(PingPongResult),
    )

    raw = await run_agent_structured(options, prompt)
    return PingPongResult.model_validate(raw)
