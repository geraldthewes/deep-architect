from __future__ import annotations

from pathlib import Path

from deep_researcher.agents.client import (
    make_agent_options,
    run_agent,
    run_agent_text,
)
from deep_researcher.config import AgentConfig
from deep_researcher.models.contract import SprintContract
from deep_researcher.models.feedback import CriticResult
from deep_researcher.prompts import load_prompt
from deep_researcher.sprints import SprintDefinition

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
    session_id: str | None = None,
) -> str | None:
    """Run the Generator for one round. Returns session_id for continuation across rounds."""
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

    prompt = (
        f"## PRD\n{prd_content}\n\n"
        f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
        f"## Working Directory\n{output_dir}\n\n"
        f"## Files to Produce\n"
        + "\n".join(f"- {f}" for f in contract.files_to_produce)
        + f"\n\n{feedback_section}\n"
        "Use the Write tool to create each file in the working directory using absolute paths. "
        "Use the Edit tool for targeted changes when addressing feedback on existing files. "
        "Each file must be a complete, standalone Markdown document."
    )

    system_prompt = load_prompt("generator_system")
    options = make_agent_options(
        config,
        system_prompt,
        allowed_tools=GENERATOR_TOOLS,
        cwd=str(output_dir),
        cli_path=cli_path,
        resume=session_id,
    )

    label = f"Generator sprint={sprint.number} round={round_num}"
    result = await run_agent(options, prompt, label=label)
    return result.session_id


async def propose_contract(
    config: AgentConfig,
    sprint: SprintDefinition,
    prd_content: str,
    *,
    cli_path: str | None = None,
) -> str:
    """Generator proposes a sprint contract. Returns raw JSON string."""
    prompt = load_prompt(
        "contract_proposal",
        prd=prd_content,
        sprint_number=str(sprint.number),
        sprint_name=sprint.name,
        sprint_description=sprint.description,
        primary_files=str(sprint.primary_files),
    )

    system_prompt = load_prompt("generator_system")
    options = make_agent_options(
        config,
        system_prompt,
        allowed_tools=[],  # No tools needed for contract proposal
        cli_path=cli_path,
    )

    return await run_agent_text(options, prompt, label=f"Generator contract sprint={sprint.number}")
