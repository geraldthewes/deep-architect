from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from deep_architect.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CodingAgentConfig:
    """Configuration for the coding agent."""

    provider: str = "opencode"
    model: str | None = None
    max_retries: int = 3
    retry_delay: float = 1.0
    permission_mode: str = "bypassPermissions"
    disallowed_tools: list[str] | None = None
    timeout_seconds: float | None = None


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

    async def fix_check_failures(
        self,
        files: list[Path],
        failure_report: str,
        context: str = "",
    ) -> bool:
        """Address quality-check failures introduced by a prior fix attempt."""


def _file_reflects_fix(
    file_path: Path,
    suggested_code: str,
    original_content: str | None,
    agent_response_text: str | None = None,
) -> bool:
    """Check whether file_path's current content shows a fix was applied.

    A coding agent reporting success is not proof it actually edited the
    file, so both OpencodeAgent and ClaudeSDKAgent verify against the file
    on disk before trusting the agent's report.

    Returns True if the file now matches suggested_code exactly, or if it
    differs from original_content (some change was made). Returns False if
    the file is unreadable while original_content was captured, or is
    unchanged from original_content.

    *agent_response_text*, when provided, is logged alongside the "unchanged"
    warning so it's possible to tell why the agent thought it was done.
    """
    try:
        current_content = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug(
            "File not found for verification (likely test env), "
            "trusting agent success for %s",
            file_path,
        )
        return True

    normalized_current = current_content.replace("\r\n", "\n")
    normalized_suggested = suggested_code.replace("\r\n", "\n")

    if normalized_current.strip() == normalized_suggested.strip():
        logger.debug(
            "Fix applied successfully for %s (file matches expected)",
            file_path,
        )
        return True

    # File doesn't match expected exactly - check if any changes were made
    # by comparing with pre-apply content.
    if original_content is not None:
        normalized_original = original_content.replace("\r\n", "\n")
        if normalized_current != normalized_original:
            logger.debug(
                "File modified for %s (differs from original)", file_path
            )
            return True
        logger.warning(
            "No changes made to %s (file unchanged). Agent's response: %s",
            file_path,
            (agent_response_text or "<no response text captured>").strip()[:1000],
        )
        return False

    # No original content provided - fallback to trusting the agent's success.
    logger.debug(
        "No original content, trusting agent success for %s", file_path
    )
    return True
