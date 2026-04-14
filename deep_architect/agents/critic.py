from __future__ import annotations

import json
from pathlib import Path

from deep_architect.agents.client import (
    json_schema_format,
    make_agent_options,
    run_agent_structured,
    run_simple_structured,
)
from deep_architect.config import AgentConfig
from deep_architect.logger import get_logger
from deep_architect.models.contract import ContractReviewResult, SprintContract
from deep_architect.models.feedback import CriticResult, PingPongResult
from deep_architect.prompts import load_prompt

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
    history_section = ""
    history_path = output_dir / "critic-history.md"
    if history_path.exists():
        history_section = (
            f"\n## Critic History\n"
            f"Prior evaluations are recorded at `{history_path}`. "
            f"Use Read or Grep to check for recurring concerns or score trends. "
            f"Do NOT write to this file.\n"
        )
    prompt = (
        f"Evaluate the architecture files in {output_dir} against the sprint contract.\n\n"
        f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
        f"## Files to Evaluate\n{files_list}\n\n"
        f"This is Round {round_num}. Use Read, Glob, and Grep to inspect each file. "
        "Score every criterion in the contract. "
        f"Return a CriticResult JSON object.{history_section}"
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
    try:
        raw = await run_agent_structured(
            options, prompt, label=label,
            max_retries=config.max_agent_retries,
            context_window=config.context_window,
            timeout_seconds=config.agent_timeout_seconds,
        )
        result = CriticResult.model_validate(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        _log.warning(
            "[%s] structured output failed (%s) — attempting rescue call", label, exc
        )
        result = await _critic_rescue(
            config, contract, output_dir, round_num, system_prompt, label
        )

    _log.info(
        "[Critic sprint=%d round=%d] avg=%.1f passed=%s criteria=%d",
        contract.sprint_number, round_num,
        result.average_score, result.passed, len(result.feedback),
    )
    return result


async def _critic_rescue(
    config: AgentConfig,
    contract: SprintContract,
    output_dir: Path,
    round_num: int,
    system_prompt: str,
    label: str,
) -> CriticResult:
    """Rescue path: read architecture files via Python I/O and make a single structured API call.

    Used when the agentic critic completes its tool-use loop but fails to emit a final
    JSON response (e.g. the session ends after the last tool call with no text turn).
    Reads files directly rather than via agent tools so no subprocess is needed.
    Note: mmdc diagram validation is skipped; Mermaid scores may be less precise.
    """
    file_sections: list[str] = []
    for fname in contract.files_to_produce:
        path = output_dir / fname
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                file_sections.append(f"### {fname}\n```\n{content}\n```")
            except OSError as read_exc:
                _log.warning("[%s] rescue: could not read %s: %s", label, path, read_exc)

    files_text = (
        "\n\n".join(file_sections) if file_sections else "(no architecture files found)"
    )
    rescue_prompt = load_prompt(
        "critic_rescue",
        contract_json=contract.model_dump_json(indent=2),
        files_text=files_text,
        round_num=str(round_num),
    )
    _log.info("[%s] rescue: evaluating %d file(s) via direct API call", label, len(file_sections))
    return await run_simple_structured(
        config, system_prompt, rescue_prompt, CriticResult, label=f"{label}-rescue"
    )


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
