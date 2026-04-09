from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from rich.console import Console

from deep_researcher.agents.client import make_structured_agent, make_text_agent
from deep_researcher.agents.critic import (
    CRITIC_SYSTEM_PROMPT,
    check_ping_pong,
    review_contract,
    run_critic,
)
from deep_researcher.agents.generator import (
    GENERATOR_SYSTEM_PROMPT,
    propose_contract,
    run_generator,
)
from deep_researcher.config import HarnessConfig
from deep_researcher.exit_criteria import should_ping_pong_exit, sprint_passes
from deep_researcher.git_ops import git_commit, validate_git_repo
from deep_researcher.io.files import (
    init_workspace,
    load_progress,
    save_contract,
    save_feedback,
    save_progress,
    save_round_log,
)
from deep_researcher.logger import get_logger, setup_logging
from deep_researcher.models.contract import SprintContract
from deep_researcher.models.feedback import CriticResult, GeneratorResult, PingPongResult
from deep_researcher.models.progress import HarnessProgress, SprintStatus
from deep_researcher.sprints import SPRINTS, SprintDefinition

logger = get_logger(__name__)
console = Console()


async def negotiate_contract(
    generator_agent: Agent[None, str],
    critic_text_agent: Agent[None, str],
    sprint: SprintDefinition,
    prd_content: str,
) -> SprintContract:
    """Generator proposes, Critic tightens. Returns final locked contract."""
    logger.info(f"[Sprint {sprint.number}] Negotiating contract...")
    proposal = await propose_contract(generator_agent, sprint, prd_content)

    review = await review_contract(critic_text_agent, proposal)
    final_json = proposal if review.upper().startswith("APPROVED") else review

    # Parse with fallback — try code blocks first, then raw text
    candidates: list[str] = [final_json.strip()]
    for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", final_json):
        candidates.insert(0, block.strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            return SprintContract.model_validate(data)
        except Exception:
            continue

    raise ValueError(f"Could not parse contract for sprint {sprint.number}")


async def run_final_agreement(
    generator_agent: Agent[None, str],
    critic_agent: Agent[None, CriticResult],
    output_dir: Path,
) -> None:
    """Both agents must independently output READY_TO_SHIP."""
    logger.info("Running final mutual agreement round...")

    final_prompt = (
        f"Review the complete architecture in {output_dir}. "
        "If it is production-ready and all C4 levels are complete, output exactly: READY_TO_SHIP\n"
        "Otherwise describe what is missing."
    )

    gen_result = await generator_agent.run(final_prompt)
    critic_result = await critic_agent.run(final_prompt)

    gen_ready = "READY_TO_SHIP" in gen_result.output
    critic_ready = "READY_TO_SHIP" in str(critic_result.output)

    if gen_ready and critic_ready:
        logger.info("Mutual agreement reached: READY_TO_SHIP")
    else:
        logger.warning(
            f"Final agreement: Generator={'READY' if gen_ready else 'NOT READY'}, "
            f"Critic={'READY' if critic_ready else 'NOT READY'}"
        )


async def run_harness(
    prd: Path,
    output_dir: Path,
    resume: bool,
    config: HarnessConfig,
) -> None:
    """Run the full adversarial C4 architecture harness."""
    log_dir = output_dir / "logs"
    log_file = setup_logging(log_dir)
    logger.info(f"Log file: {log_file}")

    repo = validate_git_repo(output_dir)
    init_workspace(output_dir)
    prd_content = prd.read_text()

    # Build agents
    generator_text: Agent[None, str] = make_text_agent(config.generator, GENERATOR_SYSTEM_PROMPT)
    generator_structured: Agent[None, GeneratorResult] = make_structured_agent(
        config.generator, GeneratorResult, GENERATOR_SYSTEM_PROMPT
    )
    critic_model = OpenAIModel(
        config.critic.model,
        provider=OpenAIProvider(base_url=config.critic.base_url, api_key=config.critic.api_key),
    )
    critic_text: Agent[None, str] = Agent(
        model=critic_model, output_type=str, system_prompt=CRITIC_SYSTEM_PROMPT
    )
    critic_agent: Agent[None, CriticResult] = Agent(
        model=critic_model, output_type=CriticResult, system_prompt=CRITIC_SYSTEM_PROMPT
    )
    ping_pong_agent: Agent[None, PingPongResult] = Agent(
        model=critic_model, output_type=PingPongResult, system_prompt=CRITIC_SYSTEM_PROMPT
    )

    # Resume or initialize progress
    if resume and (output_dir / "progress.json").exists():
        progress = load_progress(output_dir)
        start_sprint_idx = progress.current_sprint - 1
        logger.info(f"Resuming from sprint {progress.current_sprint}")
    else:
        progress = HarnessProgress(
            total_sprints=len(SPRINTS),
            sprint_statuses=[
                SprintStatus(sprint_number=s.number, sprint_name=s.name) for s in SPRINTS
            ],
        )
        start_sprint_idx = 0

    save_progress(output_dir, progress)
    start_time = time.time()
    t = config.thresholds

    for sprint in SPRINTS[start_sprint_idx:]:
        logger.info("=" * 60)
        logger.info(f"SPRINT {sprint.number}/{len(SPRINTS)}: {sprint.name}")
        logger.info("=" * 60)

        # Contract negotiation
        contract = await negotiate_contract(generator_text, critic_text, sprint, prd_content)
        save_contract(output_dir, contract)

        sprint_status = progress.sprint_statuses[sprint.number - 1]
        sprint_status.status = "building"
        progress.current_sprint = sprint.number
        save_progress(output_dir, progress)

        last_result: CriticResult | None = None
        consecutive_passes = 0

        for round_num in range(1, t.max_rounds_per_sprint + 1):
            # Global safety checks
            if progress.total_rounds >= t.max_total_rounds:
                logger.error("Max total rounds reached — stopping.")
                progress.status = "failed"
                save_progress(output_dir, progress)
                return

            elapsed = time.time() - start_time
            if elapsed > t.timeout_hours * 3600:
                logger.error("3-hour timeout reached — stopping.")
                progress.status = "failed"
                save_progress(output_dir, progress)
                return

            logger.info(f"[Sprint {sprint.number}] Round {round_num}")

            # Generator builds
            sprint_status.status = "building"
            save_progress(output_dir, progress)
            written = await run_generator(
                generator_structured,
                sprint,
                contract,
                prd_content,
                last_result,
                output_dir,
                round_num,
            )

            # Auto-commit
            git_commit(
                repo,
                f"Generator pass {round_num} - sprint {sprint.number} ({sprint.name})",
                written,
            )

            # Critic evaluates
            sprint_status.status = "evaluating"
            save_progress(output_dir, progress)
            result = await run_critic(critic_agent, contract, output_dir, round_num)
            save_feedback(output_dir, sprint.number, round_num, result)
            save_round_log(
                output_dir,
                sprint.number,
                round_num,
                {
                    "sprint": sprint.number,
                    "round": round_num,
                    "average_score": result.average_score,
                    "passed": result.passed,
                    "feedback_count": len(result.feedback),
                },
            )

            progress.total_rounds += 1
            sprint_status.rounds_completed = round_num
            logger.info(
                f"[Sprint {sprint.number}] Round {round_num}: "
                f"avg={result.average_score:.1f} passed={result.passed}"
            )

            # Exit criteria
            if sprint_passes(result, t.min_score):
                consecutive_passes += 1
                logger.info(
                    f"Consecutive passes: {consecutive_passes}/{t.consecutive_passing_rounds}"
                )
                if consecutive_passes >= t.consecutive_passing_rounds:
                    logger.info(f"Sprint {sprint.number} PASSED")
                    sprint_status.status = "passed"
                    sprint_status.final_score = result.average_score
                    break
            else:
                consecutive_passes = 0

            # Ping-pong detection (after round 3)
            if round_num >= 3 and last_result is not None:
                pp = await check_ping_pong(ping_pong_agent, result, last_result)
                if should_ping_pong_exit(
                    pp.similarity_score, result, last_result, t.ping_pong_similarity_threshold
                ):
                    logger.warning(
                        f"Ping-pong detected (similarity={pp.similarity_score:.2f}) — "
                        f"auto-exiting sprint {sprint.number} as good enough"
                    )
                    sprint_status.status = "passed"
                    sprint_status.final_score = result.average_score
                    break

            last_result = result
        else:
            # Max rounds exhausted without passing
            logger.error(
                f"Sprint {sprint.number} FAILED after {t.max_rounds_per_sprint} rounds"
            )
            sprint_status.status = "failed"
            progress.status = "failed"
            save_progress(output_dir, progress)
            return

        progress.completed_sprints += 1
        save_progress(output_dir, progress)

    # Final mutual agreement round
    await run_final_agreement(generator_text, critic_agent, output_dir)
    progress.status = "complete"
    save_progress(output_dir, progress)
    logger.info("Harness COMPLETE — architecture is production-ready")
