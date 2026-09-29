"""Resolve why a change was made and fit it to OCR's background limit.

OCR reads the text as ``ocr review --background-file``. That flag aborts above
8000 characters. This module loads a ticket or an explicit requirements file,
passes it through when it fits, and summarizes it when it does not.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from deep_architect.logger import get_logger

logger = get_logger(__name__)

OCR_BACKGROUND_HARD_LIMIT = 8000
MAX_INTENT_READ_BYTES = 8 * 1024 * 1024
BACKGROUND_OPEN_TAG = "<ocr_user_background>"
BACKGROUND_CLOSE_TAG = "</ocr_user_background>"
INTENT_FILENAME = "intent.md"
INTENT_SOURCE_FILENAME = "intent-source.md"
INTENT_SUMMARY_FILENAME = "intent-summary.md"
INTENT_META_FILENAME = "intent-meta.json"
DEFAULT_INTENT_MODEL = "standard/coder"
DEFAULT_INTENT_TIMEOUT = 300

INTENT_INSTRUCTION = (
    "This is why the change was made. Do not report behavior this intent "
    "requires as a defect. Do report an implementation that contradicts this "
    "intent, or that misses a stated requirement, when the diff shows it."
)

_TICKET_ID_RE = re.compile(r"PROJ-(\d+)", re.IGNORECASE)
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_FENCE_RE = re.compile(r"^```[^\n]*\n([\s\S]*?)\n?```\s*$")


class IntentError(Exception):
    """The intent cannot be prepared for OCR."""


class IntentDeclined(IntentError):
    """The operator declined summarization or rejected the summary."""


@dataclass(frozen=True)
class PreparedIntent:
    """Intent handed to OCR and the analyzer for one run."""

    label: str
    path: Path | None


def sanitize_markdown(text: str) -> str:
    """Match OCR's background cleanup so the character count agrees.

    Keeps newlines and tabs, drops other C0/C1 controls and Unicode format
    characters, collapses runs of blank lines, and trims the ends.
    """
    kept: list[str] = []
    for char in text:
        if char in "\n\t":
            kept.append(char)
            continue
        if char == "\r":
            continue
        code = ord(char)
        if code <= 0x1F or 0x7F <= code <= 0x9F:
            continue
        if unicodedata.category(char) == "Cf":
            continue
        kept.append(char)
    collapsed = _MULTI_NEWLINE_RE.sub("\n\n", "".join(kept))
    return collapsed.strip()


def measured_length(text: str) -> int:
    """Character count OCR uses for the background hard limit."""
    return len(sanitize_markdown(text))


def ticket_id_in(name: str) -> str | None:
    """Return ``PROJ-<digits>`` found in *name*, or None."""
    match = _TICKET_ID_RE.search(name)
    if match is None:
        return None
    return f"PROJ-{match.group(1)}"


def render_intent(body: str) -> str:
    """Prefix *body* with the review instruction, once."""
    cleaned = sanitize_markdown(body)
    if cleaned.startswith(INTENT_INSTRUCTION):
        return cleaned + "\n"
    return f"{INTENT_INSTRUCTION}\n\n{cleaned}\n"


def summary_budget() -> int:
    """Characters the summary body may use under the OCR hard limit."""
    # Two newlines sit between the instruction and the body.
    return OCR_BACKGROUND_HARD_LIMIT - len(INTENT_INSTRUCTION) - 2


def ask_yes_no(question: str) -> bool:
    """Read a y/n answer from stdin. EOF counts as no."""
    while True:
        try:
            answer = input(f"{question} [y/n] ").strip().lower()
        except EOFError:
            print()
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Answer y or n.")


def default_ask_summarize(characters: int) -> bool:
    """Ask whether to summarize an over-limit intent."""
    return ask_yes_no(
        f"Intent is {characters} characters. OCR accepts at most "
        f"{OCR_BACKGROUND_HARD_LIMIT}. Summarize it for the review?"
    )


def default_ask_accept(summary: str) -> bool:
    """Show *summary* and ask whether to use it."""
    print(summary)
    print()
    return ask_yes_no("Use this summary as the review intent?")


def prepare_review_intent(
    *,
    run_dir: Path,
    repo_root: Path,
    source: str,
    knowledge_dir: Path | None,
    background: str | None,
    background_file: Path | None,
    branch_name: str | None,
    reuse_existing: bool,
    interactive: bool,
    ask_summarize: Callable[[int], bool] | None = None,
    ask_accept: Callable[[str], bool] | None = None,
    summarize: Callable[[str], str] | None = None,
) -> PreparedIntent:
    """Resolve intent, fit it to OCR's limit, and write ``intent.md``.

    *reuse_existing* is the resume path: a saved ``intent.md`` is returned
    as-is, and a run with no saved intent stays without one. A new run with
    no ticket and no ``--background`` returns a ``none`` result and writes
    nothing.
    """
    run_dir = Path(run_dir)
    if reuse_existing:
        return _reuse_saved_intent(
            run_dir,
            background=background,
            background_file=background_file,
        )

    document = _resolve_document(
        repo_root=repo_root,
        source=source,
        knowledge_dir=knowledge_dir,
        background=background,
        background_file=background_file,
        branch_name=branch_name,
    )
    if document is None:
        logger.info("No change intent (no --background and no PROJ ticket)")
        return PreparedIntent(label="none", path=None)

    body = sanitize_markdown(document.text)
    if not body:
        raise IntentError(f"Intent from {document.label} is empty")
    _reject_reserved_tags(body, "Intent")
    rendered = render_intent(body)
    if measured_length(rendered) <= OCR_BACKGROUND_HARD_LIMIT:
        path = _write_intent(run_dir, rendered, label=document.label)
        logger.info(
            "Intent fits OCR limit (%d characters) from %s",
            measured_length(rendered),
            document.label,
        )
        return PreparedIntent(label=document.label, path=path)

    fitted, summarized = _fit_over_limit(
        body,
        run_dir=run_dir,
        interactive=interactive,
        ask_summarize=ask_summarize or default_ask_summarize,
        ask_accept=ask_accept or default_ask_accept,
        summarize=summarize,
    )
    label = f"summarized from {document.label}" if summarized else document.label
    path = _write_intent(run_dir, render_intent(fitted), label=label)
    if summarized:
        source_path = run_dir / INTENT_SOURCE_FILENAME
        source_path.write_text(sanitize_markdown(body) + "\n", encoding="utf-8")
        logger.info(
            "Intent summarized (%d -> %d characters). Wrote %s",
            measured_length(body),
            measured_length(render_intent(fitted)),
            path,
        )
    return PreparedIntent(label=label, path=path)


def summarize_with_opencode(
    prompt: str,
    *,
    model: str = DEFAULT_INTENT_MODEL,
    timeout: int = DEFAULT_INTENT_TIMEOUT,
) -> str:
    """Run one opencode completion and return its text."""
    bin_path = os.environ.get(
        "OPENCODE_BIN", "/home/gerald/.opencode/bin/opencode"
    )
    try:
        result = subprocess.run(
            [bin_path, "run", "--model", model, "--format", "json", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IntentError(f"opencode binary not found: {bin_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise IntentError(
            f"intent summarizer timed out after {timeout}s"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise IntentError(
            f"opencode exited with code {result.returncode}: {detail[:300]}"
        )
    text = _strip_fence(extract_opencode_text(result.stdout)).strip()
    if not text:
        raise IntentError("intent summarizer returned no text")
    return text


def extract_opencode_text(raw_stdout: str) -> str:
    """Concatenate text parts from an opencode ``--format json`` stream."""
    chunks: list[str] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = event.get("content", "")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if isinstance(text, str):
                        chunks.append(text)
        part = event.get("part", {})
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text", "")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def summary_prompt(source: str, budget: int) -> str:
    """Ask for a review-oriented condensation of *source*."""
    return (
        "You are condensing a change ticket or requirements document into the "
        "background a code reviewer needs.\n\n"
        f"Write at most {budget} characters.\n\n"
        "Include only:\n"
        "- why the change was made\n"
        "- the behavior it requires\n"
        "- constraints\n"
        "- what is out of scope\n"
        "- success criteria\n\n"
        "Drop research notes, history, status boilerplate, and implementation "
        "diary.\n\n"
        "Reply with the background text only. No preamble.\n\n"
        f"<source>\n{source}\n</source>\n"
    )


def retry_summary_prompt(
    source: str, previous: str, budget: int, previous_length: int
) -> str:
    """Second attempt after a summary missed the character budget."""
    return (
        f"The previous summary was {previous_length} characters. "
        f"Rewrite it in at most {budget} characters. "
        "Keep why the change was made, the required behavior, constraints, "
        "out of scope, and success criteria. "
        "Reply with the background text only.\n\n"
        f"<previous>\n{previous}\n</previous>\n\n"
        f"<source>\n{source}\n</source>\n"
    )


@dataclass(frozen=True)
class _IntentDocument:
    label: str
    text: str


def _reuse_saved_intent(
    run_dir: Path,
    *,
    background: str | None,
    background_file: Path | None,
) -> PreparedIntent:
    existing = run_dir / INTENT_FILENAME
    supplied = bool(background and background.strip()) or background_file is not None
    if existing.is_file():
        if supplied:
            logger.warning(
                "Resume keeps the saved intent at %s; new --background is ignored",
                existing,
            )
        label = _read_label(run_dir)
        logger.info("Reusing saved intent %s (%s)", existing, label)
        return PreparedIntent(label=label, path=existing.resolve())
    if supplied:
        logger.warning(
            "Resume has no saved intent; --background is not applied. "
            "Pass --no-resume to start a run with this intent."
        )
    else:
        logger.info("Resuming run has no saved intent")
    return PreparedIntent(label="none", path=None)


def _resolve_document(
    *,
    repo_root: Path,
    source: str,
    knowledge_dir: Path | None,
    background: str | None,
    background_file: Path | None,
    branch_name: str | None,
) -> _IntentDocument | None:
    inline = (background or "").strip()
    if inline or background_file is not None:
        file_text = ""
        if background_file is not None:
            file_text = _read_text(Path(background_file))
        merged = _merge(inline, file_text)
        if not merged:
            raise IntentError("Intent background is empty")
        return _IntentDocument(label="flag", text=merged)

    ticket_id = ticket_id_in(source) or (
        ticket_id_in(branch_name) if branch_name else None
    )
    if ticket_id is None:
        return None
    knowledge = (
        Path(knowledge_dir) if knowledge_dir is not None else repo_root / "knowledge"
    )
    ticket_path = knowledge / "tickets" / f"{ticket_id}.md"
    if not ticket_path.is_file():
        logger.warning(
            "No ticket file for %s at %s; reviewing without intent",
            ticket_id,
            ticket_path,
        )
        return None
    return _IntentDocument(label=f"ticket {ticket_id}", text=_read_text(ticket_path))


def _merge(inline: str, file_text: str) -> str:
    left = sanitize_markdown(inline)
    right = sanitize_markdown(file_text)
    if not left:
        return right
    if not right:
        return left
    return f"{left}\n\n{right}"


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise IntentError(f"Intent file not found: {path}")
    size = path.stat().st_size
    if size > MAX_INTENT_READ_BYTES:
        raise IntentError(
            f"Intent file {path} is {size} bytes, over the "
            f"{MAX_INTENT_READ_BYTES} byte limit"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise IntentError(f"Intent file {path} is not UTF-8") from exc


def _reject_reserved_tags(text: str, what: str) -> None:
    if BACKGROUND_OPEN_TAG in text or BACKGROUND_CLOSE_TAG in text:
        raise IntentError(
            f"{what} must not contain {BACKGROUND_OPEN_TAG!r} or "
            f"{BACKGROUND_CLOSE_TAG!r}"
        )


def _fit_over_limit(
    body: str,
    *,
    run_dir: Path,
    interactive: bool,
    ask_summarize: Callable[[int], bool],
    ask_accept: Callable[[str], bool],
    summarize: Callable[[str], str] | None,
) -> tuple[str, bool]:
    original_length = measured_length(render_intent(body))
    if interactive and not ask_summarize(original_length):
        raise IntentDeclined(
            f"Intent is {original_length} characters, over OCR's "
            f"{OCR_BACKGROUND_HARD_LIMIT} character limit. Not summarizing."
        )
    budget = summary_budget()
    if budget < 1:
        raise IntentError("Intent instruction exceeds OCR's background limit")
    cleaned_source = sanitize_markdown(body)
    summary = _one_summary(summary_prompt(cleaned_source, budget), summarize)
    if measured_length(render_intent(summary)) > OCR_BACKGROUND_HARD_LIMIT:
        summary = _one_summary(
            retry_summary_prompt(
                cleaned_source,
                summary,
                budget,
                measured_length(render_intent(summary)),
            ),
            summarize,
        )
    if measured_length(render_intent(summary)) > OCR_BACKGROUND_HARD_LIMIT:
        raise IntentError(
            "Summarizer stayed over OCR's "
            f"{OCR_BACKGROUND_HARD_LIMIT} character limit after one retry"
        )
    if interactive and not ask_accept(summary):
        run_dir.mkdir(parents=True, exist_ok=True)
        rejected = run_dir / INTENT_SUMMARY_FILENAME
        rejected.write_text(summary + "\n", encoding="utf-8")
        raise IntentDeclined(
            f"Summary rejected. It was left at {rejected}. "
            "Edit it and re-run with --background-file."
        )
    if not interactive:
        logger.info(
            "Intent is %d characters, over OCR's %d limit; "
            "summarizing without a prompt (stdin is not a TTY)",
            original_length,
            OCR_BACKGROUND_HARD_LIMIT,
        )
    return summary, True


def _one_summary(prompt: str, summarize: Callable[[str], str] | None) -> str:
    if summarize is not None:
        text = summarize(prompt)
    else:
        text = summarize_with_opencode(prompt)
    cleaned = _strip_reserved(_strip_fence(text).strip())
    if not cleaned:
        raise IntentError("intent summarizer returned no text")
    return cleaned


def _strip_reserved(text: str) -> str:
    return text.replace(BACKGROUND_OPEN_TAG, "").replace(BACKGROUND_CLOSE_TAG, "")


def _strip_fence(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    if match is None:
        return text.strip()
    return match.group(1).strip()


def _write_intent(run_dir: Path, rendered: str, *, label: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / INTENT_FILENAME
    path.write_text(rendered if rendered.endswith("\n") else rendered + "\n", encoding="utf-8")
    meta = run_dir / INTENT_META_FILENAME
    meta.write_text(
        json.dumps({"label": label}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _read_label(run_dir: Path) -> str:
    meta = run_dir / INTENT_META_FILENAME
    if not meta.is_file():
        return "flag"
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Unreadable intent metadata at %s", meta)
        return "flag"
    label = payload.get("label") if isinstance(payload, dict) else None
    if isinstance(label, str) and label.strip():
        return label.strip()
    return "flag"
