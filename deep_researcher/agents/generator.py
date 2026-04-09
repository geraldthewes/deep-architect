from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from deep_researcher.models.contract import SprintContract
from deep_researcher.models.feedback import CriticResult, GeneratorResult
from deep_researcher.sprints import SprintDefinition

GENERATOR_SYSTEM_PROMPT = """You are Winston, the BMAD Architect.
Your role is to produce the highest-quality C4 architecture documentation possible.

You will receive a PRD, a sprint contract, and optionally critic feedback from a previous round.

## C4 Diagram Rules
- C1 System Context: use `C4Context` Mermaid block
- C2 Container: use `C4Container` Mermaid block
- Always end diagrams with `UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")`
- Use `_Ext` suffix (System_Ext, Person_Ext) for external systems
- Include technology string in C2: Container(alias, "Name", "Tech", "Description")
- Do NOT use %%{init}%% directives — GitHub ignores them

## Output Rules
- Write complete, standalone Markdown files
- Each file must have a title heading, a brief narrative, the Mermaid diagram,
  and a section describing relationships
- When a Critic round provides feedback, address EVERY specific issue mentioned with file references

## Response Format
Return a JSON object with:
- "files": list of {"path": "<relative path>", "content": "<full markdown content>"}
- "summary": brief description of design decisions made
"""

CONTRACT_PROPOSAL_PROMPT = """You are proposing a sprint contract for the following sprint.

## PRD
{prd}

## Sprint Definition
Sprint {sprint_number}: {sprint_name}
{sprint_description}

Primary files to produce: {primary_files}

Propose a sprint contract as a JSON object with this structure:
{{
  "sprint_number": {sprint_number},
  "sprint_name": "{sprint_name}",
  "files_to_produce": [...],
  "criteria": [
    {{"name": "...", "description": "Specific, testable criterion", "threshold": 9.0}},
    ...
  ]
}}

Include 5-10 criteria. Each must be SPECIFIC and TESTABLE — not vague.
Criteria must cover: Mermaid diagram validity, C4 completeness, narrative quality,
accuracy to PRD, and relationship documentation.
Output ONLY the JSON."""


async def run_generator(
    agent: Agent[None, GeneratorResult],
    sprint: SprintDefinition,
    contract: SprintContract,
    prd_content: str,
    previous_feedback: CriticResult | None,
    output_dir: Path,
    round_num: int,
) -> list[Path]:
    """Run the Generator for one round. Returns list of files written."""
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
        f"## Output Directory (paths relative to this)\n{output_dir}\n"
        f"{feedback_section}\n"
        "Generate the architecture files specified in the contract. "
        "Return a GeneratorResult with the file contents and a brief summary."
    )

    result = await agent.run(prompt)
    generator_result = result.output

    written: list[Path] = []
    for generated_file in generator_result.files:
        path = output_dir / generated_file.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated_file.content)
        written.append(path)

    return written


async def propose_contract(
    agent: Agent[None, str],
    sprint: SprintDefinition,
    prd_content: str,
) -> str:
    """Generator proposes a sprint contract. Returns raw JSON string."""
    prompt = CONTRACT_PROPOSAL_PROMPT.format(
        prd=prd_content,
        sprint_number=sprint.number,
        sprint_name=sprint.name,
        sprint_description=sprint.description,
        primary_files=sprint.primary_files,
    )
    result = await agent.run(prompt)
    return result.output
