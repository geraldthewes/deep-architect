from __future__ import annotations

from pathlib import Path

from deep_researcher.agents.client import (
    json_schema_format,
    make_agent_options,
    run_agent_structured,
    run_simple_structured,
)
from deep_researcher.config import AgentConfig
from deep_researcher.logger import get_logger
from deep_researcher.models.contract import ContractReviewResult, SprintContract
from deep_researcher.models.feedback import CriticResult, PingPongResult
from deep_researcher.prompts import load_prompt

_log = get_logger(__name__)

# Tools the critic can use: read-only inspection (no Write/Edit)
CRITIC_TOOLS = ["Read", "Bash", "Glob", "Grep"]


async def review_contract(
    config: AgentConfig,
    proposal: SprintContract,
    *,
    cli_path: str | None = None,  # unused; kept for API compatibility
) -> ContractReviewResult:
    """Critic reviews the proposed contract via pydantic-ai (no agentic loop)."""
    prompt = load_prompt("contract_review", contract_json=proposal.model_dump_json(indent=2))
    system_prompt = load_prompt("contract_system")
    result = await run_simple_structured(
        config, system_prompt, prompt, ContractReviewResult, label="Critic contract-review"
    )
    verdict = "approved" if result.approved else "revised"
    _log.info("Contract review verdict: %s", verdict)
    return result


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
    raw = await run_agent_structured(
        options, prompt, label=label,
        max_retries=config.max_agent_retries,
        context_window=config.context_window,
    )
    result = CriticResult.model_validate(raw)
    _log.info(
        "[Critic sprint=%d round=%d] avg=%.1f passed=%s criteria=%d",
        contract.sprint_number, round_num,
        result.average_score, result.passed, len(result.feedback),
    )
    return result


async def check_ping_pong(
    config: AgentConfig,
    current: CriticResult,
    previous: CriticResult,
    *,
    cli_path: str | None = None,  # unused; kept for API compatibility
) -> PingPongResult:
    """Detect ping-pong / diminishing returns via pydantic-ai (no agentic loop)."""
    prompt = load_prompt(
        "ping_pong_check",
        previous_summary=previous.overall_summary,
        current_summary=current.overall_summary,
    )
    system_prompt = load_prompt("critic_system")
    return await run_simple_structured(
        config, system_prompt, prompt, PingPongResult, label="Critic ping-pong-check"
    )
