from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from deep_researcher.models.contract import SprintContract
from deep_researcher.models.feedback import CriticResult, PingPongResult

CRITIC_SYSTEM_PROMPT = """You are a hostile senior architect. Your job is to ruthlessly
critique C4 architecture documents before they reach production. Be exhaustive and specific.

## Scoring Guidelines
- 9-10: Exceptional. Handles all edge cases, complete, no gaps.
- 7-8: Good. Minor issues only.
- 5-6: Partial. Significant gaps.
- 3-4: Poor. Fundamental issues.
- 1-2: Failed. Not implemented or broken.

## Severity Rules
- Critical: Fundamental flaw causing production failures or misrepresents the system
- High: Significant gap causing serious problems or missing key relationships
- Medium: Notable issue that should be addressed
- Low: Minor improvement opportunity

## Rules
- Do NOT be generous. Resist the urge to praise mediocre work.
- Include file:line references in your feedback where possible.
- Test EVERY criterion in the contract.
- If a Mermaid diagram has syntax errors, mark it Critical.
- If relationships between containers are missing or wrong, mark it High.
"""

CONTRACT_REVIEW_PROMPT = """Review this proposed sprint contract. Make criteria more specific,
add adversarial edge cases, and raise thresholds where needed.

If the contract is sufficiently rigorous, output exactly: APPROVED

Otherwise output a revised JSON contract with the same structure.
Output ONLY "APPROVED" or the revised JSON — nothing else.

## Proposed Contract
{contract_json}"""

PING_PONG_PROMPT = """Compare these two rounds of critic feedback.
Estimate the semantic similarity (0.0 = completely different, 1.0 = identical issues).

## Previous Round Feedback
{previous_summary}

## Current Round Feedback
{current_summary}

Output a JSON object: {{"similarity_score": <float>, "reasoning": "<brief explanation>"}}"""


async def review_contract(
    agent: Agent[None, str],
    proposal_json: str,
) -> str:
    """Critic reviews the proposed contract. Returns 'APPROVED' or revised JSON."""
    prompt = CONTRACT_REVIEW_PROMPT.format(contract_json=proposal_json)
    result = await agent.run(prompt)
    return result.output.strip()


async def run_critic(
    agent: Agent[None, CriticResult],
    contract: SprintContract,
    output_dir: Path,
    round_num: int,
) -> CriticResult:
    """Run the Critic against the current architecture files."""
    file_contents = []
    for fname in contract.files_to_produce:
        fpath = output_dir / fname
        if fpath.exists():
            content = fpath.read_text()
            file_contents.append(f"### {fname}\n```markdown\n{content}\n```")
        else:
            file_contents.append(f"### {fname}\n[FILE NOT FOUND]")

    files_section = "\n\n".join(file_contents)
    prompt = (
        f"Evaluate these architecture files against the sprint contract.\n\n"
        f"## Sprint Contract\n{contract.model_dump_json(indent=2)}\n\n"
        f"## Architecture Files (Round {round_num})\n\n{files_section}\n\n"
        "Score each criterion. Return a CriticResult JSON object."
    )

    result = await agent.run(prompt)
    return result.output


async def check_ping_pong(
    agent: Agent[None, PingPongResult],
    current: CriticResult,
    previous: CriticResult,
) -> PingPongResult:
    """Use critic LLM to detect ping-pong / diminishing returns."""
    prompt = PING_PONG_PROMPT.format(
        previous_summary=previous.overall_summary,
        current_summary=current.overall_summary,
    )
    result = await agent.run(prompt)
    return result.output
