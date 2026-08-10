# PROJ-0016: Catalog-Aware Triage, Prior Feedback, and Intra-OCR Dedup — Implementation Plan

## Overview

Make `review-analyzer` classification-time aware of durable knowledge (`knowledge/backlog/`, `knowledge/tickets/`) and prior feedback passes, and collapse near-duplicate OCR comments within a single run. The goal is multi-pass convergence: deferred themes stop reappearing as `VALID` (and thrashing `review-action`), while the **same concrete defect in two different files still gets fixed in both**.

## Current State Analysis

### Pipeline today

```
ocr review → review-analyzer (isolated triage) → [optional promote BACKLOG]
           → review-action (VALID only)
```

Triage (`construct_analysis_prompt` / `analyze_finding` in `deep_architect/review_analyzer.py`) is **per-finding isolated**: the LLM sees path, snippet, comment — never catalog or prior feedback. Catalog awareness exists only **after** triage in `backlog_dedup.promote_backlog_findings`, and only for findings already marked `BACKLOG`.

### What already exists (reuse)

| Piece | Location | Notes |
|-------|----------|--------|
| Compact catalog (titles only) | `backlog_store.CatalogEntry`, `load_full_catalog()` | Explicitly no full bodies |
| Occurrence `file` trail | `Occurrence.file_path`, frontmatter `occurrences:` | Written on create/update; **not indexed** by catalog load |
| Post-hoc LLM dedup | `backlog_dedup.build_dedup_prompt`, `promote_backlog_findings` | Second LLM pass; titles only in catalog JSON |
| Path filter on findings | `filter_findings_by_path` + `--include`/`--exclude` | Finding-side globs, not catalog filter |
| TIMEOUT hygiene | `Verdict.TIMEOUT`, `is_timeout_report`, promotion skips TIMEOUT | Keep; extend prior-feedback filter |
| Feedback markdown | `generate_markdown_content`, `feedback_report.get_verdict` | Verdict parse list must grow for `DUPLICATE` |

### Gaps that caused plant-tracking noise

1. Classification never sees backlog/tickets → same themes reclassified `VALID`.
2. Promotion only runs for already-`BACKLOG` findings → cannot prevent auto-fix thrash.
3. No prior-pass memory into triage.
4. Intra-OCR near-duplicates each pay full LLM cost.
5. Catalog has no occurrence-file index for ranking (optional boost later).

### Key design decisions (locked in planning)

1. **No hard file filter on catalog.** Heads are global and cheap. File affinity may rank/boost candidates later; never exclude cross-file themes.
2. **Heads only by default** (title + kind + ticket meta + occurrence file list). Full body expanded only for top **M ≤ 3** harness-selected candidates — still one-shot `opencode`, no agentic Read loop.
3. **Match suggests deferral; does not hard-force non-VALID.** Same concrete bug in two files → both `VALID` → both fixed. Match → prefer `BACKLOG` only for deferred campaigns / parked themes.
4. **Intra-OCR dedup is same-path only.** Never collapse across files.
5. **New verdict `DUPLICATE` for intra-OCR collapse.** Overload `BACKLOG` for catalog-matched deferred work (no `KNOWN` in MVP).
6. **Promotion reuses triage `match_path`** when present to avoid a second LLM call for matched items.

## Desired End State

```
ocr review
  → path filter (--include/--exclude)
  → intra-OCR same-path dedup (DUPLICATE for non-canonical)
  → review-analyzer triage with catalog heads + optional prior-feedback index
       (optional full body for ≤3 candidates)
  → promote BACKLOG (skip LLM when match_path already set; TIMEOUT never promoted)
  → review-action (VALID only)
```

### Verification sketch

- Fixture catalog with a deferred theme → finding about that theme is prompted with catalog heads and may be classified `BACKLOG` with `match_path` (unit tests on prompt + parse; no live LLM).
- Two findings, same comment text, **different files** → both remain canonical (no `DUPLICATE`); both can be `VALID`.
- Two near-identical findings, **same file** → one full triage, one `DUPLICATE`.
- Empty knowledge / no `--prior-feedback` → behavior matches today.
- Plant-tracking dogfood: after seeding backlog from a prior pass, re-run analyzer; type-hint / Form-Body campaigns prefer `BACKLOG`, while distinct concrete bugs still land `VALID` per file.

### Key Discoveries

- `CatalogEntry` docstring already states “no full file bodies” (`backlog_store.py` ~35–44).
- Occurrence files exist on disk but `_catalog_entry_from_file` only extracts title/id/status (~156–194).
- Triage is pure prompt string via `opencode run` — no tools (`review_analyzer.py` ~375–411, ~480–520).
- `feedback_report.get_verdict` only recognizes `VALID|REJECTED|BACKLOG|TIMEOUT` (~163–166) — must add `DUPLICATE`.
- `review-action` only processes `VALID`; new verdicts are safe if they never look like `VALID`.

## What We're NOT Doing

- Hard-filtering catalog entries to “only items whose occurrence file equals this finding’s path.”
- New verdict `KNOWN` / `ALREADY_BACKLOG` (nice-to-have later).
- Agentic tool-using triage (Read/Grep loop).
- Outer multi-pass orchestrator (PROJ-0019).
- `review-action` severity gates / catalog veto (PROJ-0017).
- Cross-pass report CLI (PROJ-0018).
- Embedding-based similarity (heuristic Jaccard/token overlap only).
- Auto-creating tickets (link-only when matching tickets).
- One-shot backfill CLI from plant-tracking feedback dirs (nice-to-have; document manual ops if useful).
- Changing the OCR product itself.

## Implementation Approach

Surgical extensions on existing modules:

1. **Catalog index enrichment** in `backlog_store` (occurrence files on entries; helpers to format heads and load full body for a path).
2. **Pure functions** for intra-OCR grouping and prior-feedback index load (easy unit tests, no LLM).
3. **Prompt + parse** changes in `review_analyzer` for catalog-aware triage and optional `match_path`.
4. **Pipeline ordering** in `main` / `process_findings_concurrently`: dedup → analyze only canonicals → write DUPLICATE stubs.
5. **Promotion shortcut** in `backlog_dedup` when triage already provided `match_path`.
6. **Docs + verdict plumbing** in README, `feedback_report`, SUMMARY/INDEX.

Prefer small pure modules or clearly named functions over new frameworks. Keep thresholds as named module constants (or config keys only if a natural home already exists); document them.

---

## Phase 1: Catalog heads + occurrence index

### Overview

Enrich the compact catalog so triage (and later promotion) can show title + related source files without full bodies. Add helpers to format heads for prompts and to load a full backlog/ticket body when expanding candidates.

### Changes Required

#### 1. Extend `CatalogEntry`

**File**: `deep_architect/backlog_store.py`

**Changes**:
- Add `occurrence_files: tuple[str, ...] = ()` — unique source paths from frontmatter `occurrences:` list (`file:` fields).
- Parse nested occurrence files in `_catalog_entry_from_file` (or a small helper). Tickets typically have empty `occurrence_files`.
- Keep load resilient: missing/malformed occurrences → empty tuple, not failure.

```python
@dataclass(frozen=True)
class CatalogEntry:
    """Compact index row for prompts (no full file bodies)."""

    path: str
    title: str
    kind: str  # "backlog" | "ticket"
    ticket_id: str | None = None
    status: str | None = None
    occurrence_files: tuple[str, ...] = ()
```

#### 2. Head formatting + optional full-body load

**File**: `deep_architect/backlog_store.py` (or small `deep_architect/catalog_index.py` if `backlog_store` grows too large — prefer stay in store unless file exceeds comfort)

**Changes**:
- `format_catalog_heads(catalog: list[CatalogEntry]) -> str` — JSON or bullet list with `path`, `title`, `kind`, ticket fields, `files` (occurrence list). Suitable for injection into prompts.
- `load_entry_body(knowledge_dir: Path, entry_path: str) -> str | None` — read full markdown for expand path; log + return None on OSError.
- `rank_catalog_for_finding(catalog, finding_path: str) -> list[CatalogEntry]` — stable sort: entries whose `occurrence_files` intersect finding path first, then remaining global titles. **Does not drop any entry.**

#### 3. Tests

**File**: `tests/test_backlog_store.py`

- Load catalog from fixture backlog with multi-file occurrences → `occurrence_files` populated.
- Rank: same-file entry appears before unrelated; both still present.
- Empty backlog/tickets dirs → `[]`.
- `format_catalog_heads` contains titles not full Problem sections.

### Success Criteria

#### Automated Verification
- [x] Unit tests for occurrence parse, rank (boost not drop), format heads: `uv run python -m pytest tests/test_backlog_store.py -v`
- [x] Existing backlog store/dedup tests still pass: `uv run python -m pytest tests/test_backlog_dedup.py tests/test_backlog_store.py -v`
- [x] `uv run ruff check deep_architect/ tests/`
- [x] `uv run mypy deep_architect/`

#### Manual Verification
- [ ] (Optional) Point at a real plant-tracking `knowledge/` and confirm heads print reasonably via a tiny debug snippet or temporary CLI log — no product CLI required yet.

**Implementation Note**: After automated verification passes, pause for human confirmation before Phase 2 if desired; Phase 2 can proceed without manual dogfood.

---

## Phase 2: Intra-OCR near-duplicate collapse

### Overview

Before triage, group findings that share the **same source path** and sufficiently similar content. Keep one canonical per group; mark others `DUPLICATE` without full LLM triage.

### Changes Required

#### 1. Similarity + grouping pure functions

**File**: Prefer `deep_architect/review_analyzer.py` for cohesion, or `deep_architect/finding_dedup.py` if keeping analyzer smaller.

**API sketch**:

```python
@dataclass(frozen=True)
class DuplicateGroup:
    canonical_index: int          # index into original findings list
    duplicate_indices: tuple[int, ...]

def finding_similarity_text(finding: dict[str, Any]) -> str:
    """Normalize content/message for comparison."""

def content_similarity(a: str, b: str) -> float:
    """Token Jaccard (or similar); pure, unit-testable."""

def group_near_duplicate_findings(
    findings: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_DEDUP_SIMILARITY,  # named constant e.g. 0.85
) -> list[DuplicateGroup]:
    """Same-path only. Different paths never group together."""
```

**Rules**:
- Path key: `finding.get("path") or finding.get("file")`.
- Comments: compare normalized `content` (optionally include line-range looseness — **same path is enough**; do not require identical lines).
- Warnings: compare normalized `message`.
- Canonical selection: highest severity if present on finding, else lowest index.
- `threshold` as module-level named constant; document in README. Do not invent config.toml keys unless already planned elsewhere.

#### 2. `Verdict.DUPLICATE`

**File**: `deep_architect/review_analyzer.py`

```python
class Verdict(StrEnum):
    VALID = "valid"
    REJECTED = "rejected"
    BACKLOG = "backlog"
    TIMEOUT = "timeout"
    DUPLICATE = "duplicate"
```

#### 3. Pipeline integration

**File**: `deep_architect/review_analyzer.py` — `main` / concurrent processor

Flow:
1. Extract + path-filter (+ retry-timeouts subset) as today.
2. `group_near_duplicate_findings`.
3. Analyze **only** canonical findings via existing concurrent pool.
4. For each duplicate: write markdown with `Verdict: DUPLICATE`, analysis text referencing canonical finding id/filename, `duration_s=0`, no opencode call.
5. Include duplicates in counts, INDEX, SUMMARY.

#### 4. Markdown + consumers

**Files**:
- `generate_markdown_content` — support DUPLICATE (+ optional `duplicate_of:` line).
- `feedback_report.get_verdict` / `VERDICT_ORDER` — recognize `DUPLICATE`.
- SUMMARY: list DUPLICATE in breakdown; keep TIMEOUT separate from BACKLOG (already true).

#### 5. Tests

**File**: `tests/test_review_analyzer.py` (or `tests/test_finding_dedup.py`)

- Same path + near-identical content → one group, one duplicate.
- Same content, **different paths** → two groups (no collapse).
- Empty / single finding → no duplicates.
- Threshold boundary cases.
- Markdown emits `**Verdict**: DUPLICATE`.
- `feedback_report` parses DUPLICATE.

### Success Criteria

#### Automated Verification
- [x] Grouping unit tests cover same-path collapse and cross-file non-collapse
- [x] Pipeline design: only canonicals invoke analysis path (mock opencode; assert call count)
- [x] `uv run python -m pytest tests/test_review_analyzer.py tests/test_feedback_report.py -v`
- [x] ruff / mypy clean on touched packages

#### Manual Verification
- [ ] On a multi-comment OCR JSON with two similar conftest findings: INDEX shows one analyzed + one DUPLICATE

**Implementation Note**: Pause for manual confirmation if dogfooding OCR JSON; automated mock tests are sufficient to proceed to Phase 3.

---

## Phase 3: Catalog-aware triage prompts

### Overview

Inject compact catalog heads into every triage prompt. Rank by file affinity (boost only). Optionally expand full bodies for top M ≤ 3 candidates. Parse optional `match_path` from the LLM. Enforce product rules in prompt text: match suggests deferral; concrete multi-file bugs stay VALID.

### Changes Required

#### 1. Extend `AnalysisResult`

**File**: `deep_architect/review_analyzer.py`

```python
@dataclass
class AnalysisResult:
    verdict: Verdict
    analysis: str
    raw_response: str
    retry_count: int = 0
    duration_s: float = 0.0
    match_path: str | None = None  # catalog path when deferred theme matched
```

#### 2. Candidate selection for body expand

**File**: `deep_architect/review_analyzer.py` or catalog helpers

```python
DEFAULT_CATALOG_BODY_EXPAND_MAX = 3

def select_catalog_bodies_to_expand(
    catalog: list[CatalogEntry],
    finding: dict[str, Any],
    *,
    max_bodies: int = DEFAULT_CATALOG_BODY_EXPAND_MAX,
) -> list[CatalogEntry]:
    """Heuristic: rank_catalog_for_finding, then keep entries with
    title-token overlap against finding content/message above a named
    threshold, capped at max_bodies. May return []."""
```

Load bodies via `load_entry_body` only for selected entries.

#### 3. Prompt construction

**File**: `deep_architect/review_analyzer.py` — replace/extend `construct_analysis_prompt`

Signature becomes roughly:

```python
def construct_analysis_prompt(
    finding: dict[str, Any],
    *,
    catalog_heads: str | None = None,
    catalog_bodies: list[tuple[str, str]] | None = None,  # (path, body)
    prior_feedback_index: str | None = None,
) -> str:
```

When `catalog_heads` is None or empty, omit catalog section (today’s behavior).

**Prompt rules to include (normative text for the model)**:

1. If the finding is a **concrete, auto-fixable defect**, prefer `VALID` even if a similar catalog entry exists — **especially when the same class of bug may appear in multiple files; each file still needs a fix.**
2. If the finding is the same **deferred theme / campaign** as a backlog or ticket (style campaigns, large refactors, intentional “do later”), prefer `BACKLOG` and set `match_path` to the exact catalog path.
3. Prefer `REJECTED` for false positives / noise.
4. Catalog match is **not** permission to skip a second file’s real defect.
5. Respond JSON:

```json
{"verdict": "VALID|REJECTED|BACKLOG", "analysis": "...", "match_path": "knowledge/..." | null}
```

Do not ask the model for `DUPLICATE` or `TIMEOUT` (infrastructure / pre-filter only).

#### 4. Parse path

**File**: `_parse_opencode_json` / analysis result builders

- Extract optional `match_path`; validate against catalog path set when catalog provided; invalid → `None` + log warning (do not fail the finding).
- Unknown verdict strings → existing `BACKLOG` fallback.

#### 5. Wire catalog load once per run

**File**: `review_analyzer.main` / pipeline

- Resolve `knowledge_dir` early (same as promotion).
- `catalog = load_full_catalog(knowledge_dir)` once (missing dir → `[]`, no hard failure).
- Pass catalog into analysis workers (immutable list).
- Per finding: `rank` → `format heads` (or format once globally + pass full heads — prefer **one global heads block** for simplicity unless token size becomes an issue; ranking used only for body expand selection).

**MVP simplicity**: inject **full compact heads** every time (catalogs are small). Use rank only for body expand. Revisit truncation only if dogfood shows token pain.

#### 6. Markdown stamp for match

When `match_path` set, append to finding report e.g.:

```markdown
**Catalog match**: `knowledge/backlog/foo.md`
```

(so humans and promotion can read it back if needed).

#### 7. Tests

- `construct_analysis_prompt` with empty catalog ≡ baseline shape (no catalog section).
- With fixture heads → section present; rules about multi-file VALID present in prompt text.
- Parse: valid `match_path`, invalid path dropped, missing field OK.
- Empty knowledge dir: no exception; analysis still runs (mock).

### Success Criteria

#### Automated Verification
- [x] Prompt contract tests (catalog empty vs non-empty; multi-file rule text present)
- [x] Parse tests for `match_path` validation
- [x] `uv run python -m pytest tests/test_review_analyzer.py -v`
- [x] ruff / mypy / bandit clean

#### Manual Verification
- [ ] Dry-run on plant-tracking with seeded backlog: logs/prompts show heads; at least one deferred theme trends BACKLOG in a real run (optional this phase)

**Implementation Note**: Real LLM dogfood can wait until Phase 5; unit contracts must pass here.

---

## Phase 4: Prior-feedback memory

### Overview

Add `--prior-feedback` (repeatable and/or comma-separated dirs). Build a compact read-only index of already-triaged findings and inject into the triage prompt so prior BACKLOG/REJECTED themes do not re-open as VALID without reason.

### Changes Required

#### 1. CLI

**File**: `review_analyzer.parse_args`

```text
--prior-feedback PATH   # repeatable; also accept comma-separated list in one flag
```

Document: paths are **read-only**; no mutation of old feedback files.

#### 2. Index loader

**File**: `deep_architect/review_analyzer.py` or `deep_architect/prior_feedback.py`

```python
@dataclass(frozen=True)
class PriorFeedbackItem:
    source_dir: str
    feedback_file: str
    file_path: str
    comment_preview: str  # truncated
    verdict: str
    disposition: str | None  # from ## Backlog disposition if present
    is_timeout_noise: bool

def load_prior_feedback_index(dirs: list[Path]) -> list[PriorFeedbackItem]:
    """Scan *.md reports; skip unreadable files with log.warning."""

def format_prior_feedback_index(items: list[PriorFeedbackItem], *, max_items: int = 200) -> str:
    """Compact table/bullets for prompt injection."""
```

**Filter rules**:
- Use `is_timeout_report` / TIMEOUT verdict → set `is_timeout_noise=True` and **exclude from theme memory** (or include with explicit `TIMEOUT_NOISE` label and instruct model to ignore as theme signal). Prefer **exclude** for MVP clarity.
- Legacy timed-out BACKLOG text: reuse `is_timeout_report` logic already in analyzer.

**Prompt guidance**:
- Prior `BACKLOG` / deferred disposition → prefer BACKLOG for same deferred theme.
- Prior `REJECTED` → prefer REJECTED for same noise.
- Prior `VALID` → re-evaluate normally (code may have changed); still allow VALID.
- Prior entry on file A does not force BACKLOG on a concrete bug in file B.

#### 3. Wire into `construct_analysis_prompt`

Pass `prior_feedback_index` string from Phase 3 signature.

#### 4. Tests

- Load fixture feedback dir with mixed verdicts; TIMEOUT excluded.
- Format is compact (previews truncated).
- Missing dir → warning + empty (or skip), no crash.
- Prompt includes prior section only when non-empty.

### Success Criteria

#### Automated Verification
- [x] Loader + filter unit tests
- [x] CLI accepts repeatable `--prior-feedback`
- [x] pytest green; ruff / mypy clean

#### Manual Verification
- [ ] `review-analyzer ... --prior-feedback feedback-r1 --prior-feedback feedback-r2` on plant-tracking path (full dogfood in Phase 5)

---

## Phase 5: Promotion shortcut + pipeline polish + docs

### Overview

Reuse triage `match_path` during promotion to avoid double LLM cost; ensure TIMEOUT never promotes; SUMMARY separates timeout/infra vs true backlog vs duplicates; document multi-pass usage.

### Changes Required

#### 1. Promotion short-circuit

**File**: `deep_architect/backlog_dedup.py` — `promote_backlog_findings` / `apply_dedup_decision`

For each `Verdict.BACKLOG` result:
- If `analysis.match_path` resolves via `resolve_match_path` to a **backlog** entry → `update_backlog` without LLM (title/problem/recommendation from analysis text preview or existing entry preserved — **append occurrence only**, do not rewrite Problem if updating without LLM).
- If resolves to a **ticket** → `link_ticket` without LLM.
- If `match_path` set but unresolvable → log warning; fall through to existing LLM dedup.
- If no `match_path` → existing LLM dedup path.

**Still only promote `BACKLOG`**, never `TIMEOUT`, `DUPLICATE`, `VALID`, `REJECTED`.

#### 2. SUMMARY / INDEX polish

**File**: `generate_summary_report`, INDEX generation

- Breakdown includes DUPLICATE.
- Optional clarity line: “TIMEOUT is infrastructure; not deferred product work.”
- Promotion section unchanged structurally.

#### 3. README

**File**: `README.md`

New subsection under review-analyzer:

- Multi-pass memory: `--knowledge-dir`, catalog heads at triage, `--prior-feedback`
- Intra-OCR dedup + `DUPLICATE` verdict
- Product rule: same defect in two files → both may be VALID
- Example:

```bash
review-analyzer code-review-r3.json \
  --output-dir feedback-r3 \
  --prior-feedback feedback-r1 \
  --prior-feedback feedback-r2 \
  --knowledge-dir ./knowledge
```

- Note: run from application repo root so default knowledge dir is correct.
- Note: promotion still default-on; `--no-write-backlog` unchanged.

#### 4. Integration-style unit test (mocked LLM)

- Finding with pre-set `AnalysisResult(match_path=..., verdict=BACKLOG)` → promote updates existing backlog without calling dedup runner.
- TIMEOUT / DUPLICATE not in promotion set.

#### 5. Quality bar

```bash
uv run ruff check deep_architect/ tests/
uv run mypy deep_architect/
uv run python -m pytest tests/ -v
uv run bandit -r deep_architect/ -ll
```

### Success Criteria

#### Automated Verification
- [x] Promotion short-circuit tests pass
- [x] TIMEOUT excluded from promotion (existing + regression)
- [x] Full suite green; ruff / mypy / bandit clean
- [x] README documents flags and multi-pass usage

#### Manual Verification
- [ ] Plant-tracking dogfood:
  1. Ensure `knowledge/backlog/` has at least one deferred theme from a prior pass (promote from r1/r2 if empty).
  2. Run analyzer on later OCR JSON with `--prior-feedback` pointing at earlier dirs.
  3. Recurring type-hint / Form-Body style themes prefer BACKLOG (or at least not mass VALID).
  4. Distinct concrete issues on two files can still both be VALID.
  5. SUMMARY shows TIMEOUT vs BACKLOG vs DUPLICATE distinctly.
  6. Backlog occurrences gain new file paths when themes reappear.

**Implementation Note**: This phase is the dogfood gate before considering the ticket done.

---

## Testing Strategy

### Unit Tests

| Area | Cases |
|------|--------|
| Catalog occurrence parse | multi-file, missing, tickets empty |
| Rank | boost same-file; never drop globals |
| Grouping | same-path merge; cross-file no merge; threshold edges |
| Prompt construction | empty catalog/prior; non-empty sections; multi-file VALID rule present |
| Parse | verdict + match_path validation |
| Prior index | TIMEOUT filter; disposition parse; missing dir |
| Promotion | short-circuit update/link; fallback LLM when no match |
| Feedback report | DUPLICATE recognized |

### Integration / pipeline tests (mocked opencode)

- Full `process_findings_concurrently` (or main helpers) with 3 findings: 2 same-path dupes + 1 other → opencode called twice (or once if only one canonical group + one other).
- Empty knowledge + no prior → same verdict plumbing as before (mock returns VALID).

### Manual Testing Steps

1. Seed or promote backlog on plant-tracking from an earlier feedback dir.
2. Run analyzer on r3 OCR with prior-feedback r1+r2.
3. Inspect INDEX: DUPLICATE rows, BACKLOG with catalog match lines, VALID on real multi-file defects.
4. Confirm `knowledge/backlog/*` occurrence lists updated, not thrashing new near-duplicate files for the same theme.
5. Run `review-action` only on VALID — deferred themes not auto-fixed.

## Performance Considerations

- Catalog heads: O(number of backlog+ticket files); load once per run.
- Full body expand: ≤3 reads per finding worst case; keep M small.
- Intra-OCR dedup: O(n²) within same-path buckets only — fine for OCR sizes seen (tens of findings).
- Promotion LLM calls reduced when `match_path` present.
- No embedding model / no extra network beyond existing opencode usage.

## Migration Notes

- Existing feedback dirs remain valid; new fields (`match_path`, DUPLICATE) are additive.
- `feedback_report` and any TUI verdict filters should treat unknown verdicts safely; add DUPLICATE to known lists.
- Empty `knowledge/` → no behavior change (catalog empty).
- Old reports without disposition stamps still load for prior-feedback (verdict + comment only).

## Implementation Order Summary

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| 1 | Catalog heads + occurrence index | — |
| 2 | Intra-OCR dedup + `DUPLICATE` | — (parallelizable with 1) |
| 3 | Catalog-aware triage prompt/parse | 1 |
| 4 | `--prior-feedback` index | 3 (shares prompt API) |
| 5 | Promotion short-circuit, README, dogfood | 2, 3, 4 |

Phases 1 and 2 can be implemented in parallel. Phase 4 can start as soon as Phase 3’s prompt signature exists.

## References

- Original ticket: `knowledge/tickets/PROJ-0016.md`
- Sibling tickets: `knowledge/tickets/PROJ-0017.md`, `PROJ-0018.md`, `PROJ-0019.md`
- Prior analyzer plan: `knowledge/plans/2026-06-15-PROJ-0011-review-analyzer-tool.md`
- Code: `deep_architect/review_analyzer.py`, `backlog_store.py`, `backlog_dedup.py`, `feedback_report.py`
- Tests: `tests/test_review_analyzer.py`, `test_backlog_store.py`, `test_backlog_dedup.py`, `test_feedback_report.py`
- Plant-tracking artifacts (manual dogfood):  
  `/home/gerald/repos/plant-tracking/feedback-proj-0013/` (and `-r2`, `-r3`)
