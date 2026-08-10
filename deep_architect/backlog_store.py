"""IDLC ``knowledge/backlog/`` create/update helpers for review-analyzer.

Writes pre-ticket backlog entries in the same directory used by
``/triage_critique`` (``knowledge/backlog/<slug>.md``). Does not create
``knowledge/tickets/`` entries; ticket matches are link-only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SLUG_MAX = 60
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_UPDATED_AT_RE = re.compile(r"^updated_at:\s*.+$", re.MULTILINE)


class BacklogAction(StrEnum):
    """Disposition of a BACKLOG finding against knowledge/."""

    CREATED = "created"
    UPDATED = "updated"
    LINKED_TO_TICKET = "linked_to_ticket"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class CatalogEntry:
    """Compact index row for prompts (no full file bodies)."""

    path: str  # relative path string, e.g. knowledge/backlog/foo.md
    title: str
    kind: str  # "backlog" | "ticket"
    ticket_id: str | None = None
    status: str | None = None
    occurrence_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Occurrence:
    """One analyzer finding that maps to a backlog (or ticket link)."""

    feedback_rel: str
    file_path: str
    lines: str | None  # e.g. "10-20" or None for warnings
    date: str  # YYYY-MM-DD
    analysis_preview: str


@dataclass
class PromotionResult:
    """Outcome of promoting one BACKLOG finding."""

    action: BacklogAction
    target: str | None = None
    rationale: str = ""
    title: str = ""
    error: str | None = None


@dataclass
class PromotionCounts:
    """Aggregate promotion stats for a run."""

    created: int = 0
    updated: int = 0
    linked_to_ticket: int = 0
    skipped: int = 0
    errors: int = 0

    def record(self, result: PromotionResult) -> None:
        if result.action == BacklogAction.CREATED:
            self.created += 1
        elif result.action == BacklogAction.UPDATED:
            self.updated += 1
        elif result.action == BacklogAction.LINKED_TO_TICKET:
            self.linked_to_ticket += 1
        elif result.action == BacklogAction.SKIPPED:
            self.skipped += 1
        else:
            self.errors += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "updated": self.updated,
            "linked_to_ticket": self.linked_to_ticket,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def slugify_title(title: str, *, max_len: int = _SLUG_MAX) -> str:
    """Slugify a title like ``/triage_critique`` backlog naming.

    Lowercase, non-alphanumeric → hyphens, collapse/strip hyphens, truncate
    to *max_len* at a word boundary when possible.
    """
    raw = title.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        slug = "untitled"
    if len(slug) <= max_len:
        return slug
    truncated = slug[:max_len]
    if "-" in truncated:
        # Prefer cutting at last full segment within the budget.
        cut = truncated.rsplit("-", 1)[0]
        if cut:
            return cut
    return truncated.rstrip("-") or truncated


def default_knowledge_dir(cwd: Path | None = None) -> Path:
    """Return ``<cwd>/knowledge`` (target repo layout)."""
    base = cwd if cwd is not None else Path.cwd()
    return base / "knowledge"


def backlog_dir(knowledge_dir: Path) -> Path:
    return knowledge_dir / "backlog"


def tickets_dir(knowledge_dir: Path) -> Path:
    return knowledge_dir / "tickets"


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract simple scalar frontmatter fields (title, id, status, …)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    block = match.group(1)
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line or line.strip().startswith("-"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and value and not key.startswith("#"):
            # Skip nested list keys like blank continuation; first scalar wins.
            if key not in fields:
                fields[key] = value
    return fields


def _parse_occurrence_files(text: str) -> tuple[str, ...]:
    """Extract unique ``file:`` paths from frontmatter ``occurrences:`` list.

    Missing or malformed occurrences yield an empty tuple (never raises).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return ()
    block = match.group(1)
    if "occurrences:" not in block:
        return ()

    files: list[str] = []
    seen: set[str] = set()
    in_occurrences = False
    for line in block.splitlines():
        stripped = line.strip()
        if not in_occurrences:
            if stripped == "occurrences:" or stripped.startswith("occurrences:"):
                in_occurrences = True
            continue
        # Leave occurrences block when a non-indented key appears.
        if stripped and not line.startswith((" ", "\t", "-")) and ":" in stripped:
            break
        # Match nested "file:" under list items (any indentation).
        file_match = re.match(r"^\s*-?\s*file:\s*(.+)$", line)
        if file_match:
            raw = file_match.group(1).strip().strip("\"'")
            if raw and raw not in seen:
                seen.add(raw)
                files.append(raw)
    return tuple(files)


def _catalog_entry_from_file(
    path: Path,
    *,
    knowledge_dir: Path,
    kind: str,
) -> CatalogEntry | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read catalog file %s: %s", path, exc)
        return None

    fields = _parse_frontmatter(text)
    title = fields.get("title")
    if not title:
        # Fallback: first markdown H1
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    if not title:
        title = path.stem.replace("-", " ")

    try:
        rel = path.relative_to(knowledge_dir.parent).as_posix()
    except ValueError:
        if kind == "ticket":
            rel = f"knowledge/tickets/{path.name}"
        else:
            rel = f"knowledge/backlog/{path.name}"

    occurrence_files = _parse_occurrence_files(text)

    return CatalogEntry(
        path=rel,
        title=title,
        kind=kind,
        ticket_id=fields.get("id") if kind == "ticket" else None,
        status=fields.get("status") if kind == "ticket" else None,
        occurrence_files=occurrence_files,
    )


def format_catalog_heads(catalog: list[CatalogEntry]) -> str:
    """Format compact catalog heads for prompt injection (no full bodies).

    Each entry includes path, title, kind, optional ticket fields, and
    occurrence file list.
    """
    if not catalog:
        return ""
    lines: list[str] = []
    for entry in catalog:
        parts = [
            f"path={entry.path}",
            f"title={entry.title}",
            f"kind={entry.kind}",
        ]
        if entry.ticket_id:
            parts.append(f"id={entry.ticket_id}")
        if entry.status:
            parts.append(f"status={entry.status}")
        if entry.occurrence_files:
            files = ", ".join(entry.occurrence_files)
            parts.append(f"files=[{files}]")
        else:
            parts.append("files=[]")
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def load_entry_body(knowledge_dir: Path, entry_path: str) -> str | None:
    """Read full markdown body for a catalog entry path.

    *entry_path* is typically relative to the repo root (parent of
    knowledge/), e.g. ``knowledge/backlog/foo.md``. Returns None on
    missing/unreadable files (logs a warning).
    """
    if not entry_path or not entry_path.strip():
        return None
    normalized = entry_path.strip().lstrip("./")
    candidates = [
        knowledge_dir.parent / normalized,
        knowledge_dir / normalized,
        Path(normalized),
    ]
    # Also try under backlog/tickets by basename.
    name = Path(normalized).name
    candidates.append(knowledge_dir / "backlog" / name)
    candidates.append(knowledge_dir / "tickets" / name)

    for cand in candidates:
        try:
            if cand.is_file():
                return cand.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Could not read catalog body %s: %s", cand, exc)
            return None
    log.warning("Catalog entry body not found for path %s", entry_path)
    return None


def rank_catalog_for_finding(
    catalog: list[CatalogEntry],
    finding_path: str,
) -> list[CatalogEntry]:
    """Stable sort: entries whose occurrence files intersect *finding_path* first.

    Does **not** drop any entry — file affinity only boosts rank.
    Matching is exact path equality, or either path as a suffix of the other
    (handles ``src/foo.py`` vs ``app/src/foo.py``).
    """
    if not catalog:
        return []
    finding_norm = (finding_path or "").strip().replace("\\", "/")

    def _path_affinity(occ: str) -> bool:
        if not finding_norm or not occ:
            return False
        occ_norm = occ.strip().replace("\\", "/")
        if occ_norm == finding_norm:
            return True
        if occ_norm.endswith("/" + finding_norm) or finding_norm.endswith("/" + occ_norm):
            return True
        return False

    def _matches(entry: CatalogEntry) -> bool:
        return any(_path_affinity(occ) for occ in entry.occurrence_files)

    # Two-pass stable partition preserving original relative order.
    boosted: list[CatalogEntry] = []
    rest: list[CatalogEntry] = []
    for entry in catalog:
        if _matches(entry):
            boosted.append(entry)
        else:
            rest.append(entry)
    return boosted + rest


def load_backlog_catalog(knowledge_dir: Path) -> list[CatalogEntry]:
    """Load titles from ``knowledge/backlog/*.md``."""
    directory = backlog_dir(knowledge_dir)
    if not directory.is_dir():
        return []
    entries: list[CatalogEntry] = []
    for path in sorted(directory.glob("*.md")):
        entry = _catalog_entry_from_file(path, knowledge_dir=knowledge_dir, kind="backlog")
        if entry is not None:
            entries.append(entry)
    return entries


def load_ticket_catalog(knowledge_dir: Path) -> list[CatalogEntry]:
    """Load titles from ``knowledge/tickets/*.md``."""
    directory = tickets_dir(knowledge_dir)
    if not directory.is_dir():
        return []
    entries: list[CatalogEntry] = []
    for path in sorted(directory.glob("*.md")):
        entry = _catalog_entry_from_file(path, knowledge_dir=knowledge_dir, kind="ticket")
        if entry is not None:
            entries.append(entry)
    return entries


def load_full_catalog(knowledge_dir: Path) -> list[CatalogEntry]:
    """Backlog + tickets catalog for dedup prompts."""
    return load_backlog_catalog(knowledge_dir) + load_ticket_catalog(knowledge_dir)


def catalog_paths(catalog: list[CatalogEntry]) -> set[str]:
    """Normalized path strings present in *catalog*."""
    return {e.path for e in catalog}


def resolve_match_path(
    match_path: str | None,
    *,
    knowledge_dir: Path,
    catalog: list[CatalogEntry],
) -> Path | None:
    """Resolve a catalog-relative match path to an absolute Path if valid."""
    if not match_path:
        return None
    normalized = match_path.strip().lstrip("./")
    # Accept paths relative to repo root or knowledge/
    candidates = [
        knowledge_dir.parent / normalized,
        Path(normalized),
    ]
    if normalized.startswith("knowledge/"):
        candidates.append(knowledge_dir.parent / normalized)
    else:
        candidates.append(knowledge_dir / normalized)

    valid_paths = catalog_paths(catalog)
    # Normalize catalog comparison
    for cand in candidates:
        try:
            if cand.is_file():
                try:
                    rel = cand.relative_to(knowledge_dir.parent).as_posix()
                except ValueError:
                    rel = normalized
                if rel in valid_paths or normalized in valid_paths:
                    return cand.resolve()
                # File exists on disk even if catalog stale
                if "backlog" in cand.parts or "tickets" in cand.parts:
                    return cand.resolve()
        except OSError:
            continue

    # Fuzzy: match by filename against catalog
    name = Path(normalized).name
    for entry in catalog:
        if Path(entry.path).name == name:
            resolved = knowledge_dir.parent / entry.path
            if resolved.is_file():
                return resolved.resolve()
    return None


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _yaml_scalar(value: str) -> str:
    """Quote YAML scalar when needed."""
    if value == "" or any(c in value for c in ":#{}[]&*!|>'\"%@`\n"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _occurrence_frontmatter_lines(occ: Occurrence) -> list[str]:
    lines = [
        f"  - feedback: {_yaml_scalar(occ.feedback_rel)}",
        f"    file: {_yaml_scalar(occ.file_path)}",
    ]
    if occ.lines:
        lines.append(f"    lines: {_yaml_scalar(occ.lines)}")
    lines.append(f"    date: {occ.date}")
    return lines


def _occurrence_body_bullet(occ: Occurrence) -> str:
    preview = occ.analysis_preview.replace("\n", " ").strip()
    if len(preview) > 200:
        preview = preview[:200] + "…"
    file_bit = f" (`{occ.file_path}`)" if occ.file_path else ""
    lines_bit = f" lines {occ.lines}" if occ.lines else ""
    body = f"- **{occ.date}** — `{occ.feedback_rel}`{file_bit}{lines_bit}"
    if preview:
        body += f"\n  {preview}"
    return body


def unique_backlog_path(knowledge_dir: Path, title: str) -> Path:
    """Return a free ``knowledge/backlog/<slug>.md`` path (``-2``, ``-3``, …)."""
    directory = backlog_dir(knowledge_dir)
    base = slugify_title(title)
    candidate = directory / f"{base}.md"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = directory / f"{base}-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


def create_backlog_entry(
    knowledge_dir: Path,
    *,
    title: str,
    problem: str,
    recommendation: str,
    source_report: str,
    occurrence: Occurrence,
    source_severity: str = "n/a",
) -> Path:
    """Create a new backlog markdown file; return its path."""
    directory = backlog_dir(knowledge_dir)
    directory.mkdir(parents=True, exist_ok=True)

    path = unique_backlog_path(knowledge_dir, title)
    # If unique_backlog_path returned existing-free path but race — still ok.
    now = _iso_now()
    fm_lines = [
        "---",
        f"title: {_yaml_scalar(title)}",
        f"source_report: {_yaml_scalar(source_report)}",
        f"source_severity: {_yaml_scalar(source_severity)}",
        f"created_at: {now}",
        f"updated_at: {now}",
        "source: review-analyzer",
        "occurrences:",
        *_occurrence_frontmatter_lines(occurrence),
        "---",
        "",
        f"# {title}",
        "",
        "## Problem",
        "",
        problem.strip() or "(no problem statement)",
        "",
        "## Recommendation",
        "",
        recommendation.strip() or "(no recommendation)",
        "",
        "## Occurrences",
        "",
        _occurrence_body_bullet(occurrence),
        "",
    ]
    path.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")
    log.info("Created backlog entry %s", path)
    return path


def update_backlog_entry(
    path: Path,
    *,
    occurrence: Occurrence,
) -> None:
    """Append an occurrence to an existing backlog file; bump ``updated_at``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Cannot read backlog entry {path}: {exc}") from exc

    now = _iso_now()
    if _UPDATED_AT_RE.search(text):
        text = _UPDATED_AT_RE.sub(f"updated_at: {now}", text, count=1)
    else:
        # Insert updated_at after frontmatter open if missing.
        text = text.replace("---\n", f"---\nupdated_at: {now}\n", 1)

    # Append to frontmatter occurrences list if present (before closing ---).
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match and "occurrences:" in fm_match.group(1):
        # Insert new list items just before the closing --- of frontmatter.
        insert = "\n".join(_occurrence_frontmatter_lines(occurrence)) + "\n"
        end = fm_match.end()
        # fm_match.end() points after closing ---\n; insert before that closer.
        closer_start = text.rfind("---", 0, end)
        if closer_start > 0:
            text = text[:closer_start] + insert + text[closer_start:]

    bullet = _occurrence_body_bullet(occurrence)
    if "## Occurrences" in text:
        # Append after the section header block (end of file is fine).
        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + bullet + "\n"
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n## Occurrences\n\n" + bullet + "\n"

    path.write_text(text, encoding="utf-8")
    log.info("Updated backlog entry %s", path)


def stamp_feedback_disposition(
    feedback_path: Path,
    *,
    action: BacklogAction,
    target: str | None,
    rationale: str,
) -> None:
    """Append a ``## Backlog disposition`` section to a feedback report."""
    if action == BacklogAction.ERROR:
        action_label = "error"
    elif action == BacklogAction.LINKED_TO_TICKET:
        action_label = "linked_to_ticket"
    elif action == BacklogAction.CREATED:
        action_label = "created"
    elif action == BacklogAction.UPDATED:
        action_label = "updated"
    else:
        action_label = "skipped"

    lines = [
        "",
        "## Backlog disposition",
        "",
        f"- **Action**: {action_label}",
    ]
    if target:
        lines.append(f"- **Target**: `{target}`")
    if rationale:
        lines.append(f"- **Rationale**: {rationale}")
    lines.append("")

    try:
        existing = feedback_path.read_text(encoding="utf-8") if feedback_path.is_file() else ""
    except OSError as exc:
        log.error("Failed to read feedback for disposition stamp %s: %s", feedback_path, exc)
        return

    # Replace prior disposition if re-run
    if "## Backlog disposition" in existing:
        existing = existing.split("## Backlog disposition")[0].rstrip() + "\n"

    try:
        feedback_path.write_text(existing + "\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log.error("Failed to stamp disposition on %s: %s", feedback_path, exc)


def relative_to_repo(path: Path, knowledge_dir: Path) -> str:
    """Best-effort path relative to the repo root (parent of knowledge/)."""
    repo = knowledge_dir.parent
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def deterministic_title_from_finding(finding: dict[str, Any], analysis_text: str) -> str:
    """Build a short title when the dedup LLM fails."""
    if finding.get("type") == "comment":
        content = str(finding.get("content") or "").strip()
        path = str(finding.get("path") or "unknown")
        if content:
            first = content.splitlines()[0].strip()
            if len(first) > 80:
                first = first[:80].rstrip() + "…"
            return first
        return f"Review finding in {path}"
    message = str(finding.get("message") or "").strip()
    if message:
        first = message.splitlines()[0].strip()
        if len(first) > 80:
            first = first[:80].rstrip() + "…"
        return first
    preview = analysis_text.strip().splitlines()[0] if analysis_text.strip() else "Backlog finding"
    if len(preview) > 80:
        preview = preview[:80].rstrip() + "…"
    return preview


def build_occurrence(
    finding: dict[str, Any],
    *,
    feedback_rel: str,
    analysis_preview: str,
    date: str | None = None,
) -> Occurrence:
    """Construct an :class:`Occurrence` from an OCR finding dict."""
    if finding.get("type") == "comment":
        file_path = str(finding.get("path", "(unknown)"))
        start = finding.get("start_line")
        end = finding.get("end_line")
        lines: str | None
        if start is not None and end is not None:
            lines = f"{start}-{end}"
        else:
            lines = None
    else:
        file_path = str(finding.get("file", "(unknown)"))
        lines = None
    return Occurrence(
        feedback_rel=feedback_rel,
        file_path=file_path,
        lines=lines,
        date=date or _date_today(),
        analysis_preview=analysis_preview,
    )


def format_promotion_summary(counts: PromotionCounts) -> str:
    """Human-readable promotion block for SUMMARY.md / stdout."""
    d = counts.as_dict()
    lines = [
        "Backlog promotion:",
        f"- created: {d['created']}",
        f"- updated: {d['updated']}",
        f"- linked_to_ticket: {d['linked_to_ticket']}",
        f"- skipped: {d['skipped']}",
        f"- errors: {d['errors']}",
    ]
    return "\n".join(lines) + "\n"
