from __future__ import annotations

from pathlib import Path

from deep_researcher.agents.client import (
    json_schema_format,
    make_agent_options,
    run_agent_structured,
)
from deep_researcher.config import AgentConfig
from deep_researcher.models.contract import ContractReviewResult, SprintContract
from deep_researcher.models.feedback import CriticResult, PingPongResult
from deep_researcher.prompts import load_prompt

# Tools the critic can use: read-only inspection (no Write/Edit)
CRITIC_TOOLS = ["Read", "Bash", "Glob", "Grep"]


async def review_contract(
    config: AgentConfig,
    proposal: SprintContract,
    *,
    cli_path: str | None = None,
) -> ContractReviewResult:
    """Critic reviews the proposed contract.

    Uses structured output to bypass the agentic tool-call loop — the model
    is forced onto the JSON schema path and cannot generate criterion-named
    tool calls.
    """
    prompt = load_prompt("contract_review", contract_json=proposal.model_dump_json(indent=2))
    system_prompt = load_prompt("contract_system")
    options = make_agent_options(
        config,
        system_prompt,
        allowed_tools=[],
        cli_path=cli_path,
        output_format=json_schema_format(ContractReviewResult),
    )
    raw = await run_agent_structured(options, prompt, label="Critic contract-review")
    return ContractReviewResult.model_validate(raw)


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
        output_format=json_schema_format(CriticResult),
    )

    label = f"Critic sprint={contract.sprint_number} round={round_num}"
    raw = await run_agent_structured(options, prompt, label=label)
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
        output_format=json_schema_format(PingPongResult),
    )

    raw = await run_agent_structured(options, prompt, label="Critic ping-pong-check")
    return PingPongResult.model_validate(raw)
