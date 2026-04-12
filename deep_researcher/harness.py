from __future__ import annotations

import os
import time
from pathlib import Path

from rich.console import Console

from deep_researcher.agents.client import init_run_stats, make_agent_options, run_agent_text
from deep_researcher.agents.critic import check_ping_pong, review_contract, run_critic
from deep_researcher.agents.generator import GeneratorRoundResult, propose_contract, run_generator
from deep_researcher.config import AgentConfig, HarnessConfig
from deep_researcher.exit_criteria import should_ping_pong_exit, sprint_passes
from deep_researcher.git_ops import (
    get_modified_files,
    git_commit,
    git_commit_staged,
    restore_arch_files_from_commit,
    validate_git_repo,
)
from deep_researcher.io.files import (
    append_critic_history,
    append_generator_history,
    append_rollback_event,
    init_workspace,
    load_contract,
    load_feedback,
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


def _build_supplementary_context(context_files: list[Path] | None) -> str:
    """Read and assemble supplementary context files into a single prompt section body."""
    if not context_files:
        return ""

    parts: list[str] = [
        "The following supplementary context was provided by the user as binding "
        "constraints. Technology choices, architectural decisions, and other directives "
        "here take precedence over default preferences and must be respected in the "
        "architecture. Treat these as requirements of equal weight to the PRD."
    ]
    for path in context_files:
        parts.append(f"\n### {path.name}\n{path.read_text()}")

    return "\n".join(parts)


async def negotiate_contract(
    generator_config: AgentConfig,
    critic_config: AgentConfig,
    sprint: SprintDefinition,
    prd_content: str,
    output_dir: Path,
    *,
    cli_path: str | None = None,
    supplementary_context: str = "",
) -> SprintContract:
    """Generator proposes, Critic tightens. Returns final locked contract.

    Both operations use structured output (JSON schema) so the model is forced
    onto the schema path and cannot generate criterion-named tool calls.
    The proposal is saved to disk immediately; the critic revision overwrites it.
    """
    logger.info(f"[Sprint {sprint.number}] Negotiating contract...")
    logger.info(f"[Sprint {sprint.number}] Generator proposing contract...")
    contract = await propose_contract(
        generator_config, sprint, prd_content,
        cli_path=cli_path, supplementary_context=supplementary_context,
    )
    save_contract(output_dir, contract)
    logger.info(f"[Sprint {sprint.number}] Contract proposal saved.")

    logger.info(f"[Sprint {sprint.number}] Critic reviewing contract...")
    review = await review_contract(critic_config, contract, cli_path=cli_path)
    verdict = "approved as-is" if review.approved else "revised by critic"
    logger.info(f"[Sprint {sprint.number}] Contract {verdict}")

    if review.approved or review.revised_contract is None:
        return contract

    save_contract(output_dir, review.revised_contract)
    return review.revised_contract


async def run_preflight_check(
    generator_config: AgentConfig,
    critic_config: AgentConfig,
    *,
    cli_path: str | None = None,
) -> None:
    """Send a minimal prompt to each model to verify connectivity before the expensive run.

    Raises RuntimeError if either agent fails to respond.
    """
    logger.info("Running preflight check...")
    prompt = "Reply with exactly one word: OK"

    failures: list[str] = []
    for role, cfg in [("Generator", generator_config), ("Critic", critic_config)]:
        options = make_agent_options(
            cfg,
            "",  # No system prompt needed for a smoke test
            allowed_tools=[],
            cli_path=cli_path,
        )
        try:
            response = await run_agent_text(options, prompt, label=f"Preflight {role}")
            if response.strip():
                logger.info(f"  {role} ({cfg.model}): OK — responded: {response.strip()[:80]}")
            else:
                failures.append(f"{role} ({cfg.model}): empty response")
                logger.error(f"  {role} ({cfg.model}): FAIL — empty response")
        except Exception as exc:
            failures.append(f"{role} ({cfg.model}): {exc}")
            logger.error(f"  {role} ({cfg.model}): FAIL — {exc}")

    if failures:
        raise RuntimeError(
            "Preflight check failed — fix the configuration before re-running:\n"
            + "\n".join(f"  • {f}" for f in failures)
        )
    logger.info("Preflight check passed.")


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
    context_files: list[Path] | None = None,
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
    if repo.working_tree_dir is None:
        raise RuntimeError("Git repository has no working tree directory")
    checkpoint_dir = Path(repo.working_tree_dir) / ".checkpoints"

    # Fail fast before any expensive operations if --resume has no checkpoint
    if resume:
        checkpoint = checkpoint_dir / "progress.json"
        if not checkpoint.exists():
            raise FileNotFoundError(
                f"--resume passed but no checkpoint found at {checkpoint}. "
                "Run without --resume to start a fresh run, or restore a prior checkpoint."
            )

    init_workspace(output_dir)
    prd_content = prd.read_text()
    supplementary_context = _build_supplementary_context(context_files)
    if supplementary_context:
        logger.info(
            "Supplementary context: %d file(s), %d chars",
            len(context_files) if context_files else 0, len(supplementary_context),
        )

    cli_path = config.cli_path

    await run_preflight_check(config.generator, config.critic, cli_path=cli_path)

    # Resume or initialize progress
    if resume:
        progress = load_progress(checkpoint_dir)
        progress.status = "running"  # reset — user explicitly chose to retry
        start_sprint_idx = progress.current_sprint - 1
        logger.info("Resuming from sprint %d", progress.current_sprint)
    else:
        progress = HarnessProgress(
            total_sprints=len(SPRINTS),
            sprint_statuses=[
                SprintStatus(sprint_number=s.number, sprint_name=s.name) for s in SPRINTS
            ],
        )
        start_sprint_idx = 0
    save_progress(checkpoint_dir, progress)
    start_time = time.time()
    t = config.thresholds

    for sprint in SPRINTS[start_sprint_idx:]:
        logger.info("=" * 60)
        logger.info(f"SPRINT {sprint.number}/{len(SPRINTS)}: {sprint.name}")
        logger.info("=" * 60)

        sprint_status = progress.sprint_statuses[sprint.number - 1]

        # On resume, a sprint may already be passed/failed if the crash happened
        # between completed_sprints++ and the next sprint's current_sprint update.
        if resume and sprint_status.status in ("passed", "failed"):
            if sprint_status.status == "passed":
                logger.info("[Sprint %d] Already passed — skipping", sprint.number)
                continue
            # failed: if rounds_completed < current limit the user bumped the config;
            # reset to building so the existing mid-sprint resume logic picks up.
            if sprint_status.rounds_completed >= t.max_rounds_per_sprint:
                logger.info("[Sprint %d] Already failed — skipping", sprint.number)
                continue
            logger.info(
                "[Sprint %d] Previously failed after %d rounds but max_rounds_per_sprint "
                "is now %d — resuming from round %d (consecutive_passes=%d)",
                sprint.number, sprint_status.rounds_completed,
                t.max_rounds_per_sprint, sprint_status.rounds_completed + 1,
                sprint_status.consecutive_passes,
            )
            sprint_status.status = "building"

        # Contract negotiation — or load from disk on mid-sprint resume
        t0 = time.monotonic()
        if resume and sprint_status.rounds_completed > 0:
            try:
                contract = load_contract(output_dir, sprint.number)
                logger.info(
                    "[Sprint %d] Loaded saved contract from disk (%d criteria)",
                    sprint.number, len(contract.criteria),
                )
            except FileNotFoundError:
                logger.warning(
                    "[Sprint %d] No saved contract found — re-negotiating", sprint.number
                )
                contract = await negotiate_contract(
                    config.generator, config.critic, sprint, prd_content, output_dir,
                    cli_path=cli_path, supplementary_context=supplementary_context,
                )
                logger.info(
                    "[Sprint %d] Contract negotiation completed in %.1fs (%d criteria)",
                    sprint.number, time.monotonic() - t0, len(contract.criteria),
                )
        else:
            contract = await negotiate_contract(
                config.generator, config.critic, sprint, prd_content, output_dir,
                cli_path=cli_path, supplementary_context=supplementary_context,
            )
            logger.info(
                "[Sprint %d] Contract negotiation completed in %.1fs (%d criteria)",
                sprint.number, time.monotonic() - t0, len(contract.criteria),
            )

        sprint_status.status = "building"
        progress.current_sprint = sprint.number
        save_progress(checkpoint_dir, progress)

        last_result: CriticResult | None = None
        consecutive_passes = 0
        best_result: CriticResult | None = None
        best_commit_sha: str | None = None

        # On mid-sprint resume: restore round state from checkpoint
        start_round = sprint_status.rounds_completed + 1
        if resume and sprint_status.rounds_completed > 0:
            consecutive_passes = sprint_status.consecutive_passes
            prior_feedback_path = (
                output_dir / "feedback"
                / f"sprint-{sprint.number}-round-{sprint_status.rounds_completed}.json"
            )
            if prior_feedback_path.exists():
                last_result = load_feedback(
                    output_dir, sprint.number, sprint_status.rounds_completed
                )
            # Seed best_result from prior rounds (best_commit_sha stays None —
            # rollback is only safe once a new commit exists in this session)
            for r in range(1, sprint_status.rounds_completed + 1):
                try:
                    prior = load_feedback(output_dir, sprint.number, r)
                    if best_result is None or prior.average_score > best_result.average_score:
                        best_result = prior
                except FileNotFoundError:
                    pass
            logger.info(
                "[Sprint %d] Resuming from round %d (rounds_completed=%d, consecutive_passes=%d)",
                sprint.number, start_round,
                sprint_status.rounds_completed, consecutive_passes,
            )

        for round_num in range(start_round, t.max_rounds_per_sprint + 1):
            # Global safety checks
            elapsed_total = time.time() - start_time
            if progress.total_rounds >= t.max_total_rounds:
                logger.error(
                    "Max total rounds reached (elapsed=%.1fm) — stopping.",
                    elapsed_total / 60,
                )
                progress.status = "failed"
                save_progress(checkpoint_dir, progress)
                return

            if t.timeout_hours > 0 and elapsed_total > t.timeout_hours * 3600:
                logger.error(
                    "Timeout reached (elapsed=%.1fm, limit=%.0fh) — stopping.",
                    elapsed_total / 60, t.timeout_hours,
                )
                progress.status = "failed"
                save_progress(checkpoint_dir, progress)
                return

            logger.info(
                "[Sprint %d] Round %d/%d (total_rounds=%d elapsed=%.1fm)",
                sprint.number, round_num, t.max_rounds_per_sprint,
                progress.total_rounds, elapsed_total / 60,
            )

            # Inner retry loop — recovers from CLI crashes (e.g. disallowed tool call)
            round_ok = False
            result: CriticResult | None = None
            for round_attempt in range(1, t.max_round_retries + 2):
                try:
                    # Generator builds — writes files directly via tool use
                    sprint_status.status = "building"
                    save_progress(checkpoint_dir, progress)
                    t0 = time.monotonic()
                    gen_round: GeneratorRoundResult = await run_generator(
                        config.generator,
                        sprint,
                        contract,
                        prd_content,
                        last_result,
                        output_dir,
                        round_num,
                        cli_path=cli_path,
                        supplementary_context=supplementary_context,
                    )
                    logger.info(
                        "[Sprint %d] Generator round %d completed in %.1fs",
                        sprint.number, round_num, time.monotonic() - t0,
                    )

                    # Detect files written by the generator and auto-commit
                    written = get_modified_files(repo)
                    if written:
                        logger.info(
                            "[Sprint %d] Generator wrote %d files: %s",
                            sprint.number, len(written), ", ".join(p.name for p in written),
                        )
                    else:
                        logger.warning(
                            "[Sprint %d] Generator produced no file changes", sprint.number
                        )
                    git_commit(
                        repo,
                        f"Generator pass {round_num} - sprint {sprint.number} ({sprint.name})",
                        written,
                    )
                    append_generator_history(
                        output_dir,
                        sprint.number,
                        round_num,
                        previous_feedback=last_result,
                        modified_files=written,
                        input_tokens=gen_round.input_tokens,
                    )

                    # Critic evaluates — reads files via tool use
                    sprint_status.status = "evaluating"
                    save_progress(checkpoint_dir, progress)
                    t0 = time.monotonic()
                    result = await run_critic(
                        config.critic, contract, output_dir, round_num, cli_path=cli_path
                    )
                    logger.info(
                        "[Sprint %d] Critic round %d completed in %.1fs",
                        sprint.number, round_num, time.monotonic() - t0,
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
                    append_critic_history(output_dir, sprint.number, round_num, result)
                    round_ok = True
                    break

                except Exception as exc:
                    logger.error(
                        "[Sprint %d] Round %d attempt %d/%d FAILED: %s",
                        sprint.number, round_num, round_attempt, t.max_round_retries + 1, exc,
                    )
                    if round_attempt <= t.max_round_retries:
                        logger.info(
                            "[Sprint %d] Retrying round %d with fresh generator session...",
                            sprint.number, round_num,
                        )
                    else:
                        logger.error(
                            "[Sprint %d] Round %d exhausted all %d attempts",
                            sprint.number, round_num, t.max_round_retries + 1,
                        )

            if not round_ok:
                sprint_status.status = "failed"
                sprint_status.final_score = last_result.average_score if last_result else 0.0
                break

            assert result is not None
            progress.total_rounds += 1
            sprint_status.rounds_completed = round_num
            logger.info(
                "[Sprint %d] Round %d result: avg=%.1f passed=%s",
                sprint.number, round_num, result.average_score, result.passed,
            )
            for f in result.feedback:
                logger.info(
                    "  %s: %.1f/10 [%s] %s",
                    f.criterion, f.score, f.severity, f.details[:120],
                )

            # Score trajectory vs previous round
            if last_result is not None:
                delta = result.average_score - last_result.average_score
                if delta > 0.05:
                    direction = "improved"
                elif delta < -0.05:
                    direction = "regressed"
                else:
                    direction = "unchanged"
                logger.info(
                    "[Sprint %d] Score trajectory: %.1f -> %.1f (%+.1f, %s)",
                    sprint.number, last_result.average_score, result.average_score,
                    delta, direction,
                )

            # Track best for keep-best hill climbing
            if best_result is None or result.average_score > best_result.average_score:
                best_result = result
                best_commit_sha = repo.head.commit.hexsha
                logger.info(
                    "[Sprint %d] New best score: %.1f (commit %s)",
                    sprint.number, best_result.average_score, best_commit_sha[:8],
                )

            # Exit criteria
            if sprint_passes(result, t.min_score):
                consecutive_passes += 1
                logger.info(
                    "Consecutive passes: %d/%d", consecutive_passes, t.consecutive_passing_rounds
                )
                if consecutive_passes >= t.consecutive_passing_rounds:
                    logger.info(f"Sprint {sprint.number} PASSED")
                    sprint_status.status = "passed"
                    sprint_status.final_score = result.average_score
                    break
            else:
                consecutive_passes = 0

            # Persist consecutive_passes after each completed round for crash recovery
            sprint_status.consecutive_passes = consecutive_passes
            save_progress(checkpoint_dir, progress)

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
                else:
                    logger.debug(
                        "[Sprint %d] Ping-pong check clear (similarity=%.2f threshold=%.2f)",
                        sprint.number, pp.similarity_score, t.ping_pong_similarity_threshold,
                    )

            # Keep-best rollback
            rolled_back = False
            if (
                best_result is not None
                and best_commit_sha is not None
                and result.average_score
                < best_result.average_score - t.rollback_regression_threshold
            ):
                logger.warning(
                    "[Sprint %d] Round %d score %.1f < best %.1f — rolling back to %s",
                    sprint.number, round_num,
                    result.average_score, best_result.average_score, best_commit_sha[:8],
                )
                restored = restore_arch_files_from_commit(repo, best_commit_sha)
                if restored:
                    git_commit_staged(
                        repo,
                        f"Rollback sprint {sprint.number} round {round_num}: "
                        f"restore best (score {best_result.average_score:.1f}, "
                        f"was {result.average_score:.1f})",
                    )
                    logger.info(
                        "[Sprint %d] Rollback committed: %d file(s)", sprint.number, len(restored)
                    )
                append_rollback_event(
                    output_dir, sprint.number, round_num,
                    result.average_score, best_result.average_score, best_commit_sha,
                )
                last_result = best_result  # next generator sees best-version feedback
                rolled_back = True

            if not rolled_back:
                last_result = result
        else:
            # Max rounds exhausted without passing
            elapsed_total = time.time() - start_time
            logger.error(
                "[Sprint %d] FAILED after %d rounds (elapsed=%.1fm)",
                sprint.number, t.max_rounds_per_sprint, elapsed_total / 60,
            )
            logger.error(
                "Hint: sprint exhausted max_rounds_per_sprint=%d without achieving "
                "%d consecutive passing round(s). Last consecutive passes: %d/%d. "
                "To fix: increase max_rounds_per_sprint (currently %d) or lower "
                "consecutive_passing_rounds (currently %d) in ~/.deep-researcher.toml.",
                t.max_rounds_per_sprint, t.consecutive_passing_rounds,
                consecutive_passes, t.consecutive_passing_rounds,
                t.max_rounds_per_sprint, t.consecutive_passing_rounds,
            )
            sprint_status.status = "failed"
            progress.status = "failed"
            save_progress(checkpoint_dir, progress)
            return

        if sprint_status.status == "failed":
            # Round retry exhaustion caused the loop to break before passing
            logger.error(
                "[Sprint %d] Sprint failed due to unrecoverable round failure",
                sprint.number,
            )
            progress.status = "failed"
            save_progress(checkpoint_dir, progress)
            return

        progress.completed_sprints += 1
        save_progress(checkpoint_dir, progress)
        # Sprint-boundary commit: capture critic feedback, progress, and history
        written = get_modified_files(repo)
        git_commit(
            repo,
            f"Sprint {sprint.number} complete: {sprint.name}",
            written,
        )

    # Final mutual agreement round
    await run_final_agreement(config.generator, config.critic, output_dir, cli_path=cli_path)
    progress.status = "complete"
    save_progress(checkpoint_dir, progress)
    # Final commit: capture terminal progress state
    written = get_modified_files(repo)
    git_commit(
        repo,
        f"Architecture complete — all {progress.total_sprints} sprints passed",
        written,
    )
    total_elapsed = time.time() - start_time
    logger.info(
        "Harness COMPLETE in %.1f minutes — architecture is production-ready",
        total_elapsed / 60,
    )
    run_stats.log_summary()
