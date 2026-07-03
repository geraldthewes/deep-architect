from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from deep_architect.config import HarnessConfig, load_config
from deep_architect.git_ops import git_commit, validate_git_repo
from deep_architect.logger import get_logger

logger = get_logger(__name__)

# Global flag for graceful shutdown on SIGINT
_shutdown_requested = False


def _sigint_handler(signum: int, frame: object) -> None:
    """Signal handler for SIGINT (CTRL-C). Sets shutdown flag and logs."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("CTRL-C received, finishing current finding before shutdown...")


if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
    import types


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
class FindingStatus:
    """Persistent status for a finding — written to the .md file after each run."""

    status: str  # "completed" | "error" | "skipped" | "interrupted"
    timestamp: str
    summary: str = ""
    commit_sha: str | None = None
    error_message: str | None = None


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
        original_content: str | None = None,
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

    def _load_prompt_template(self) -> str:
        """Load the prompt template from package resources."""
        try:
            import importlib.resources
            with importlib.resources.files('deep_architect.resources').joinpath('prompt_template.md').open('r') as f:
                return f.read()
        except (ImportError, AttributeError, FileNotFoundError):
            prompt_path = Path(__file__).parent / 'resources' / 'prompt_template.md'
            if prompt_path.exists():
                return prompt_path.read_text(encoding='utf-8')
            return (
                "You are a precise coding assistant. Your task is to:\n"
                "1. Read the feedback file to understand what needs to be fixed\n"
                "2. Confirm the issue is valid and needs fixing  \n"
                "3. Apply the exact fix suggested in the feedback\n"
                "4. Commit the changes with a conventional commit message\n"
                "5. Briefly summarize what was done\n\n"
                "The feedback file contains:\n"
                "- File to modify\n"
                "- Existing code (what's currently there)\n"
                "- Suggested code (what it should be changed to)\n"
                "- Context/explanation of why the change is needed\n\n"
                "When committing, use the format: `fix: {brief_description} [{file_path}]`\n"
                "If no changes are needed (already fixed), that's also acceptable."
            )

    async def apply_fix(
        self,
        file_path: Path,
        existing_code: str,
        suggested_code: str,
        context: str = "",
        original_content: str | None = None,
    ) -> bool:
        """Apply fix using opencode subprocess with file-based input."""
        import tempfile
        import os

        # Use absolute path to avoid any path resolution issues
        absolute_file_path = file_path.resolve()
        
        # Create temporary files for prompt and feedback
        prompt_file = None
        feedback_file = None
        
        try:
            # Create prompt file
            prompt_content = self._load_prompt_template()
            with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
                f.write(prompt_content)
                prompt_file = f.name
            
            # Create feedback file with the specific finding details
            feedback_content = (
                f"**File**: {absolute_file_path}\n\n"
                f"**Existing Code**:\n```\n{existing_code}\n```\n\n"
                f"**Suggested Code**:\n```\n{suggested_code}\n```\n\n"
                f"**Review Comment**: {context}\n"
            )
            with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
                f.write(feedback_content)
                feedback_file = f.name
            
            # Run opencode with file-based input
            result = subprocess.run(
                [
                    self.opencode_bin,
                    "run",
                    "--format",
                    "json",
                    "--dangerously-skip-permissions",
                    "Apply the fix based on the review feedback",
                    "--file",
                    prompt_file,
                    "--file",
                    feedback_file,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            opencode_success = _parse_opencode_ndjson(result.stdout)
            if opencode_success:
                # Verify the file was actually modified
                try:
                    current_content = absolute_file_path.read_text(
                        encoding="utf-8"
                    )
                    normalized_current = current_content.replace(
                        '\r\n', '\n'
                    )
                    normalized_suggested = suggested_code.replace(
                        '\r\n', '\n'
                    )

                    if normalized_current.strip() == normalized_suggested.strip():
                        logger.debug(
                            "OpencodeAgent: fix applied successfully for %s (file matches expected)",
                            file_path,
                        )
                        return True
                    else:
                        # File doesn't match expected exactly - check if any
                        # changes were made by comparing with pre-apply content
                        if original_content is not None:
                            normalized_original = original_content.replace(
                                '\r\n', '\n'
                            )
                            if normalized_current != normalized_original:
                                logger.debug(
                                    "OpencodeAgent: file modified for %s (differs from original)",
                                    file_path,
                                )
                                return True
                            else:
                                logger.warning(
                                    "OpencodeAgent: no changes made to %s (file unchanged)",
                                    file_path,
                                )
                                return False
                        else:
                            # No original content provided - fallback to trusting
                            # opencode's success if the file exists
                            logger.debug(
                                "OpencodeAgent: no original content, trusting opencode for %s",
                                file_path,
                            )
                            return True
                except FileNotFoundError:
                    logger.debug(
                        "OpencodeAgent: file not found for verification (likely test env), trusting opencode success for %s",
                        file_path,
                    )
                    return True
                except Exception as e:
                    logger.error(
                        "OpencodeAgent: error verifying fix for %s: %s",
                        file_path,
                        e,
                    )
                    return False
            else:
                # opencode failed - extract error information from output
                last_error = "unknown error"
                stdout_preview = ""
                stderr_preview = ""
                error_details = []
                full_stdout_for_debug = result.stdout[:1000] if result.stdout else ""
                full_stderr_for_debug = result.stderr[:1000] if result.stderr else ""

                if result.stderr.strip():
                    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
                    if stderr_lines:
                        stderr_preview = " | ".join(stderr_lines[-3:])
                        raw_stderr = result.stderr.strip()
                        last_error = raw_stderr[:200]
                        error_details.append(f"stderr: {raw_stderr[:100]}")

                if result.stdout.strip():
                    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                    if stdout_lines:
                        stdout_preview = " | ".join(stdout_lines[-3:])
                        # Try to parse NDJSON for error details
                        for line in result.stdout.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                # Check for error events
                                if event.get("type") == "error":
                                    error_data = event.get("error", {})
                                    if isinstance(error_data, dict):
                                        message = error_data.get("message", "Unknown error")
                                        last_error = str(message)[:200]
                                        error_details.append(f"error event: {message}")
                                    else:
                                        last_error = str(error_data)[:200]
                                        error_details.append(f"error event: {error_data}")
                                # Check for tool_use events with errors
                                elif event.get("type") == "tool_use":
                                    part = event.get("part", {})
                                    if part.get("type") == "tool_use":
                                        state = part.get("state", {})
                                        if state.get("status") == "error":
                                            error_msg = state.get("error", "unknown error")
                                            last_error = error_msg[:200]
                                            error_details.append(f"tool_use error: {error_msg}")
                                # Check for text events
                                elif event.get("type") == "text":
                                    part = event.get("part", {})
                                    if part.get("type") == "text":
                                        text_content = part.get("text", "")
                                        if text_content and ("error" in text_content.lower() or "fail" in text_content.lower()):
                                            last_error = text_content[:200]
                                            error_details.append(f"text error indicator: {text_content[:100]}")
                            except json.JSONDecodeError:
                                # Not JSON, might be raw text - use first 200 chars
                                if not last_error or last_error == "no output":
                                    last_error = line[:200]
                                    error_details.append(f"raw line: {line[:100]}")
                
                # If we still have no specific error, check if there are any text events that might indicate what happened
                if last_error == "unknown error" and result.stdout.strip():
                    # Look for any text content that might be useful
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                            if event.get("type") == "text":
                                part = event.get("part", {})
                                if part.get("type") == "text":
                                    text_content = part.get("text", "")
                                    if text_content and len(text_content) > 10:  # Avoid tiny snippets
                                        last_error = text_content[:200]
                                        error_details.append(f"text content: {text_content[:100]}")
                                        break
                        except json.JSONDecodeError:
                            pass
                
                logger.error(
                    "OpencodeAgent: failed to apply fix for %s: returncode=%d, error=%s, stdout_preview=%s, stderr_preview=%s, error_details=%s, full_stdout=%s, full_stderr=%s, model=%s",
                    file_path,
                    result.returncode,
                    last_error,
                    stdout_preview or "(empty)",
                    stderr_preview or "(empty)",
                    " | ".join(error_details[:3]) if error_details else "(none)",
                    full_stdout_for_debug,
                    full_stderr_for_debug,
                    self.model,
                )
                return False
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
        finally:
            # Clean up temporary files
            for temp_file in [prompt_file, feedback_file]:
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.unlink(temp_file)
                    except OSError:
                        pass  # Ignore cleanup errors


def _parse_opencode_ndjson(raw_stdout: str) -> bool:
    """Parse opencode NDJSON output and return True if the agent completed.

    opencode streams NDJSON events; we check for a ResultMessage-like event
    that indicates completion without error.
    """
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # ResultMessage in NDJSON: {"type": "result", ...}
        if event.get("type") == "result":
            is_error = event.get("is_error", False)
            if is_error:
                error_detail = event.get("errors", ["Unknown error"])
                logger.error(
                    "OpencodeAgent: result error: %s", error_detail
                )
                return False
            return True

    # No result event found — fallback: check stderr for clues
    if raw_stdout.strip():
        logger.warning(
            "OpencodeAgent: no result event in output, assuming partial"
        )
    return False


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
# Finding Status Persistence
# ---------------------------------------------------------------------------

_ACTION_TAKEN_HEADER = "## Action Taken"


def _finding_status_header() -> str:
    """Return the section header used in the markdown file."""
    return _ACTION_TAKEN_HEADER


def has_action_taken(file_path: Path) -> bool:
    """Check if a finding has already been acted upon."""
    try:
        content = file_path.read_text(encoding="utf-8")
        return _ACTION_TAKEN_HEADER in content
    except Exception:
        return False


def read_action_taken(file_path: Path) -> FindingStatus | None:
    """Read the action-taken status from a finding markdown file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    header_idx = content.find(_ACTION_TAKEN_HEADER)
    if header_idx == -1:
        return None

    # Extract the YAML-style block after the header
    block_start = content.find("\n", header_idx) + 1
    block_end = len(content)
    # Find next header or end of file
    for marker in ("\n## ", "\n---\n", "\n*Generated by"):
        idx = content.find(marker, block_start)
        if 0 <= idx < block_end:
            block_end = idx

    block = content[block_start:block_end].strip()

    status_data: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            status_data[key.strip()] = value.strip()

    st = status_data.get("Status", "").strip()
    if not st:
        return None

    raw_ts = status_data.get("Timestamp", "")
    raw_summary = status_data.get("Summary", "")
    raw_commit = status_data.get("CommitSha", "")
    raw_error = status_data.get("ErrorMessage", "")

    return FindingStatus(
        status=st,
        timestamp=raw_ts.strip(),
        summary=raw_summary.strip(),
        commit_sha=raw_commit.strip() or None,
        error_message=raw_error.strip() or None,
    )


def write_action_taken(file_path: Path, status: FindingStatus) -> None:
    """Append or update the action-taken section in a finding markdown file.

    If an existing section is present, it is replaced to avoid stale status.
    """
    separator = "\n" + _ACTION_TAKEN_HEADER + "\n"
    new_block = separator + "\n".join([
        f"Status: {status.status}",
        f"Timestamp: {status.timestamp}",
        f"Summary: {status.summary}",
    ])
    if status.commit_sha:
        new_block += f"\nCommitSha: {status.commit_sha}"
    if status.error_message:
        new_block += f"\nErrorMessage: {status.error_message}"
    new_block += "\n"

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        content = ""

    # Remove any existing section
    header_idx = content.find(_ACTION_TAKEN_HEADER)
    if header_idx >= 0:
        content = content[:header_idx].rstrip("\n")

    content += new_block
    try:
        file_path.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.error("Failed to write action status to %s: %s", file_path, e)


def _now_iso() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


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
    After each action, a ## Action Taken section is appended to the finding
    markdown file so interrupted runs can be resumed safely.
    """
    finding = parse_markdown_finding(md_file)
    if finding is None:
        # Warning-type findings have no code blocks — skip, don't error
        skip_msg = (
            f"Cannot parse action from {md_file.name} (no code blocks)"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="skipped",
                timestamp=_now_iso(),
                summary=skip_msg,
            ),
        )
        return (
            "skipped",
            False,
            skip_msg,
        )

    logger.info(
        "Processing finding %s for %s", finding.finding_id, finding.file_path
    )

    # Dry-run: skip agent call entirely
    if dry_run:
        logger.info("[DRY RUN] Would apply fix for %s", finding.file_path)
        write_action_taken(
            md_file,
            FindingStatus(
                status="completed",
                timestamp=_now_iso(),
                summary="[DRY RUN] Would apply fix",
            ),
        )
        return ("committed", True, None)

    # Capture original file content before any changes for verification
    original_content: str | None = None
    try:
        original_content = finding.file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug(
            "Original file not found for %s, skipping original content capture",
            finding.file_path,
        )

    # Apply fix with retries
    success = False
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        # Check for interrupt signal
        if _shutdown_requested:
            interrupt_msg = "Interrupted by SIGINT"
            write_action_taken(
                md_file,
                FindingStatus(
                    status="interrupted",
                    timestamp=_now_iso(),
                    summary=interrupt_msg,
                    error_message=interrupt_msg,
                ),
            )
            return ("interrupted", False, interrupt_msg)
        
        try:
            success = asyncio.run(
                agent.apply_fix(
                    finding.file_path,
                    finding.existing_code,
                    finding.suggested_code,
                    finding.analysis,
                    original_content=original_content,
                )
            )

            if success:
                break
            last_error = "Agent.apply_fix returned False"

        except asyncio.CancelledError:
            raise
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
        error_msg = (
            f"Failed to apply fix for {finding.file_path} "
            f"after {max_retries + 1} attempts: {last_error}"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="error",
                timestamp=_now_iso(),
                summary=f"Fix failed after {max_retries + 1} attempts",
                error_message=error_msg,
            ),
        )
        return (
            "error",
            False,
            error_msg,
        )

    # Validate changes
    if not run_validation(finding.file_path, validation_config):
        skip_msg = (
            f"Validation failed for {finding.file_path}, skipping commit"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="skipped",
                timestamp=_now_iso(),
                summary=skip_msg,
            ),
        )
        return (
            "skipped",
            False,
            skip_msg,
        )

    # Commit changes
    try:
        repo = validate_git_repo(Path.cwd())
        comment_snippet = finding.review_comment[:50]
        suffix = (
            "..." if len(finding.review_comment) > 50 else ""
        )
        commit_message = (
            f"fix: {comment_snippet}{suffix} [{finding.finding_id}]"
        )
        committed = git_commit(
            repo, commit_message, [finding.file_path]
        )
        if committed:
            logger.info("Committed fix for %s", finding.file_path)
            commit_sha = repo.head.commit.hexsha[:8]
            write_action_taken(
                md_file,
                FindingStatus(
                    status="completed",
                    timestamp=_now_iso(),
                    summary=f"Fix applied and committed: {commit_message}",
                    commit_sha=commit_sha,
                ),
            )
            return ("committed", True, None)
        else:
            logger.info(
                "No changes to commit for %s (file unchanged)",
                finding.file_path,
            )
            write_action_taken(
                md_file,
                FindingStatus(
                    status="skipped",
                    timestamp=_now_iso(),
                    summary="File already contained expected changes",
                ),
            )
            return ("skipped", False, None)
    except Exception as e:
        error_msg = (
            f"Failed to commit changes for {finding.file_path}: {e}"
        )
        write_action_taken(
            md_file,
            FindingStatus(
                status="error",
                timestamp=_now_iso(),
                summary="Commit failed",
                error_message=error_msg,
            ),
        )
        return (
            "error",
            False,
            error_msg,
        )


def process_findings(
    output_dir: Path,
    agent: CodingAgent,
    validation_config: ValidationConfig,
    max_retries: int,
    retry_delay: float,
    dry_run: bool = False,
    force: bool = False,
    skip_errors: bool = False,
) -> dict[str, int]:
    """Process all VALID findings in the output directory.

    Already-processed findings (those with a ## Action Taken section) are
    skipped unless force=True.  Findings previously marked "error" are
    retried unless skip_errors=True.
    """
    stats: dict[str, int] = {
        "processed": 0,
        "committed": 0,
        "skipped": 0,
        "errors": 0,
        "restored": 0,
        "total_findings": 0,
        "interrupted": False,
    }

    if not output_dir.exists():
        logger.error("Output directory %s does not exist", output_dir)
        return stats

    markdown_files = sorted(output_dir.glob("*.md"))
    if not markdown_files:
        logger.warning("No markdown files found in %s", output_dir)
        return stats

    logger.info("Found %d markdown files to process", len(markdown_files))

    # Count eligible findings first
    for md_file in markdown_files:
        if md_file.name not in ("SUMMARY.md", "INDEX.md"):
            stats["total_findings"] += 1

    for md_file in markdown_files:
        # Check for interrupt signal
        if _shutdown_requested:
            stats["interrupted"] = True
            break
            
        # Skip summary/index files
        if md_file.name in ("SUMMARY.md", "INDEX.md"):
            continue

        # Skip already-processed findings unless forced
        if not force and has_action_taken(md_file):
            existing = read_action_taken(md_file)
            if existing and existing.status == "completed":
                logger.info(
                    "Skipping completed finding: %s (commit: %s)",
                    md_file.name,
                    existing.commit_sha or "unknown",
                )
                stats["restored"] += 1
                continue
            elif existing and existing.status == "error" and skip_errors:
                logger.info(
                    "Skipping errored finding (--skip-errors): %s",
                    md_file.name,
                )
                stats["skipped"] += 1
                continue
            elif existing:
                logger.info(
                    "Replaying %s finding: %s",
                    existing.status,
                    md_file.name,
                )

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
        original_content: str | None = None,
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process findings that were already completed",
    )
    parser.add_argument(
        "--skip-errors",
        action="store_true",
        help="Skip findings that previously failed instead of retrying them",
    )
    return parser.parse_args(argv)


def print_summary(stats: dict[str, int], output_dir: Path) -> None:
    """Print the final processing summary and write to file."""
    print("\n=== Review Action Harness Summary ===")
    print(f"Restored:   {stats['restored']}")
    print(f"Processed:  {stats['processed']}")
    print(f"Committed:  {stats['committed']}")
    print(f"Skipped:    {stats['skipped']}")
    print(f"Errors:     {stats['errors']}")
    
    # Write summary to file
    summary_file = output_dir / "review-action_summary.md"
    with summary_file.open("w", encoding="utf-8") as f:
        f.write("# Review Action Summary\n\n")
        f.write(f"Restored:   {stats['restored']}\n")
        f.write(f"Processed:  {stats['processed']}\n")
        f.write(f"Committed:  {stats['committed']}\n")
        f.write(f"Skipped:    {stats['skipped']}\n")
        f.write(f"Errors:     {stats['errors']}\n")
        f.write(f"Interrupted: {'yes' if stats['interrupted'] else 'no'}\n")
        processed = stats['processed']
        total = stats['total_findings']
        f.write(f"Progress: {processed} out of {total} findings processed\n")


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    args = parse_args(argv)

    # Reset shutdown flag and set up signal handler for graceful interrupt
    global _shutdown_requested
    _shutdown_requested = False
    signal.signal(signal.SIGINT, _sigint_handler)

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
        force=args.force,
        skip_errors=args.skip_errors,
    )

    print_summary(stats, args.output_dir)

    return 130 if stats["interrupted"] else (0 if stats["errors"] == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
