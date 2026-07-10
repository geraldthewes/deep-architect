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
    max_turns: int | None = None


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
        review_comment: str = "",
    ) -> bool:
        """Apply a fix to a file. Returns True if successful."""

    async def fix_check_failures(
        self,
        files: list[Path],
        failure_report: str,
        context: str = "",
    ) -> bool:
        """Address quality-check failures introduced by a prior fix attempt."""

    async def run_structured(
        self,
        system_prompt: str,
        prompt: str,
        label: str = "structured",
    ) -> str:
        """Run a one-shot, tool-free prompt through the backend; return raw text.

        The prompt embeds everything needed — no file tools are used. Raises
        RuntimeError on CLI/process failure or empty output. JSON-schema
        enforcement is the caller's job (parse-and-retry), since the CLIs
        cannot enforce a schema server-side.
        """


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

    if normalized_suggested.strip() and normalized_current.strip() == normalized_suggested.strip():
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


def format_suggested_code_section(suggested_code: str) -> str:
    """Render the Suggested Code section, or an instruction to derive it.

    Some findings (prose-only review comments with no concrete replacement)
    have no suggested code. In that case the agent must work out the edit
    from the Review Comment and Analysis instead of being shown an empty
    fenced block, which would read as "replace this code with nothing."
    """
    if suggested_code.strip():
        return f"**Suggested Code**:\n```\n{suggested_code}\n```\n\n"
    return (
        "No suggested code was provided. Derive the exact change from the "
        "Review Comment and Analysis below, then apply it to the Existing "
        "Code shown above. Change nothing else.\n\n"
    )


def _normalize_block(text: str) -> str:
    """Stripped, blank-free lines - tolerant matching of code snippets."""
    lines = (ln.strip() for ln in text.replace("\r\n", "\n").split("\n"))
    return "\n".join(ln for ln in lines if ln)


def finding_already_satisfied(
    file_content: str, existing_code: str, suggested_code: str
) -> str | None:
    """Return a human reason if the fix is a no-op, else None.

    - suggested_code already present  -> "already applied"
    - existing_code anchor absent     -> "stale/obsolete anchor"
    Empty existing_code (pure addition) is never treated as stale.
    """
    body = _normalize_block(file_content)
    sugg = _normalize_block(suggested_code)
    if sugg and sugg in body:
        return "Already applied — file already reflects the suggested code"
    anchor = _normalize_block(existing_code)
    if anchor and anchor not in body:
        return (
            "Stale finding — target code not found in file "
            "(already changed or removed elsewhere)"
        )
    return None
