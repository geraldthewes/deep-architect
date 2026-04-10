from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from rich.console import Console

from deep_researcher.agents.client import init_run_stats, make_agent_options, run_agent_text
from deep_researcher.agents.critic import check_ping_pong, review_contract, run_critic
from deep_researcher.agents.generator import propose_contract, run_generator
from deep_researcher.config import AgentConfig, HarnessConfig
from deep_researcher.exit_criteria import should_ping_pong_exit, sprint_passes
from deep_researcher.git_ops import get_modified_files, git_commit, validate_git_repo
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
from deep_researcher.models.feedback import CriticResult
from deep_researcher.models.progress import HarnessProgress, SprintStatus
from deep_researcher.prompts import load_prompt
from deep_researcher.sprints import SPRINTS, SprintDefinition

logger = get_logger(__name__)
console = Console()


async def negotiate_contract(
    generator_config: AgentConfig,
    critic_config: AgentConfig,
    sprint: SprintDefinition,
    prd_content: str,
    *,
    cli_path: str | None = None,
) -> SprintContract:
    """Generator proposes, Critic tightens. Returns final locked contract."""
    logger.info(f"[Sprint {sprint.number}] Negotiating contract...")
    logger.info(f"[Sprint {sprint.number}] Generator proposing contract...")
    proposal = await propose_contract(generator_config, sprint, prd_content, cli_path=cli_path)
    logger.info(f"[Sprint {sprint.number}] Critic reviewing contract...")
    review = await review_contract(critic_config, proposal, cli_path=cli_path)
    approved = review.upper().startswith("APPROVED")
    verdict = "approved as-is" if approved else "revised by critic"
    logger.info(f"[Sprint {sprint.number}] Contract {verdict}")
    final_json = proposal if approved else review

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
    generator_config: AgentConfig,
    critic_config: AgentConfig,
    output_dir: Path,
    *,
    cli_path: str | None = None,
) -> None:
    """Both agents must independently output READY_TO_SHIP."""
    logger.info("Running final mutual agreement round...")

    final_prompt = load_prompt("final_agreement", output_dir=str(output_dir))

    gen_options = make_agent_options(
        generator_config,
        load_prompt("generator_system"),
        allowed_tools=["Read", "Glob", "Grep"],
        cwd=str(output_dir),
        cli_path=cli_path,
    )
    critic_options = make_agent_options(
        critic_config,
        load_prompt("critic_system"),
        allowed_tools=["Read", "Glob", "Grep"],
        cwd=str(output_dir),
        cli_path=cli_path,
    )

    gen_result = await run_agent_text(
        gen_options, final_prompt, label="Generator final-agreement"
    )
    critic_result = await run_agent_text(
        critic_options, final_prompt, label="Critic final-agreement"
    )

    gen_ready = "READY_TO_SHIP" in gen_result
    critic_ready = "READY_TO_SHIP" in critic_result

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

    # Log Anthropic environment configuration (mask secrets)
    anthropic_vars = [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ]
    secret_vars = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"]
    for var in anthropic_vars:
        val = os.environ.get(var)
        logger.info(f"  {var}={val if val is not None else '(not set)'}")
    for var in secret_vars:
        val = os.environ.get(var)
        logger.info(f"  {var}={'(set)' if val else '(not set)'}")

    run_stats = init_run_stats()

    repo = validate_git_repo(output_dir)
    init_workspace(output_dir)
    prd_content = prd.read_text()

    cli_path = config.cli_path

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
        contract = await negotiate_contract(
            config.generator, config.critic, sprint, prd_content, cli_path=cli_path
        )
        save_contract(output_dir, contract)

        sprint_status = progress.sprint_statuses[sprint.number - 1]
        sprint_status.status = "building"
        progress.current_sprint = sprint.number
        save_progress(output_dir, progress)

        last_result: CriticResult | None = None
        consecutive_passes = 0
        generator_session_id: str | None = None  # persist context across rounds per sprint

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

            # Generator builds — writes files directly via tool use
            sprint_status.status = "building"
            save_progress(output_dir, progress)
            generator_session_id = await run_generator(
                config.generator,
                sprint,
                contract,
                prd_content,
                last_result,
                output_dir,
                round_num,
                cli_path=cli_path,
                session_id=generator_session_id,
            )

            # Detect files written by the generator and auto-commit
            written = get_modified_files(repo)
            git_commit(
                repo,
                f"Generator pass {round_num} - sprint {sprint.number} ({sprint.name})",
                written,
            )

            # Critic evaluates — reads files via tool use
            sprint_status.status = "evaluating"
            save_progress(output_dir, progress)
            result = await run_critic(
                config.critic, contract, output_dir, round_num, cli_path=cli_path
            )
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
                pp = await check_ping_pong(
                    config.critic, result, last_result, cli_path=cli_path
                )
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
    await run_final_agreement(config.generator, config.critic, output_dir, cli_path=cli_path)
    progress.status = "complete"
    save_progress(output_dir, progress)
    logger.info("Harness COMPLETE — architecture is production-ready")
    run_stats.log_summary()
