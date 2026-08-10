"""LLM-based dedup for review-analyzer BACKLOG → knowledge/ promotion."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from deep_architect.backlog_store import (
    BacklogAction,
    CatalogEntry,
    PromotionCounts,
    PromotionResult,
    backlog_dir,
    build_occurrence,
    catalog_paths,
    create_backlog_entry,
    deterministic_title_from_finding,
    load_full_catalog,
    relative_to_repo,
    resolve_match_path,
    slugify_title,
    stamp_feedback_disposition,
    update_backlog_entry,
)
from deep_architect.review_analyzer import (
    AnalysisResult,
    Verdict,
    generate_output_filename,
)

log = logging.getLogger(__name__)

_VALID_ACTIONS = frozenset({"create", "update_backlog", "link_ticket", "skip"})

# Injected at call sites for tests; defaults to opencode via review_analyzer.
DedupRunner = Callable[[str, str], str]
"""``(prompt, model) -> raw text or JSON body from LLM``."""


@dataclass(frozen=True)
class DedupDecision:
    """Parsed, validated LLM (or fallback) decision for one finding."""

    action: str  # create | update_backlog | link_ticket | skip
    match_path: str | None
    title: str
    problem: str
    recommendation: str
    rationale: str


def build_dedup_prompt(
    finding: dict[str, Any],
    analysis: AnalysisResult,
    catalog: list[CatalogEntry],
) -> str:
    """Build the structured dedup prompt for opencode."""
    if finding.get("type") == "comment":
        finding_block = (
            f"type: comment\n"
            f"file: {finding.get('path')}\n"
            f"lines: {finding.get('start_line')}-{finding.get('end_line')}\n"
            f"comment: {finding.get('content', '')}\n"
        )
        existing = finding.get("existing_code")
        if existing:
            finding_block += f"existing_code:\n{existing}\n"
    else:
        finding_block = (
            f"type: warning\n"
            f"file: {finding.get('file')}\n"
            f"message: {finding.get('message', '')}\n"
            f"warning_type: {finding.get('warning_type', finding.get('type', 'warning'))}\n"
        )

    backlog_rows = [
        {"path": e.path, "title": e.title}
        for e in catalog
        if e.kind == "backlog"
    ]
    ticket_rows = [
        {
            "path": e.path,
            "id": e.ticket_id,
            "title": e.title,
            "status": e.status,
        }
        for e in catalog
        if e.kind == "ticket"
    ]

    return (
        "You triage a code-review finding already classified BACKLOG "
        "(defer for later — not auto-fixed now).\n"
        "Decide whether it matches an existing knowledge/backlog or "
        "knowledge/tickets entry, or needs a new backlog item.\n\n"
        "FINDING:\n"
        f"{finding_block}\n"
        "ANALYZER_ANALYSIS:\n"
        f"{analysis.analysis}\n\n"
        "EXISTING_BACKLOG (JSON):\n"
        f"{json.dumps(backlog_rows, indent=2)}\n\n"
        "EXISTING_TICKETS (JSON):\n"
        f"{json.dumps(ticket_rows, indent=2)}\n\n"
        "Rules:\n"
        "- Prefer link_ticket if a ticket already tracks the same core issue.\n"
        "- Prefer update_backlog if a backlog entry is the same theme "
        "(even different wording).\n"
        "- create only when nothing is close.\n"
        "- skip only if this is infrastructure noise or not a real deferred "
        "item (rare).\n"
        "- match_path must be copied exactly from the catalogs above, or null.\n"
        "- title: short human title (not a full paragraph).\n"
        "- problem: 1-3 sentences.\n"
        "- recommendation: what to do later.\n"
        "- rationale: one sentence why this action/match.\n\n"
        "Respond with ONLY a JSON object (no markdown fences):\n"
        "{\n"
        '  "action": "create" | "update_backlog" | "link_ticket" | "skip",\n'
        '  "match_path": "knowledge/..." | null,\n'
        '  "title": "...",\n'
        '  "problem": "...",\n'
        '  "recommendation": "...",\n'
        '  "rationale": "..."\n'
        "}\n"
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object from LLM text (fences allowed)."""
    stripped = text.strip()
    if not stripped:
        return None

    # Strip markdown fences if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Scan for first {...} block
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(stripped[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = stripped[start : i + 1]
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, dict):
                    return parsed
                return None
    return None


def parse_dedup_response(
    raw_text: str,
    *,
    finding: dict[str, Any],
    analysis: AnalysisResult,
    catalog: list[CatalogEntry],
) -> DedupDecision:
    """Parse LLM text into a :class:`DedupDecision`, with safe fallbacks."""
    fallback_title = deterministic_title_from_finding(finding, analysis.analysis)
    fallback_problem = analysis.analysis.strip()[:500] or fallback_title
    fallback = DedupDecision(
        action="create",
        match_path=None,
        title=fallback_title,
        problem=fallback_problem,
        recommendation="Revisit this deferred review finding when capacity allows.",
        rationale="Fallback create after unparseable or invalid dedup response",
    )

    parsed = _extract_json_object(raw_text)
    if parsed is None:
        log.warning("Dedup LLM response not parseable; falling back to create")
        return fallback

    action = str(parsed.get("action", "create")).strip().lower()
    if action not in _VALID_ACTIONS:
        log.warning("Dedup action %r invalid; falling back to create", action)
        return fallback

    title = str(parsed.get("title") or fallback_title).strip() or fallback_title
    problem = str(parsed.get("problem") or fallback_problem).strip() or fallback_problem
    recommendation = str(
        parsed.get("recommendation")
        or "Revisit this deferred review finding when capacity allows."
    ).strip()
    rationale = str(parsed.get("rationale") or "").strip()

    match_raw = parsed.get("match_path")
    match_path: str | None
    if match_raw is None or match_raw == "" or str(match_raw).lower() == "null":
        match_path = None
    else:
        match_path = str(match_raw).strip()

    valid = catalog_paths(catalog)
    if action in {"update_backlog", "link_ticket"}:
        if not match_path:
            log.warning(
                "Dedup action %s without match_path; falling back to create",
                action,
            )
            return DedupDecision(
                action="create",
                match_path=None,
                title=title,
                problem=problem,
                recommendation=recommendation,
                rationale=rationale or "Missing match_path; created new backlog entry",
            )
        # Normalize and validate against catalog
        normalized = match_path.lstrip("./")
        if normalized not in valid and match_path not in valid:
            # Allow filename-only match against catalog
            name = match_path.rsplit("/", 1)[-1]
            matched_entry = next(
                (e for e in catalog if e.path.endswith(name)),
                None,
            )
            if matched_entry is None:
                log.warning(
                    "Dedup match_path %r not in catalog; falling back to create",
                    match_path,
                )
                return DedupDecision(
                    action="create",
                    match_path=None,
                    title=title,
                    problem=problem,
                    recommendation=recommendation,
                    rationale=rationale
                    or f"Invalid match_path {match_path!r}; created new entry",
                )
            match_path = matched_entry.path
            if matched_entry.kind == "ticket" and action == "update_backlog":
                action = "link_ticket"
            elif matched_entry.kind == "backlog" and action == "link_ticket":
                action = "update_backlog"
        else:
            # Prefer catalog path spelling
            for e in catalog:
                if e.path == normalized or e.path == match_path:
                    match_path = e.path
                    if e.kind == "ticket" and action == "update_backlog":
                        action = "link_ticket"
                    elif e.kind == "backlog" and action == "link_ticket":
                        action = "update_backlog"
                    break

    if action == "create":
        match_path = None
    if action == "skip":
        match_path = None

    return DedupDecision(
        action=action,
        match_path=match_path,
        title=title,
        problem=problem,
        recommendation=recommendation,
        rationale=rationale or f"Dedup decided {action}",
    )


def fallback_create_decision(
    finding: dict[str, Any],
    analysis: AnalysisResult,
    *,
    rationale: str,
) -> DedupDecision:
    """Deterministic create decision (LLM error path)."""
    title = deterministic_title_from_finding(finding, analysis.analysis)
    return DedupDecision(
        action="create",
        match_path=None,
        title=title,
        problem=analysis.analysis.strip()[:500] or title,
        recommendation="Revisit this deferred review finding when capacity allows.",
        rationale=rationale,
    )


def apply_dedup_decision(
    decision: DedupDecision,
    *,
    finding: dict[str, Any],
    analysis: AnalysisResult,
    knowledge_dir: Any,
    ocr_file: Any,
    output_dir: Any,
    feedback_path: Any | None = None,
) -> PromotionResult:
    """Execute create / update / link / skip against the filesystem."""
    from pathlib import Path  # noqa: PLC0415

    knowledge_dir = Path(knowledge_dir)
    ocr_file = Path(ocr_file)
    output_dir = Path(output_dir)
    if feedback_path is None:
        feedback_path = output_dir / generate_output_filename(finding)
    else:
        feedback_path = Path(feedback_path)

    feedback_rel = feedback_path.name
    try:
        feedback_rel = relative_to_repo(feedback_path, knowledge_dir)
    except Exception:  # noqa: BLE001
        feedback_rel = str(feedback_path)

    occ = build_occurrence(
        finding,
        feedback_rel=feedback_rel,
        analysis_preview=analysis.analysis,
    )
    source_report = str(ocr_file)

    if decision.action == "skip":
        result = PromotionResult(
            action=BacklogAction.SKIPPED,
            target=None,
            rationale=decision.rationale,
            title=decision.title,
        )
        stamp_feedback_disposition(
            feedback_path,
            action=result.action,
            target=result.target,
            rationale=result.rationale,
        )
        return result

    if decision.action == "link_ticket":
        catalog = load_full_catalog(knowledge_dir)
        resolved = resolve_match_path(
            decision.match_path,
            knowledge_dir=knowledge_dir,
            catalog=catalog,
        )
        if resolved is None:
            # Fall through to create
            log.warning(
                "link_ticket path unresolved (%s); creating backlog instead",
                decision.match_path,
            )
            return apply_dedup_decision(
                DedupDecision(
                    action="create",
                    match_path=None,
                    title=decision.title,
                    problem=decision.problem,
                    recommendation=decision.recommendation,
                    rationale=(
                        f"Unresolved ticket path {decision.match_path!r}; "
                        "created backlog"
                    ),
                ),
                finding=finding,
                analysis=analysis,
                knowledge_dir=knowledge_dir,
                ocr_file=ocr_file,
                output_dir=output_dir,
                feedback_path=feedback_path,
            )
        target_rel = relative_to_repo(resolved, knowledge_dir)
        result = PromotionResult(
            action=BacklogAction.LINKED_TO_TICKET,
            target=target_rel,
            rationale=decision.rationale,
            title=decision.title,
        )
        stamp_feedback_disposition(
            feedback_path,
            action=result.action,
            target=result.target,
            rationale=result.rationale,
        )
        return result

    if decision.action == "update_backlog":
        catalog = load_full_catalog(knowledge_dir)
        resolved = resolve_match_path(
            decision.match_path,
            knowledge_dir=knowledge_dir,
            catalog=catalog,
        )
        if resolved is None or not resolved.is_file():
            log.warning(
                "update_backlog path unresolved (%s); creating instead",
                decision.match_path,
            )
            return apply_dedup_decision(
                DedupDecision(
                    action="create",
                    match_path=None,
                    title=decision.title,
                    problem=decision.problem,
                    recommendation=decision.recommendation,
                    rationale=(
                        f"Unresolved backlog path {decision.match_path!r}; "
                        "created new entry"
                    ),
                ),
                finding=finding,
                analysis=analysis,
                knowledge_dir=knowledge_dir,
                ocr_file=ocr_file,
                output_dir=output_dir,
                feedback_path=feedback_path,
            )
        try:
            update_backlog_entry(resolved, occurrence=occ)
        except OSError as exc:
            return PromotionResult(
                action=BacklogAction.ERROR,
                target=None,
                rationale=decision.rationale,
                title=decision.title,
                error=str(exc),
            )
        target_rel = relative_to_repo(resolved, knowledge_dir)
        result = PromotionResult(
            action=BacklogAction.UPDATED,
            target=target_rel,
            rationale=decision.rationale,
            title=decision.title,
        )
        stamp_feedback_disposition(
            feedback_path,
            action=result.action,
            target=result.target,
            rationale=result.rationale,
        )
        return result

    # create — if the primary slug already exists, append (update) instead
    existing_slug = backlog_dir(knowledge_dir) / f"{slugify_title(decision.title)}.md"
    if existing_slug.is_file():
        try:
            update_backlog_entry(existing_slug, occurrence=occ)
        except OSError as exc:
            return PromotionResult(
                action=BacklogAction.ERROR,
                target=None,
                rationale=decision.rationale,
                title=decision.title,
                error=str(exc),
            )
        target_rel = relative_to_repo(existing_slug, knowledge_dir)
        result = PromotionResult(
            action=BacklogAction.UPDATED,
            target=target_rel,
            rationale=decision.rationale
            or "Slug already existed; appended occurrence",
            title=decision.title,
        )
        stamp_feedback_disposition(
            feedback_path,
            action=result.action,
            target=result.target,
            rationale=result.rationale,
        )
        return result

    try:
        created = create_backlog_entry(
            knowledge_dir,
            title=decision.title,
            problem=decision.problem,
            recommendation=decision.recommendation,
            source_report=source_report,
            occurrence=occ,
        )
    except OSError as exc:
        return PromotionResult(
            action=BacklogAction.ERROR,
            target=None,
            rationale=decision.rationale,
            title=decision.title,
            error=str(exc),
        )
    target_rel = relative_to_repo(created, knowledge_dir)
    result = PromotionResult(
        action=BacklogAction.CREATED,
        target=target_rel,
        rationale=decision.rationale,
        title=decision.title,
    )
    stamp_feedback_disposition(
        feedback_path,
        action=result.action,
        target=result.target,
        rationale=result.rationale,
    )
    return result


def run_dedup_llm(
    prompt: str,
    model: str,
    *,
    timeout: int,
    runner: DedupRunner | None = None,
) -> str:
    """Call opencode (or *runner*) and return best-effort text content."""
    if runner is not None:
        return runner(prompt, model)

    from deep_architect.review_analyzer import (  # noqa: PLC0415
        _run_opencode_once,
    )

    result = _run_opencode_once(prompt, model, timeout=timeout)
    if result.verdict == Verdict.TIMEOUT:
        raise TimeoutError(result.analysis)
    # Prefer raw_response for full NDJSON; analysis has extracted text body
    if result.analysis and not result.analysis.startswith("opencode"):
        return result.analysis
    return result.raw_response or result.analysis


def decision_from_triage_match(
    finding: dict[str, Any],
    analysis: AnalysisResult,
    *,
    knowledge_dir: Any,
    catalog: list[CatalogEntry],
) -> DedupDecision | None:
    """Build a promotion decision from triage ``match_path`` without an LLM call.

    Returns a decision when *match_path* resolves to a backlog (update) or
    ticket (link). Returns ``None`` when *match_path* is absent, or set but
    unresolvable (caller should fall through to LLM dedup).

    Update path only appends an occurrence — it does not rewrite Problem /
    Recommendation on the existing entry.
    """
    from pathlib import Path  # noqa: PLC0415

    if not analysis.match_path:
        return None

    knowledge_dir = Path(knowledge_dir)
    resolved = resolve_match_path(
        analysis.match_path,
        knowledge_dir=knowledge_dir,
        catalog=catalog,
    )
    if resolved is None:
        log.warning(
            "Triage match_path %r unresolvable; falling through to LLM dedup",
            analysis.match_path,
        )
        return None

    # Prefer catalog entry identity for path spelling + kind.
    entry: CatalogEntry | None = None
    try:
        rel = relative_to_repo(resolved, knowledge_dir)
    except Exception:  # noqa: BLE001
        rel = analysis.match_path.strip().lstrip("./")

    candidates = {
        analysis.match_path.strip().lstrip("./"),
        rel,
        f"knowledge/backlog/{resolved.name}",
        f"knowledge/tickets/{resolved.name}",
    }
    for e in catalog:
        if e.path in candidates or Path(e.path).name == resolved.name:
            entry = e
            break

    if entry is not None:
        kind = entry.kind
        match_path = entry.path
    elif "tickets" in resolved.parts:
        kind = "ticket"
        match_path = (
            rel
            if rel.startswith("knowledge/")
            else f"knowledge/tickets/{resolved.name}"
        )
    elif "backlog" in resolved.parts:
        kind = "backlog"
        match_path = (
            rel
            if rel.startswith("knowledge/")
            else f"knowledge/backlog/{resolved.name}"
        )
    else:
        log.warning(
            "Triage match_path %r resolved to non-catalog path %s; "
            "falling through to LLM dedup",
            analysis.match_path,
            resolved,
        )
        return None

    title = deterministic_title_from_finding(finding, analysis.analysis)
    problem = analysis.analysis.strip()[:500] or title
    if kind == "ticket":
        return DedupDecision(
            action="link_ticket",
            match_path=match_path,
            title=title,
            problem=problem,
            recommendation="Tracked by existing ticket.",
            rationale=(
                f"Triage match_path short-circuit → ticket {match_path}"
            ),
        )
    if kind == "backlog":
        return DedupDecision(
            action="update_backlog",
            match_path=match_path,
            title=title,
            problem=problem,
            recommendation="See existing backlog entry.",
            rationale=(
                f"Triage match_path short-circuit → backlog {match_path}"
            ),
        )
    log.warning(
        "Triage match_path %r has unknown kind %r; falling through to LLM",
        match_path,
        kind,
    )
    return None


def promote_backlog_findings(
    results: list[tuple[dict[str, Any], AnalysisResult]],
    *,
    knowledge_dir: Any,
    ocr_file: Any,
    output_dir: Any,
    model: str,
    timeout: int,
    runner: DedupRunner | None = None,
) -> PromotionCounts:
    """Sequentially promote BACKLOG findings into ``knowledge/backlog/``.

    *runner*, when provided, replaces the live opencode call (for tests).
    Only :attr:`Verdict.BACKLOG` is promoted — never ``TIMEOUT``,
    ``DUPLICATE``, ``VALID``, or ``REJECTED``.

    When triage already set :attr:`AnalysisResult.match_path` and it resolves
    to a catalog entry, promotion skips the LLM and updates/links directly.
    """
    from pathlib import Path  # noqa: PLC0415

    knowledge_dir = Path(knowledge_dir)
    ocr_file = Path(ocr_file)
    output_dir = Path(output_dir)

    counts = PromotionCounts()
    backlog_items = [
        (f, a) for f, a in results if a.verdict == Verdict.BACKLOG
    ]
    if not backlog_items:
        log.info("No BACKLOG findings to promote")
        return counts

    log.info(
        "Promoting %d BACKLOG finding(s) into %s/backlog/",
        len(backlog_items),
        knowledge_dir,
    )

    for finding, analysis in backlog_items:
        feedback_path = output_dir / generate_output_filename(finding)
        try:
            catalog = load_full_catalog(knowledge_dir)
            decision = decision_from_triage_match(
                finding,
                analysis,
                knowledge_dir=knowledge_dir,
                catalog=catalog,
            )
            if decision is not None:
                log.info(
                    "Promotion short-circuit for %s via match_path=%s → %s",
                    generate_output_filename(finding),
                    analysis.match_path,
                    decision.action,
                )
            else:
                prompt = build_dedup_prompt(finding, analysis, catalog)
                try:
                    raw = run_dedup_llm(
                        prompt, model, timeout=timeout, runner=runner
                    )
                    decision = parse_dedup_response(
                        raw,
                        finding=finding,
                        analysis=analysis,
                        catalog=catalog,
                    )
                except TimeoutError as exc:
                    log.warning(
                        "Dedup LLM timed out for %s: %s; fallback create",
                        generate_output_filename(finding),
                        exc,
                    )
                    decision = fallback_create_decision(
                        finding,
                        analysis,
                        rationale=f"Dedup timed out; fallback create ({exc})",
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "Dedup LLM failed for %s: %s; fallback create",
                        generate_output_filename(finding),
                        exc,
                    )
                    decision = fallback_create_decision(
                        finding,
                        analysis,
                        rationale=f"Dedup error; fallback create ({exc})",
                    )

            result = apply_dedup_decision(
                decision,
                finding=finding,
                analysis=analysis,
                knowledge_dir=knowledge_dir,
                ocr_file=ocr_file,
                output_dir=output_dir,
                feedback_path=feedback_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "Backlog promotion failed for %s",
                generate_output_filename(finding),
            )
            result = PromotionResult(
                action=BacklogAction.ERROR,
                error=str(exc),
                rationale=str(exc),
            )
            stamp_feedback_disposition(
                feedback_path,
                action=result.action,
                target=None,
                rationale=result.rationale,
            )

        counts.record(result)
        log.info(
            "Promotion %s → %s (%s)",
            generate_output_filename(finding),
            result.action.value,
            result.target or result.error or "",
        )

    return counts
