from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from deep_architect.config import HarnessConfig, load_config
from deep_architect.git_ops import git_commit, validate_git_repo
from deep_architect.logger import get_logger

logger = get_logger(__name__)


if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage


# ---------------------------------------------------------------------------
# Constants & Data Types
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR = Path("feedback")


@dataclass
class ReviewFinding:
    """Represents a single review finding from review-analyzer output."""

    file_path: Path
    line_start: int | None
    line_end: int | None
    existing_code: str
    suggested_code: str
    review_comment: str
    analysis: str
    finding_id: str


@dataclass
class ValidationConfig:
    """Configuration for validation commands."""

    commands: list[list[str]]
    timeout: int = 30


@dataclass
class AgentConfig:
    """Configuration for the coding agent."""

    provider: str = "opencode"
    model: str = "standard/coder"
    max_retries: int = 3
    retry_delay: float = 1.0
    permission_mode: str = "bypassPermissions"
    disallowed_tools: list[str] | None = None


# ---------------------------------------------------------------------------
# CodingAgent Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CodingAgent(Protocol):
    """Protocol defining the interface for applying code fixes."""

    async def apply_fix(
        self,
        file_path: Path,
        existing_code: str,
        suggested_code: str,
        context: str = "",
    ) -> bool:
        """Apply a fix to a file. Returns True if successful."""


# ---------------------------------------------------------------------------
# OpencodeAgent
# ---------------------------------------------------------------------------


class OpencodeAgent:
    """Opencode implementation of CodingAgent using subprocess."""

    def __init__(
        self,
        model: str = "standard/coder",
        opencode_bin: str | None = None,
    ) -> None:
        self.model = model
        self.opencode_bin = opencode_bin or os.environ.get(
            "OPENCODE_BIN", "/home/gerald/.opencode/bin/opencode"
        )

    async def apply_fix(
        self,
        file_path: Path,
        existing_code: str,
        suggested_code: str,
        context: str = "",
    ) -> bool:
        """Apply fix using opencode subprocess."""
        prompt = (
            f"File: {file_path}\n"
            f"Existing code:\n{existing_code}\n\n"
            f"Replace with:\n{suggested_code}\n\n"
            f"Context: {context}"
        )

        try:
            result = subprocess.run(
                [
                    self.opencode_bin,
                    "run",
                    "--model",
                    self.model,
                    "--format",
                    "text",
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                logger.error(
                    "OpencodeAgent: failed to apply fix for %s: %s",
                    file_path,
                    result.stderr,
                )
                return False
            return True
        except FileNotFoundError:
            logger.error(
                "OpencodeAgent: opencode binary not found at %s",
                self.opencode_bin,
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error(
                "OpencodeAgent: timeout applying fix for %s", file_path
            )
            return False
        except Exception as e:
            logger.error(
                "OpencodeAgent: exception applying fix for %s: %s",
                file_path,
                e,
            )
            return False


# ---------------------------------------------------------------------------
# Markdown Parsing
# ---------------------------------------------------------------------------


def parse_markdown_finding(file_path: Path) -> ReviewFinding | None:
    """Parse a review-analyzer markdown file into a ReviewFinding."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read %s: %s", file_path, e)
        return None

    finding_id = file_path.stem

    file_match = re.search(r"-?\s*\*\*File\*\*:?\s*(.+)", content)
    lines_match = re.search(r"-?\s*\*\*Lines\*\*:?\s*(.+)", content)
    existing_code_match = re.search(
        r"\*\*Existing Code\*\*:?\s*```[a-zA-Z]*\s*\n(.*?)\n```",
        content,
        re.DOTALL,
    )
    suggested_code_match = re.search(
        r"\*\*Suggested Code\*\*:?\s*```[a-zA-Z]*\s*\n(.*?)\n```",
        content,
        re.DOTALL,
    )
    review_comment_match = re.search(
        r"-?\s*\*\*Review Comment\*\*:?\s*(.+?)(?:\n|$)", content
    )
    analysis_match = re.search(
        r"\*\*Analysis\*\*:?\s*\n(.*?)(?:\n---|\n\*Generated|\Z)",
        content,
        re.DOTALL,
    )

    if (
        file_match is None
        or existing_code_match is None
        or suggested_code_match is None
        or review_comment_match is None
    ):
        logger.warning("Missing required sections in %s", file_path)
        return None

    file_str = file_match.group(1).strip()
    try:
        full_path = Path(file_str)
    except Exception as e:
        logger.error(
            "Invalid file path '%s' in %s: %s", file_str, file_path, e
        )
        return None

    line_start: int | None = None
    line_end: int | None = None
    if lines_match:
        lines_str = lines_match.group(1).strip()
        if lines_str and "-" in lines_str:
            try:
                parts = lines_str.split("-")
                line_start = int(parts[0].strip())
                line_end = int(parts[1].strip())
            except ValueError:
                pass

    return ReviewFinding(
        file_path=full_path,
        line_start=line_start,
        line_end=line_end,
        existing_code=existing_code_match.group(1).strip(),
        suggested_code=suggested_code_match.group(1).strip(),
        review_comment=review_comment_match.group(1).strip(),
        analysis=analysis_match.group(1).strip() if analysis_match else "",
        finding_id=finding_id,
    )


def is_valid_finding(file_path: Path) -> bool:
    """Check if a markdown file contains a VALID verdict."""
    try:
        content = file_path.read_text(encoding="utf-8")
        verdict_match = re.search(
            r"\*\*Verdict\*\*:?\s*(VALID|REJECTED|BACKLOG)", content
        )
        if verdict_match:
            return verdict_match.group(1) == "VALID"
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def run_validation(file_path: Path, config: ValidationConfig) -> bool:
    """Run validation commands on a file."""
    for cmd in config.commands:
        try:
            full_cmd = cmd + [str(file_path)]
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout,
            )
            if result.returncode != 0:
                logger.warning(
                    "Validation failed for %s: %s\n%s",
                    file_path,
                    " ".join(full_cmd),
                    result.stderr,
                )
                return False
        except subprocess.TimeoutExpired:
            logger.error("Validation timeout for %s", file_path)
            return False
        except Exception as e:
            logger.error(
                "Error running validation on %s: %s", file_path, e
            )
            return False
    return True


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def _process_single_finding(
    md_file: Path,
    agent: CodingAgent,
    validation_config: ValidationConfig,
    max_retries: int,
    retry_delay: float,
    dry_run: bool,
) -> tuple[str, bool, str | None]:
    """Process a single VALID finding. Returns (status, committed, error).

    Status is one of: 'skipped', 'committed', 'error'.
    """
    finding = parse_markdown_finding(md_file)
    if finding is None:
        return ("error", False, f"Failed to parse finding: {md_file.name}")

    logger.info(
        "Processing finding %s for %s", finding.finding_id, finding.file_path
    )

    # Apply fix with retries
    success = False
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        try:
            success = asyncio.run(
                agent.apply_fix(
                    finding.file_path,
                    finding.existing_code,
                    finding.suggested_code,
                    finding.analysis,
                )
            )

            if success:
                break
            last_error = "Agent.apply_fix returned False"

        except Exception as e:
            last_error = str(e)
            logger.warning(
                "Attempt %d failed for %s: %s",
                attempt + 1,
                finding.file_path,
                e,
            )

            if attempt < max_retries:
                asyncio.get_event_loop().run_until_complete(
                    asyncio.sleep(retry_delay * (2 ** attempt))
                )

    if not success:
        return (
            "error",
            False,
            (
                f"Failed to apply fix for {finding.file_path} "
                f"after {max_retries + 1} attempts: {last_error}"
            ),
        )

    # Validate changes
    if not dry_run:
        if not run_validation(finding.file_path, validation_config):
            return (
                "skipped",
                False,
                f"Validation failed for {finding.file_path}, skipping commit",
            )

    # Commit changes
    if not dry_run:
        try:
            repo = validate_git_repo(Path.cwd())
            comment_snippet = finding.review_comment[:50]
            suffix = (
                "..." if len(finding.review_comment) > 50 else ""
            )
            commit_message = (
                f"fix: {comment_snippet}{suffix} [{finding.finding_id}]"
            )
            git_commit(repo, commit_message, [finding.file_path])
            logger.info("Committed fix for %s", finding.file_path)
            return ("committed", True, None)
        except Exception as e:
            return (
                "error",
                False,
                f"Failed to commit changes for {finding.file_path}: {e}",
            )
    else:
        logger.info("[DRY RUN] Would commit fix for %s", finding.file_path)
        return ("committed", True, None)


def process_findings(
    output_dir: Path,
    agent: CodingAgent,
    validation_config: ValidationConfig,
    max_retries: int,
    retry_delay: float,
    dry_run: bool = False,
) -> dict[str, int]:
    """Process all VALID findings in the output directory."""
    stats: dict[str, int] = {
        "processed": 0,
        "committed": 0,
        "skipped": 0,
        "errors": 0,
    }

    if not output_dir.exists():
        logger.error("Output directory %s does not exist", output_dir)
        return stats

    markdown_files = sorted(output_dir.glob("*.md"))
    if not markdown_files:
        logger.warning("No markdown files found in %s", output_dir)
        return stats

    logger.info("Found %d markdown files to process", len(markdown_files))

    for md_file in markdown_files:
        # Skip summary/index files
        if md_file.name in ("SUMMARY.md", "INDEX.md"):
            continue

        stats["processed"] += 1

        if not is_valid_finding(md_file):
            logger.info("Skipping non-VALID finding: %s", md_file.name)
            stats["skipped"] += 1
            continue

        status, committed, error = _process_single_finding(
            md_file,
            agent,
            validation_config,
            max_retries,
            retry_delay,
            dry_run,
        )

        if status == "error":
            logger.error("%s", error or f"Unknown error processing {md_file.name}")
            stats["errors"] += 1
        elif status == "skipped":
            logger.warning("%s", error or f"Skipped {md_file.name}")
            stats["skipped"] += 1
        elif committed:
            stats["committed"] += 1

    return stats


# ---------------------------------------------------------------------------
# Agent Factory
# ---------------------------------------------------------------------------


def create_agent(config: AgentConfig) -> CodingAgent:
    """Factory function to create the appropriate coding agent."""
    if config.provider == "opencode":
        return OpencodeAgent(model=config.model)
    elif config.provider == "claude":
        return _create_claude_agent(config)
    else:
        raise ValueError(
            f"Unsupported agent provider: {config.provider}"
        )


def _create_claude_agent(config: AgentConfig) -> CodingAgent:
    """Create a Claude SDK agent, or raise if SDK unavailable."""
    try:
        from claude_agent_sdk import (  # noqa: F401 PLC0415
            ClaudeAgentOptions,
            query,
        )
    except ImportError:
        raise ImportError(
            "claude-agent-sdk is required for claude provider. "
            "Install it with: pip install claude-agent-sdk"
        ) from None

    from deep_architect.review_action_harness import (  # noqa: PLC0415
        ClaudeSDKAgent,
    )

    return ClaudeSDKAgent(
        model=config.model,
        permission_mode=config.permission_mode,
        disallowed_tools=config.disallowed_tools,
    )


# ---------------------------------------------------------------------------
# Claude SDK Agent
# ---------------------------------------------------------------------------


class ClaudeSDKAgent:
    """Claude SDK implementation of CodingAgent."""

    def __init__(
        self,
        model: str = "sonnet",
        permission_mode: str = "bypassPermissions",
        disallowed_tools: list[str] | None = None,
    ) -> None:
        self.model = model
        self.permission_mode = permission_mode
        self.disallowed_tools = disallowed_tools or [
            "TodoWrite",
            "Agent",
            "WebSearch",
            "WebFetch",
            "Bash",
            "NotebookEdit",
            "NotebookRead",
            "NotebookCreate",
            "NotebookQuery",
        ]
        self.model_id = self._resolve_model_id(model)
        self.cli_path = self._resolve_cli_path()

    def _resolve_model_id(self, model_alias: str) -> str:
        """Resolve model alias to actual model ID."""
        alias_map = {
            "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        }
        env_var = alias_map.get(model_alias.lower())
        if env_var:
            model_id = os.environ.get(env_var, model_alias)
            return model_id or model_alias
        return model_alias

    def _resolve_cli_path(self) -> str | None:
        """Resolve Claude CLI path."""
        import shutil  # noqa: PLC0415

        return shutil.which("claude")

    async def apply_fix(
        self,
        file_path: Path,
        existing_code: str,
        suggested_code: str,
        context: str = "",
    ) -> bool:
        """Apply fix using Claude SDK."""
        from claude_agent_sdk import (  # noqa: PLC0415
            ClaudeAgentOptions,
        )

        prompt = (
            f"Please apply the following code change to {file_path}:\n\n"
            f"Existing code:\n```\n{existing_code}\n```\n\n"
            f"Replace with:\n```\n{suggested_code}\n```\n\n"
            f"Context: {context}\n\n"
            "Make the change and confirm it was applied correctly."
        )

        system_prompt = (
            "You are a precise code editing assistant. Your task is to make "
            "exact code replacements as specified. Do not make any other "
            "changes unless explicitly instructed. Confirm when the change "
            "has been made."
        )

        try:
            options = ClaudeAgentOptions(
                system_prompt=system_prompt,
                permission_mode=self.permission_mode,  # type: ignore[arg-type]
                tools=[],
                disallowed_tools=self.disallowed_tools,
                model=self.model_id,
                settings='{"alwaysThinkingEnabled": false}',
            )

            if self.cli_path:
                options = ClaudeAgentOptions(
                    system_prompt=system_prompt,
                    permission_mode=self.permission_mode,  # type: ignore[arg-type]
                    tools=[],
                    disallowed_tools=self.disallowed_tools,
                    model=self.model_id,
                    cli_path=self.cli_path,
                    settings='{"alwaysThinkingEnabled": false}',
                )

            result = await self._consume_query(prompt, options)

            if result.is_error:
                error_detail = (
                    result.errors[0] if result.errors else "Unknown error"
                )
                logger.error("Claude SDK error: %s", error_detail)
                return False

            return not result.is_error

        except Exception as e:
            logger.error("Exception using Claude SDK: %s", e)
            return False

    async def _consume_query(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> ResultMessage:
        """Consume the query generator."""
        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ResultMessage,
            query,
        )

        gen = query(prompt=prompt, options=options).__aiter__()
        try:
            last_message: ResultMessage | None = None
            while True:
                try:
                    message = await gen.__anext__()
                except StopAsyncIteration:
                    break
                except Exception as e:
                    logger.error("Error in query consumption: %s", e)
                    break

                if isinstance(message, ResultMessage):
                    last_message = message
                elif isinstance(message, AssistantMessage):
                    if message.error is not None:
                        logger.warning(
                            "Claude SDK assistant error: %s", message.error
                        )

            if last_message is not None:
                return last_message

            # Fallback: create an error ResultMessage
            # Use **kwargs to handle potential SDK version differences
            kwargs: dict[str, object] = {
                "result": "",
                "session_id": "unknown",
                "duration_ms": 0,
                "is_error": True,
                "num_turns": 0,
                "errors": ["No result message received"],
            }
            return ResultMessage(**kwargs)  # type: ignore[arg-type]
        finally:
            try:
                await gen.aclose()  # type: ignore[attr-defined]
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Apply review-analyzer fixes automatically"
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=DEFAULT_OUTPUT_DIR,
        type=Path,
        help="Directory containing review-analyzer markdown output",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model to use (overrides config)",
    )
    parser.add_argument(
        "--provider",
        choices=["opencode", "claude"],
        default=None,
        help="Agent provider to use (overrides config, default: opencode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to configuration file "
            "(defaults to ~/.deep-architect.toml)"
        ),
    )
    return parser.parse_args(argv)


def print_summary(stats: dict[str, int]) -> None:
    """Print the final processing summary."""
    print("\n=== Review Action Harness Summary ===")
    print(f"Processed:  {stats['processed']}")
    print(f"Committed:  {stats['committed']}")
    print(f"Skipped:    {stats['skipped']}")
    print(f"Errors:     {stats['errors']}")


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    args = parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load configuration
    harness_config = HarnessConfig()
    try:
        config_path = args.config or Path.home() / ".deep-architect.toml"
        if config_path.exists():
            harness_config = load_config(config_path)
    except Exception as e:
        logger.warning(
            "Failed to load config: %s, using defaults", e
        )

    model = args.model or harness_config.generator.model
    provider = args.provider or "opencode"

    # Validate git repo
    try:
        validate_git_repo(Path.cwd())
    except SystemExit:
        return 1
    except Exception as e:
        logger.error("Not in a valid git repository: %s", e)
        return 1

    # Initialize configs
    agent_config = AgentConfig(
        provider=provider,
        model=model,
        max_retries=harness_config.thresholds.model_comm_failure_threshold,
        retry_delay=harness_config.thresholds.model_comm_base_backoff,
        permission_mode="bypassPermissions",
    )

    validation_config = ValidationConfig(
        commands=[["ruff", "check"], ["mypy"]],
        timeout=30,
    )

    # Create agent
    try:
        agent = create_agent(agent_config)
    except Exception as e:
        logger.error("Failed to initialize agent: %s", e)
        return 1

    # Process findings
    stats = process_findings(
        args.output_dir,
        agent,
        validation_config,
        agent_config.max_retries,
        agent_config.retry_delay,
        args.dry_run,
    )

    print_summary(stats)

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
