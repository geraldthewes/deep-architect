from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from deep_architect.agents.client import (
    extract_input_tokens,
    make_agent_options,
    run_agent,
    run_simple_structured,
)
from deep_architect.config import AgentConfig
from deep_architect.logger import get_logger
from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult
from deep_architect.prompts import load_prompt
from deep_architect.sprints import SprintDefinition

_log = get_logger(__name__)


@dataclass
class GeneratorRoundResult:
    """Result of one generator round."""

    session_id: str | None
    input_tokens: int

# Tools the generator can use: full agentic access to write and inspect files
GENERATOR_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


async def run_generator(
    config: AgentConfig,
    sprint: SprintDefinition,
    contract: SprintContract,
    prd_content: str,
    previous_feedback: CriticResult | None,
    output_dir: Path,
    round_num: int,
    *,
    cli_path: str | None = None,
    supplementary_context: str = "",
) -> GeneratorRoundResult:
    """Run the Generator for one round."""
    _log.info("[Generator sprint=%d round=%d] Starting new session", sprint.number, round_num)

    if previous_feedback:
        _log.info(
            "[Generator sprint=%d round=%d] Feedback included: avg=%.1f, %d items",
            sprint.number, round_num,
            previous_feedback.average_score, len(previous_feedback.feedback),
        )

    feedback_section = ""
    if previous_feedback:
        feedback_lines = "\n".join(
            f"- [{f.severity}] {f.criterion} ({f.score}/10): {f.details}"
            for f in previous_feedback.feedback
        )
        feedback_section = (
            f"\n## Critic Feedback from Round {round_num - 1} (MUST ADDRESS ALL ISSUES)\n\n"
            f"Average Score: {previous_feedback.average_score:.1f}/10\n"
            f"{feedback_lines}\n\n"
            f"Overall: {previous_feedback.overall_summary}\n"
        )

    learnings_section = ""
    learnings_path = output_dir / "generator-learnings.md"
    if learnings_path.exists():
        learnings_content = learnings_path.read_text().strip()
        if learnings_content:
            learnings_section = f"\n## Prior Learnings\n{learnings_content}\n"
            _log.info(
                "[Generator sprint=%d round=%d] Loaded learnings (%d chars)",
                sprint.number, round_num, len(learnings_content),
            )

    context_section = (
        f"## Supplementary Context\n{supplementary_context}\n\n"
        if supplementary_context else ""
    )

    history_section = ""
    history_path = output_dir / "generator-history.md"
    if history_path.exists():
        history_section = (
            f"\n## Round History\n"
            f"Your prior work is recorded at `{history_path}`. "
            f"Use Read or Grep to search it by sprint number, round number, "
            f"or filename. Do NOT write to this file.\n"
        )

    prompt = (
        f"## PRD\n{prd_content}\n\n"
        f"{context_section}"
        f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
        f"## Working Directory\n{output_dir}\n\n"
        f"## Files to Produce\n"
        + "\n".join(f"- {f}" for f in contract.files_to_produce)
        + f"\n\n{feedback_section}{learnings_section}{history_section}\n"
        "Use the Write tool to create each file in the working directory using absolute paths. "
        "Use the Edit tool for targeted changes when addressing feedback on existing files. "
        "Each file must be a complete, standalone Markdown document."
    )

    system_prompt = (
        load_prompt("generator_system")
        + "\n\n"
        + load_prompt("c4_skill")
        + "\n\n"
        + load_prompt("mermaid_c4_guide")
    )
    options = make_agent_options(
        config,
        system_prompt,
        allowed_tools=GENERATOR_TOOLS,
        cwd=str(output_dir),
        cli_path=cli_path,
    )

    label = f"Generator sprint={sprint.number} round={round_num}"
    result = await run_agent(
        options, prompt, label=label,
        max_retries=config.max_agent_retries,
        context_window=config.context_window,
        timeout_seconds=config.agent_timeout_seconds,
    )
    return GeneratorRoundResult(
        session_id=result.session_id,
        input_tokens=extract_input_tokens(result),
    )


async def propose_contract(
    config: AgentConfig,
    sprint: SprintDefinition,
    prd_content: str,
    *,
    cli_path: str | None = None,  # unused; kept for API compatibility
    supplementary_context: str = "",
) -> SprintContract:
    """Generator proposes a sprint contract via pydantic-ai (no agentic loop)."""
    prompt = load_prompt(
        "contract_proposal",
        prd=prd_content,
        sprint_number=str(sprint.number),
        sprint_name=sprint.name,
        sprint_description=sprint.description,
        primary_files=str(sprint.primary_files),
    )
    if supplementary_context:
        prompt += f"\n\n## Supplementary Context\n{supplementary_context}\n"
    system_prompt = load_prompt("contract_system")
    label = f"Generator contract sprint={sprint.number}"
    return await run_simple_structured(config, system_prompt, prompt, SprintContract, label=label)
