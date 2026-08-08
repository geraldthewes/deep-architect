from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from deep_architect.logger import get_logger

logger = get_logger(__name__)

# Agent free-text that indicates an intentional no-op (fix already on disk /
# finding obsolete) rather than a failed edit. Used only when the file is
# byte-identical to the pre-apply snapshot — never overrides a real failure
# path where the agent never completed.
_ALREADY_DONE_RE = re.compile(
    r"(?is)"
    r"(?<!\bnot\s)(?<!\bn't\s)"
    r"(?:"
    r"already\s+(?:fixed|applied|addressed|implemented|done|present|renamed|"
    r"correct(?:ly)?|in\s+place)|"
    r"no changes?\s+needed|"
    r"nothing to (?:do|change)|"
    r"fix (?:is|was|has been)\s+already|"
    r"feedback (?:has\s+)?already|"
    r"already been (?:applied|addressed|implemented|fixed)|"
    r"tests? (?:are|were|have been)\s+already"
    r")"
)

# def/class/async def names introduced by suggested code (not placeholders).
_DEF_NAME_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_CLASS_NAME_RE = re.compile(
    r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]",
    re.MULTILINE,
)
# Suggested-code noise we never treat as "must be present" evidence.
_PLACEHOLDER_LINE_RE = re.compile(
    r"(\{[.]{2,}\}|\b\.\.\.\b|\bTODO\b|\bFIXME\b|"
    r"\bConsider\b|\bor document\b)",
    re.IGNORECASE,
)


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


def _agent_reports_already_done(agent_response_text: str | None) -> bool:
    """True when agent free-text clearly claims the fix is already on disk."""
    if not agent_response_text or not agent_response_text.strip():
        return False
    return _ALREADY_DONE_RE.search(agent_response_text) is not None


def _file_reflects_fix(
    file_path: Path,
    suggested_code: str,
    original_content: str | None,
    agent_response_text: str | None = None,
    existing_code: str = "",
) -> bool:
    """Check whether file_path's current content shows a fix was applied.

    A coding agent reporting success is not proof it actually edited the
    file, so agents verify against the file on disk before trusting the
    report. Returns True when:

    - the file matches ``suggested_code`` exactly, or
    - the file differs from ``original_content`` (some edit landed), or
    - the file is unchanged but the finding is already satisfied
      (suggested content / new symbols present, or stale anchor), or
    - the file is unchanged and the agent clearly reports the fix is
      already applied (intentional no-op — not a failed edit).

    Returns False only when the file is unchanged *and* we have no
    evidence the finding is already resolved (lazy/failed agent).
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

        # Unchanged on disk. Prefer structural evidence over agent prose.
        reason = finding_already_satisfied(
            current_content, existing_code, suggested_code
        )
        if reason is not None:
            logger.info(
                "No disk delta for %s but finding already satisfied: %s",
                file_path,
                reason,
            )
            return True
        if _agent_reports_already_done(agent_response_text):
            response_preview = (agent_response_text or "").strip()[:500]
            logger.info(
                "No disk delta for %s; agent reports already done — "
                "treating as satisfied. Agent's response: %s",
                file_path,
                response_preview,
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


def _extract_def_class_names(code: str) -> set[str]:
    """Return def/class names declared in *code*."""
    names = set(_DEF_NAME_RE.findall(code))
    names.update(_CLASS_NAME_RE.findall(code))
    return names


def _substantial_new_lines(suggested: str, existing: str) -> list[str]:
    """Lines in suggested (normalized) that are not in existing and not noise."""
    existing_lines = set(_normalize_block(existing).split("\n")) if existing.strip() else set()
    out: list[str] = []
    for ln in _normalize_block(suggested).split("\n"):
        if not ln or ln in existing_lines:
            continue
        if _PLACEHOLDER_LINE_RE.search(ln):
            continue
        # Skip pure punctuation / very short fragments.
        if len(ln) < 8:
            continue
        out.append(ln)
    return out


def finding_already_satisfied(
    file_content: str, existing_code: str, suggested_code: str
) -> str | None:
    """Return a human reason if the fix is a no-op, else None.

    - suggested_code already present           -> "already applied"
    - new defs/classes from suggested present  -> "already applied"
    - most distinctive new suggested lines in  -> "already applied"
    - existing_code anchor absent              -> "stale/obsolete anchor"
    Empty existing_code (pure addition) is never treated as stale.
    """
    body = _normalize_block(file_content)
    sugg = _normalize_block(suggested_code)
    if sugg and sugg in body:
        return "Already applied — file already reflects the suggested code"

    # Additive / partial fixes: suggested introduces new symbols or lines that
    # are already on disk even though the full suggested block does not match
    # (e.g. placeholder sketches, or a better fix already landed).
    if suggested_code.strip():
        existing_names = _extract_def_class_names(existing_code)
        new_names = _extract_def_class_names(suggested_code) - existing_names
        present_names = [n for n in sorted(new_names) if n in file_content]
        if new_names and len(present_names) == len(new_names):
            shown = ", ".join(f"`{n}`" for n in present_names[:5])
            return (
                "Already applied — suggested symbols already present in file "
                f"({shown})"
            )

        new_lines = _substantial_new_lines(suggested_code, existing_code)
        if new_lines:
            matched = sum(1 for ln in new_lines if ln in body)
            # Require strong evidence: all lines, or ≥80% when several exist.
            if matched == len(new_lines) or (
                len(new_lines) >= 3 and matched / len(new_lines) >= 0.8
            ):
                return (
                    "Already applied — distinctive suggested changes "
                    "already present in file"
                )

    anchor = _normalize_block(existing_code)
    if anchor and anchor not in body:
        return (
            "Stale finding — target code not found in file "
            "(already changed or removed elsewhere)"
        )
    return None
