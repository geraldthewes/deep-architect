from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from deep_architect.agents.circuit_breaker import (
    CircuitBreakerState,
    ModelCommunicationError,
)
from deep_architect.agents.client import (
    TurnLimitError,
    init_run_stats,
    make_agent_options,
    run_agent_text,
)
from deep_architect.agents.critic import check_ping_pong, review_contract, run_critic
from deep_architect.agents.generator import GeneratorRoundResult, propose_contract, run_generator
from deep_architect.config import AgentConfig, HarnessConfig
from deep_architect.exit_criteria import (
    is_perfect_score,
    should_early_accept,
    should_ping_pong_exit,
    sprint_passes,
)
from deep_architect.git_ops import (
    get_modified_files,
    git_commit,
    git_commit_staged,
    reject_unauthorized_files,
    restore_arch_files_from_commit,
    validate_git_repo,
)
from deep_architect.io.files import (
    append_critic_history,
    append_generator_history,
    append_rollback_event,
    generate_sprint_documentation,
    init_workspace,
    load_contract,
    load_feedback,
    load_progress,
    save_contract,
    save_feedback,
    save_progress,
    save_round_log,
    write_index,
)
from deep_architect.logger import get_logger, setup_logging
from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult
from deep_architect.models.progress import HarnessProgress, SprintStatus
from deep_architect.prompts import load_prompt
from deep_architect.sprints import SPRINTS, SprintDefinition

logger = get_logger(__name__)
console = Console()


_HARNESS_STATE_FILES = frozenset({
    "generator-history.md",
    "generator-learnings.md",
    "critic-history.md",
    "INDEX.md",
    "architecture-decisions.md",
    "nfr_catalog.json",
})

_HARNESS_STATE_DIRS = frozenset({
    "contracts/",
    "feedback/",
    "logs/",
    ".checkpoints/",
})


def _build_file_allowlist(
    sprint: SprintDefinition,
    contract: SprintContract,
) -> tuple[set[str], set[str]]:
    """Return (allowed_relpaths, allowed_dir_prefixes) for reject_unauthorized_files."""
    allowed_relpaths: set[str] = set(_HARNESS_STATE_FILES)
    allowed_relpaths.update(contract.files_to_produce)

    allowed_dir_prefixes: set[str] = set(_HARNESS_STATE_DIRS)
    if sprint.allow_extra_files:
        for f in contract.files_to_produce:
            top = f.split("/")[0]
            if "/" in f:
                allowed_dir_prefixes.add(top + "/")

    return allowed_relpaths, allowed_dir_prefixes


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
    prd_content: str | None,
    output_dir: Path,
    *,
    codebase_path: str | None = None,
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
        codebase_path=codebase_path,
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
    codebase_path: str | None = None,
    cli_path: str | None = None,
) -> tuple[bool, bool]:
    """Both agents must independently output READY_TO_SHIP.

    Returns (gen_ready, critic_ready). Non-blocking per ADR-017: a NOT READY
    result logs the agent's full feedback but does not fail the run.
    """
    logger.info("Running final mutual agreement round...")

    prompt_name = "final_agreement_re" if codebase_path is not None else "final_agreement"
    final_prompt = load_prompt(prompt_name)

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
            "Final agreement: Generator=%s, Critic=%s — review feedback below before shipping",
            "READY" if gen_ready else "NOT READY",
            "READY" if critic_ready else "NOT READY",
        )
        if not gen_ready:
            logger.warning("Generator final-agreement feedback:\n%s", gen_result.strip())
        if not critic_ready:
            logger.warning("Critic final-agreement feedback:\n%s", critic_result.strip())

    return gen_ready, critic_ready


def _do_early_accept(
    sprint: SprintDefinition,
    sprint_status: SprintStatus,
    best_result: CriticResult,
    best_commit_sha: str | None,
    current_result: CriticResult | None,
    repo: Any,
) -> None:
    """Restore best commit (if needed) and mark the sprint as passed."""
    logger.info(
        "[Sprint %d] Early accept: best_score=%.1f — declaring good enough",
        sprint.number, best_result.average_score,
    )
    if (
        best_commit_sha is not None
        and current_result is not None
        and current_result.average_score < best_result.average_score
    ):
        restored = restore_arch_files_from_commit(repo, best_commit_sha)
        if restored:
            git_commit_staged(
                repo,
                f"Early accept sprint {sprint.number}: restore best "
                f"(score {best_result.average_score:.1f})",
            )
            logger.info(
                "[Sprint %d] Best-effort restore committed: %d file(s)",
                sprint.number, len(restored),
            )
    sprint_status.status = "passed"
    sprint_status.final_score = best_result.average_score


def _log_resume_scores(progress: HarnessProgress) -> None:
    """Log a score summary for every sprint that has any tracked score."""
    any_scores = False
    for ss in progress.sprint_statuses:
        if ss.final_score is not None:
            logger.info(
                "  Sprint %d (%s): final_score=%.1f",
                ss.sprint_number, ss.sprint_name, ss.final_score,
            )
            any_scores = True
        elif ss.best_scores:
            avg = sum(ss.best_scores.values()) / len(ss.best_scores)
            logger.info(
                "  Sprint %d (%s): best_so_far=%.1f (round %d)",
                ss.sprint_number, ss.sprint_name, avg, ss.best_round or 0,
            )
            any_scores = True
    if not any_scores:
        logger.info("  No scores recorded yet.")


def _print_sprint_pause(
    sprint: SprintDefinition,
    output_dir: Path,
    progress: HarnessProgress,
) -> None:
    files_label = ", ".join(sprint.primary_files) if sprint.primary_files else "(none)"
    next_sprint = sprint.number + 1
    more_to_go = next_sprint <= progress.total_sprints
    console.print()
    console.print(
        f"[green]Sprint {sprint.number}/{progress.total_sprints} complete:[/green] "
        f"{sprint.name}"
    )
    console.print(f"  Review files in [bold]{output_dir}[/bold]:")
    console.print(f"    {files_label}")
    if more_to_go:
        console.print(
            f"  When ready, re-run your command with [bold]--resume[/bold] "
            f"to continue with sprint {next_sprint}."
        )
        console.print(
            "  Add [bold]--yolo[/bold] to run the remaining sprints unattended."
        )
    else:
        console.print(
            "  All 7 sprints complete. Re-run your command with [bold]--resume[/bold] "
            "to run the final mutual-agreement round."
        )
    console.print()
    logger.info(
        "[Sprint %d] Paused for human review (non-yolo mode). "
        "Re-run with --resume to continue.",
        sprint.number,
    )


async def run_harness(
    prd: Path | None,
    output_dir: Path,
    resume: bool,
    config: HarnessConfig,
    context_files: list[Path] | None = None,
    codebase: Path | None = None,
    *,
    strict: bool = False,
    yolo: bool = False,
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

    # Fail fast if no API key is configured
    if not os.environ.get("ANTHROPIC_AUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "No API key found. Set ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY "
            "in your environment and retry."
        )

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
    prd_content: str | None = prd.read_text() if prd is not None else None
    codebase_path: str | None = str(codebase.resolve()) if codebase is not None else None
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
        _log_resume_scores(progress)
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

    # Create circuit breaker states for Generator and Critic
    gen_circuit_state = CircuitBreakerState(
        agent_role="Generator",
        model=config.generator.model,
    )
    critic_circuit_state = CircuitBreakerState(
        agent_role="Critic",
        model=config.critic.model,
    )

    for sprint in SPRINTS[start_sprint_idx:]:
        logger.info("=" * 60)
        logger.info(f"SPRINT {sprint.number}/{len(SPRINTS)}: {sprint.name}")
        logger.info("=" * 60)

        sprint_status = progress.sprint_statuses[sprint.number - 1]

        # On resume, a sprint may already be passed/failed/accepted if the crash happened
        # between completed_sprints++ and the next sprint's current_sprint update.
        if resume and sprint_status.status in ("passed", "failed", "accepted"):
            if sprint_status.status in ("passed", "accepted"):
                logger.info(
                    "[Sprint %d] Already %s — skipping", sprint.number, sprint_status.status
                )
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
                    codebase_path=codebase_path,
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
        stall_count = 0
        best_result: CriticResult | None = None
        best_commit_sha: str | None = None
        last_known_gen_input_tokens: int = 0
        
        # Reset circuit breaker states at the start of each sprint
        gen_circuit_state.reset()
        critic_circuit_state.reset()

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
            # If the last completed round was a regression that was rolled back,
            # restore the generator's feedback pointer to the best round so it
            # resumes with the critic feedback that matches the rolled-back spec.
            if (
                last_result is not None
                and best_result is not None
                and best_result.average_score > last_result.average_score
            ):
                last_result = best_result
            logger.info(
                "[Sprint %d] Resuming from round %d (rounds_completed=%d, consecutive_passes=%d)",
                sprint.number, start_round,
                sprint_status.rounds_completed, consecutive_passes,
            )

        for round_num in range(start_round, t.max_rounds_per_sprint + 1):
            # Global safety checks
            elapsed_total = time.time() - start_time
            if t.max_total_rounds > 0 and progress.total_rounds >= t.max_total_rounds:
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
            turn_limited = False
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
                         codebase_path=codebase_path,
                         cli_path=cli_path,
                         supplementary_context=supplementary_context,
                         last_known_input_tokens=last_known_gen_input_tokens,
                         circuit_breaker_state=gen_circuit_state,
                         failure_threshold=t.model_comm_failure_threshold,
                         base_backoff=t.model_comm_base_backoff,
                         max_backoff=t.model_comm_max_backoff,
                     )
                    logger.info(
                        "[Sprint %d] Generator round %d completed in %.1fs",
                        sprint.number, round_num, time.monotonic() - t0,
                    )
                    if gen_round.input_tokens:
                        last_known_gen_input_tokens = gen_round.input_tokens

                    # Detect files written by the generator and auto-commit
                    written = get_modified_files(repo)
                    allowed_relpaths, allowed_dir_prefixes = _build_file_allowlist(
                        sprint, contract
                    )
                    rejected = reject_unauthorized_files(
                        repo, output_dir, written, allowed_relpaths, allowed_dir_prefixes
                    )
                    if rejected:
                        logger.warning(
                            "[Sprint %d] Rejected %d scratch file(s): %s",
                            sprint.number, len(rejected),
                            ", ".join(p.name for p in rejected),
                        )
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

                    # Critic evaluates - reads files via tool use
                    sprint_status.status = "evaluating"
                    save_progress(checkpoint_dir, progress)
                    t0 = time.monotonic()
                    result = await run_critic(
                        config.critic, contract, output_dir, round_num, cli_path=cli_path,
                        circuit_breaker_state=critic_circuit_state,
                        failure_threshold=t.model_comm_failure_threshold,
                        base_backoff=t.model_comm_base_backoff,
                        max_backoff=t.model_comm_max_backoff,
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

                except TurnLimitError:
                    # Generator ran out of turns — don't retry with a fresh session.
                    # Commit whatever partial work was written and let the sprint
                    # loop decide whether to early-accept or continue.
                    logger.warning(
                        "[Sprint %d] Round %d: turn limit reached — committing partial work",
                        sprint.number, round_num,
                    )
                    written = get_modified_files(repo)
                    allowed_relpaths, allowed_dir_prefixes = _build_file_allowlist(
                        sprint, contract
                    )
                    rejected = reject_unauthorized_files(
                        repo, output_dir, written, allowed_relpaths, allowed_dir_prefixes
                    )
                    if rejected:
                        logger.warning(
                            "[Sprint %d] Rejected %d scratch file(s): %s",
                            sprint.number, len(rejected),
                            ", ".join(p.name for p in rejected),
                        )
                        written = get_modified_files(repo)
                    if written:
                        git_commit(
                            repo,
                            f"Generator turn limit sprint {sprint.number}"
                            f" round {round_num} (partial)",
                            written,
                        )
                    turn_limited = True
                    break  # exits inner retry loop; round_ok stays False

                except ModelCommunicationError as exc:
                      logger.critical(
                          "Circuit breaker opened: %s\n"
                          "Failures: %d consecutive between %s and %s\n"
                          "Recommendation: Check network connectivity and provider status. "
                          "Run will not continue.",
                          exc.agent_role, exc.failures,
                          exc.timestamps[0] if exc.timestamps else "unknown",
                          exc.timestamps[-1] if exc.timestamps else "unknown",
                      )
                      progress.status = "failed"
                      save_progress(checkpoint_dir, progress)
                      return
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
                          # Drain any anyio cancel-scope cancels leaked from the failed
                          # SDK subprocess before the next attempt starts a fresh query().
                          _ht = asyncio.current_task()
                          if _ht is not None:
                              while _ht.cancelling() > 0:
                                  _ht.uncancel()
                      else:
                          logger.error(
                              "[Sprint %d] Round %d exhausted all %d attempts",
                              sprint.number, round_num, t.max_round_retries + 1,
                          )

            if turn_limited:
                stall_count += 1
                logger.warning(
                    "[Sprint %d] Round %d: stall_count=%d (turn limit)",
                    sprint.number, round_num, stall_count,
                )
                if best_result is not None and should_early_accept(
                    best_result.average_score, stall_count,
                    t.early_accept_score, t.early_accept_stalls,
                ):
                    _do_early_accept(
                        sprint, sprint_status, best_result, best_commit_sha,
                        last_result, repo,
                    )
                    last_result = best_result
                    break
                progress.total_rounds += 1
                sprint_status.rounds_completed = round_num
                sprint_status.consecutive_passes = consecutive_passes
                save_progress(checkpoint_dir, progress)
                continue  # proceed to next round without marking as failed

            if not round_ok:
                # Round failed after exhausting all retries — treat like a stall
                # (no-op round) rather than terminating the sprint.  A transient
                # crash or timeout does not mean the remaining rounds will fail;
                # consuming the slot and continuing gives the sprint its full
                # budget.  The for…else clause handles exhaustion of all rounds.
                stall_count += 1
                logger.warning(
                    "[Sprint %d] Round %d: all attempts failed — treating as stall"
                    " (stall_count=%d)",
                    sprint.number, round_num, stall_count,
                )
                if best_result is not None and should_early_accept(
                    best_result.average_score, stall_count,
                    t.early_accept_score, t.early_accept_stalls,
                ):
                    _do_early_accept(
                        sprint, sprint_status, best_result, best_commit_sha,
                        last_result, repo,
                    )
                    last_result = best_result
                    break
                progress.total_rounds += 1
                sprint_status.rounds_completed = round_num
                sprint_status.consecutive_passes = consecutive_passes
                save_progress(checkpoint_dir, progress)
                continue

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
            is_improvement = best_result is None or result.average_score > best_result.average_score
            if is_improvement:
                best_result = result
                best_commit_sha = repo.head.commit.hexsha
                sprint_status.best_round = round_num
                sprint_status.best_scores = dict(result.scores)
                logger.info(
                    "[Sprint %d] New best score: %.1f (commit %s)",
                    sprint.number, best_result.average_score, best_commit_sha[:8],
                )
            else:
                # is_improvement is False ↔ best_result is not None (see expression above)
                assert best_result is not None
                # Round did not improve on best — count as a stall
                stall_count += 1
                logger.info(
                    "[Sprint %d] Round %d stalled (score=%.1f <= best=%.1f), stall_count=%d",
                    sprint.number, round_num,
                    result.average_score, best_result.average_score, stall_count,
                )
                if should_early_accept(
                    best_result.average_score, stall_count,
                    t.early_accept_score, t.early_accept_stalls,
                ):
                    _do_early_accept(
                        sprint, sprint_status, best_result, best_commit_sha,
                        result, repo,
                    )
                    last_result = best_result
                    break

            # Perfect score: immediate sprint victory — no further improvement possible
            if is_perfect_score(result):
                logger.info(
                    "[Sprint %d] Perfect score (10.0/10.0 all criteria) — declaring victory",
                    sprint.number,
                )
                sprint_status.status = "passed"
                sprint_status.final_score = result.average_score
                break

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
            if not strict and best_result is not None:
                logger.warning(
                    "[Sprint %d] Max rounds exhausted (elapsed=%.1fm, best_score=%.2f < "
                    "threshold=%.2f). Accepting best-effort result (pass --strict to halt "
                    "instead).",
                    sprint.number, elapsed_total / 60,
                    best_result.average_score, t.min_score,
                )
                if best_commit_sha is not None:
                    restored = restore_arch_files_from_commit(repo, best_commit_sha)
                    if restored:
                        git_commit_staged(
                            repo,
                            f"Accept best-effort sprint {sprint.number} "
                            f"(score {best_result.average_score:.2f} / "
                            f"threshold {t.min_score:.2f})",
                        )
                        logger.info(
                            "[Sprint %d] Best-effort restore committed: %d file(s)",
                            sprint.number, len(restored),
                        )
                else:
                    logger.info(
                        "[Sprint %d] No rollback SHA (best_result was seeded from a prior "
                        "session and never beaten this session). Accepting current on-disk "
                        "state at score %.2f.",
                        sprint.number, best_result.average_score,
                    )
                    git_commit_staged(
                        repo,
                        f"Accept best-effort sprint {sprint.number} "
                        f"(resumed; score {best_result.average_score:.2f} / "
                        f"threshold {t.min_score:.2f})",
                    )
                sprint_status.status = "accepted"
                sprint_status.final_score = best_result.average_score
                last_result = best_result
            else:
                logger.error(
                    "[Sprint %d] FAILED after %d rounds (elapsed=%.1fm)",
                    sprint.number, t.max_rounds_per_sprint, elapsed_total / 60,
                )
                logger.error(
                    "Hint: sprint exhausted max_rounds_per_sprint=%d without achieving "
                    "%d consecutive passing round(s). Last consecutive passes: %d/%d. "
                    "To fix: increase max_rounds_per_sprint (currently %d) or lower "
                    "consecutive_passing_rounds (currently %d) in ~/.deep-architect.toml.",
                    t.max_rounds_per_sprint, t.consecutive_passing_rounds,
                    consecutive_passes, t.consecutive_passing_rounds,
                    t.max_rounds_per_sprint, t.consecutive_passing_rounds,
                )
                sprint_status.status = "failed"
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

        # Generate and save sprint documentation
        try:
            # Read history files for documentation generation
            generator_history = ""
            critic_history = ""
            gen_history_path = output_dir / "generator-history.md"
            crit_history_path = output_dir / "critic-history.md"
            if gen_history_path.exists():
                generator_history = gen_history_path.read_text()
            if crit_history_path.exists():
                critic_history = crit_history_path.read_text()
            
            sprint_doc_path = generate_sprint_documentation(
                output_dir=output_dir,
                sprint_number=sprint.number,
                sprint_name=sprint.name,
                progress=progress,
                sprint_status=sprint_status,
                generator_history=generator_history,
                critic_history=critic_history,
            )
            logger.info(f"[Sprint {sprint.number}] Documentation generated: {sprint_doc_path.name}")
        except Exception as e:
            logger.warning(f"[Sprint {sprint.number}] Failed to generate sprint documentation: {e}")

        if not yolo:
            _print_sprint_pause(sprint, output_dir, progress)
            return

    # Final mutual agreement round
    gen_ready, critic_ready = await run_final_agreement(
        config.generator, config.critic, output_dir,
        codebase_path=codebase_path,
        cli_path=cli_path,
    )
    progress.status = "complete"
    save_progress(checkpoint_dir, progress)
    # Write navigational index before the final commit so it lands in git
    index_path = write_index(output_dir, SPRINTS, progress)
    logger.info("Index written: %s", index_path)
    # Final commit: capture terminal progress state + index
    written = get_modified_files(repo)
    git_commit(
        repo,
        f"Architecture complete — all {progress.total_sprints} sprints passed",
        written,
    )
    total_elapsed = time.time() - start_time
    if gen_ready and critic_ready:
        logger.info(
            "Harness COMPLETE in %.1f minutes — architecture is production-ready",
            total_elapsed / 60,
        )
    else:
        logger.warning(
            "Harness COMPLETE in %.1f minutes — all sprints passed but final agreement "
            "was not reached; review the feedback above before shipping",
            total_elapsed / 60,
        )
    run_stats.log_summary()
