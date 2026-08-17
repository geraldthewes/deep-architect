# Review Driver Implementation Plan

## Overview

Add `review-driver`, a PR-first orchestrator that runs `ocr review` → `review-analyzer` → `review-action` for up to `--max-passes` iterations and stops when the count of high/medium `VALID` findings is 0 for K consecutive passes. All artifacts land under a standard repo directory (default `.review-runs/`). The same command is unattended locally and in CI.

## Current State Analysis

There is no orchestrator today. The review pipeline is three independent steps:

```bash
ocr review --from main --to BRANCH --format json > code-review-rN.json
review-analyzer code-review-rN.json --output-dir feedback-rN --prior-feedback …
review-action feedback-rN/
```

Operators guess when to stop. Raw OCR comment count is a bad signal (plant-tracking PROJ-0013: 29 → 31 → 36 while high severity edged down and low rose).

### What already exists (reuse)

| Piece | Location | Notes |
|-------|----------|--------|
| Analyzer CLI, in-process `main(argv) -> int` | `deep_architect/review_analyzer.py:2061–2167` | `--prior-feedback`, `--knowledge-dir`, `--exclude`, `--no-tui`, `--output-dir` |
| Catalog + prior-feedback memory (PROJ-0016) | `review_analyzer.py` (`load_prior_feedback_index`, `group_near_duplicate_findings`) | Implemented in code even though the ticket is still `backlog` |
| Action CLI, in-process `main(argv) -> int` | `deep_architect/review_action_harness.py:1100–1362` | `--no-tui`, `--provider`, `--model`, `--dry-run`; **no** `--min-severity` yet |
| Feedback dir parser (verdict + severity) | `deep_architect/feedback_report.py:178–316` | `load_feedback_dir`, `get_verdict`, `FeedbackFinding.severity` |
| Consecutive-K exit pattern | `exit_criteria.py`, `harness.py:876–892` | Score-based; copy the *shape*, do not import CriticResult |
| Atomic progress JSON | `io/files.py:166–177`, `models/progress.py` | `.tmp` + `os.replace`; harness-specific model — do not reuse `HarnessProgress` |
| Argparse review-CLI shape | `review_analyzer.py`, `review_action_harness.py` | `parse_args(argv)` + `main(argv) -> int` |
| Console scripts | `pyproject.toml:22–26` | Four scripts; no `review-driver` |
| OCR CLI (external) | system `ocr review` | `--from` = base ref, `--to` = PR ref, `--format json`, `--audience agent` |

### Gaps

- No `review-driver` / `review-compare` / novelty module.
- PROJ-0017 (action severity gate, catalog veto, exclude globs) is **not** in tree. `--min-severity` does not exist on `review-action`.
- PROJ-0018 (`review-compare`) is **not** in tree.
- Neither review CLI refuses a dirty working tree. `validate_git_repo` (`git_ops.py:61–70`) only checks that a repo exists; its `SystemExit` text names `adversarial-architect`.
- Review artifacts (`feedback/`, OCR JSON) are not gitignored (`.gitignore` only has `.checkpoints/` and `*.log`).
- `ocr` is never invoked from this package.

### Implementation gate

Phases 1–2 (pure stop logic + injectable loop) can land anytime.

Phases 3–4 call `review-action` with PROJ-0017 gate flags (`--min-severity medium`). **Do not start Phase 3 until PROJ-0017 has added those flags.** Do not invent a driver-level low-severity workaround (planning decision). Document 0016+0017 as dogfood prerequisites in the README.

## Desired End State

```bash
cd <application-repo>          # on the PR branch, clean tree
review-driver --source my-pr   # --target defaults to main
```

The driver:

1. PrefLights: git repo, `ocr` on PATH, `HEAD` SHA equals `--source` SHA, no dirty **tracked** files outside the output dir.
2. Each pass N (children run **quiet**; the operator sees **phase-boundary summaries**, not live finding lines):
   - print `Pass N/max · OCR starting…`
   - `ocr review --from <target> --to <source> --format json --audience agent` → `.review-runs/code-review-rN.json`
   - print OCR summary (comments by severity, files reviewed, tokens, elapsed)
   - `review-analyzer` with `--no-tui`, `--knowledge-dir`, accumulating `--prior-feedback` of `feedback-r1…r{N-1}`
   - print triage summary (verdicts, VALID-by-severity, backlog promotion if present, wall-clock)
   - `review-action` with `--no-tui` and 0017 gates (`--min-severity medium`)
   - print action summary (committed / skipped / errors, cost if present, wall-clock)
   - print **pass rollup + trend vs previous pass** (novelty, severity mix) so you can see convergence
   - novelty = count of findings with verdict `VALID` and severity in `{high, medium}`
3. Stops when that count is 0 for K consecutive passes (default K=2) or `--max-passes` is hit (default 5).
4. Writes `progress.json` + `REPORT.md` under `.review-runs/`.
5. Exit 0 if converged; non-zero if preflight/step hard-fail, action errors occurred, or max-passes with remaining novelty.

### Operator UX (locked)

Less live chatter, more **summary between phases**. The question the terminal must answer is “are we converging?”

**During a phase:** one start line (`Pass 2/5 · analyzer starting…`). Child stdout/stderr goes to a log file under `.review-runs/logs/`, not the terminal. No TUI. No `Processed 5/29` / per-finding action lines on stdout. `--verbose` also writes those logs to stderr (still no TUI).

**After each phase:** a compact block. **After each pass:** the same numbers plus a trend vs the previous pass.

Canonical layout (plain text, fixed labels for tests):

```text
── Pass 2/5 ─────────────────────────────────────────
OCR starting…

OCR      comments 31  files 12  high 5  med 13  low 13  tokens 412345 (in 300000 / out 112345)  4m01s
Analyzer VALID 8 (H3 M4 L1)  BACKLOG 16  REJECTED 5  DUP 2  TIMEOUT 0  6m20s
Action   committed 4  skipped 3  errors 1  $0.12  3m50s

Pass 2   novelty=1  zeros=0/2  wall=14m11s
Trend    novelty 3→1  high 7→5  med 14→13  low 10→13  VALID 13→8
─────────────────────────────────────────────────────
```

Rules:

- **Severity** is OCR severity on findings (high / medium / low / unknown), not IDLC ticket status. Show (1) all comments by severity after OCR, (2) VALID-by-severity after analyzer — that is the auto-fix surface.
- **Time** is always wall-clock measured by the driver (`time.monotonic()` around each runner). Also print OCR `summary.elapsed` when the JSON has it (plant-tracking reports include `summary.elapsed`).
- **Tokens** when the artifact has them; otherwise omit the field (do not print `0` or invent numbers):
  - OCR: `summary.total_tokens` / `input_tokens` / `output_tokens` / `cache_read_tokens` (present on plant-tracking `code-review-proj-0013-r2.json` and r3).
  - Action: parse `Total cost: $…` from `review-action_summary.md` (`action_report.ActionRunBlock.cost_line`) when present.
  - Analyzer: **no token totals today** — show wall-clock only. Do not scrape opencode logs for tokens in MVP.
- **Trend** after pass ≥ 2: previous → current for novelty, OCR high/med/low, and VALID count. A declining novelty + declining high is the “converging” signal; rising low with falling high is called out as-is (that is the plant-tracking failure mode).
- Same blocks are appended to `REPORT.md` so a crashed or CI run is readable after the fact.

### Verification sketch

- `[3, 1, 0, 0]` + K=2 stops after pass 4 as converged.
- `[1, 1, 1]` + max-passes=3 stops as `max_passes`.
- Orchestrator calls OCR → analyzer → action in that order with injectable fakes (no live OCR/LLM).
- `--resume` continues at `current_pass + 1`; missing state + `--resume` fails fast.
- `parse_args` requires `--source`; `--target` defaults to `main`; `--output-dir` defaults to `.review-runs`.
- Phase-summary formatter, given fixture OCR JSON + feedback dirs, prints severity mix, novelty, wall-clock, and tokens-when-present; omits missing token/cost fields.

### Key Discoveries

- OCR flag names are inverted vs the ticket: `--from` is the **base** (`--target`), `--to` is the **PR branch** (`--source`). Confirmed via `ocr review --help`.
- Both review CLIs auto-start a TUI on a TTY (`review_analyzer.py:234–249`). The driver must always pass `--no-tui`.
- Analyzer `main()` is in-process callable; `load_ocr_json` can still `sys.exit(1)` on bad JSON (`review_analyzer.py:272–288`).
- Action commits every dirty file `get_modified_files` sees except the feedback dir (`git_ops.py:73–84`, harness `_exclude_output_dir`). That is why preflight refuses dirty tracked source files.
- Intra-OCR Jaccard (`content_similarity`, `review_analyzer.py:382–393`) is same-path dedup only — not a cross-pass novelty engine.
- `load_feedback_dir` already exposes `verdict` + lowercased `severity` (`feedback_report.py:48–65`, `146–148`). Novelty is a filter over that list.
- Real OCR JSON (plant-tracking r2/r3) already carries `summary.{comments,files_reviewed,total_tokens,input_tokens,output_tokens,cache_read_tokens,elapsed}` plus per-comment `severity`. That is the token/time source for the OCR phase — no extra OCR flags.
- Action summaries optionally include `Total cost: $…` (`review_action_harness.py:1293–1297`, parsed by `action_report.py:265–266`). Analyzer has per-finding `duration_s` but **no** run-level token total.
- ADR-011 puts harness checkpoints in repo-root `.checkpoints/` (gitignored). This driver keeps **state next to artifacts** under `.review-runs/` so one CI cache directory holds the whole run. Atomic write + fail-fast `--resume` are copied; the path is intentionally different.

## What We're NOT Doing

- Implementing PROJ-0016, PROJ-0017, or PROJ-0018.
- A driver-level skip of low-severity VALID (rejected workaround).
- Replacing OCR or changing the C4 harness.
- Auto-merge / auto-PR / auto-checkout of `--source`.
- Interactive confirm between passes.
- Live TUI or per-finding progress lines on the driver terminal (children stay `--no-tui` and their stdout is captured to logs).
- Scraping analyzer/opencode logs for token counts (no stable field today).
- Changing `review-action` commit-per-finding behavior to batch commits.
- Dry-run, cost budgets, notify-on-converge, machine-readable JSON summary (nice-to-haves).
- `review-browse` (PROJ-0015).
- Cross-pass fingerprint / Jaccard novelty (rejected; stop metric is this-pass high/medium VALID count).
- Reusing `HarnessProgress` / `.checkpoints/progress.json` as the driver state file.
- A third circuit breaker in the driver (children already have their own).
- Plant-tracking as a test dependency.

## Implementation Approach

Plain argparse CLI in the review-tool family. Split only what tests need to stay pure:

1. **`review_novelty.py`** — count + stop predicates. Single source of truth so PROJ-0018 can later import the same functions.
2. **`review_driver.py`** — progress model, preflight, injectable runners, loop, CLI.
3. Production runners: OCR via `subprocess.run`; analyzer/action via `main(argv)` with `--no-tui`.
4. Thresholds on `ThresholdConfig` (ADR-006 / ADR-009): never hardcode K or max-passes at call sites.

Keep commit-per-finding. Accumulate `--prior-feedback` as every previous `feedback-rN` under the output dir.

Operator-facing I/O is owned by the driver: measure wall-clock, parse artifacts, print phase summaries. Do not rely on child CLIs’ live reporters.

---

## Phase 1: Novelty metric and stop predicates

### Overview

Pure functions and unit tests for the stop rule. No CLI, no subprocess, no git.

### Changes Required

#### 1. Novelty + stop module

**File**: `deep_architect/review_novelty.py` (new)

```python
from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from deep_architect.feedback_report import load_feedback_dir

HIGH_SIGNAL_SEVERITIES: frozenset[str] = frozenset({"high", "medium"})
DEFAULT_ZERO_NOVELTY_PASSES = 2
DEFAULT_MAX_PASSES = 5


class StopReason(StrEnum):
    CONTINUE = "continue"
    CONVERGED = "converged"
    MAX_PASSES = "max_passes"


def count_high_signal_valid(feedback_dir: Path) -> int:
    """Count VALID findings whose OCR severity is high or medium.

    Missing / unknown / low severity do not count. DUPLICATE, BACKLOG,
    TIMEOUT, REJECTED never count. Catalog/dedup already happened in
    review-analyzer; remaining high/medium VALID *are* the novelty signal.
    """
    report = load_feedback_dir(feedback_dir)
    return sum(
        1
        for finding in report.findings
        if finding.verdict == "VALID" and finding.severity in HIGH_SIGNAL_SEVERITIES
    )


def consecutive_zero_novelty(history: list[int]) -> int:
    """Trailing count of zeros; empty history → 0."""
    n = 0
    for value in reversed(history):
        if value == 0:
            n += 1
        else:
            break
    return n


def decide_stop(
    *,
    novelty_history: list[int],
    k: int,
    max_passes: int,
) -> StopReason:
    """Decide after a pass has been appended to *novelty_history*.

    Converged takes priority when the last pass also hits max-passes.
    """
    if consecutive_zero_novelty(novelty_history) >= k:
        return StopReason.CONVERGED
    if len(novelty_history) >= max_passes:
        return StopReason.MAX_PASSES
    return StopReason.CONTINUE
```

Also in this module (pure, unit-tested) — used by the driver’s phase summaries:

```python
def count_valid_by_severity(feedback_dir: Path) -> dict[str, int]:
    """VALID findings bucketed by OCR severity (missing → 'unknown')."""

def count_findings_by_severity(feedback_dir: Path) -> dict[str, int]:
    """All findings bucketed by OCR severity."""

def count_verdicts(feedback_dir: Path) -> dict[str, int]:
    """Verdict histogram via load_feedback_dir."""

def parse_ocr_run_stats(ocr_json: Path) -> OcrRunStats:
    """Read optional OCR JSON ``summary`` object.

    Fields (all optional): comments, files_reviewed, total_tokens,
    input_tokens, output_tokens, cache_read_tokens, elapsed.
    Missing ``summary`` → empty stats, not an error.
    """
```

`OcrRunStats` is a small frozen dataclass. Severity keys stay lowercase (`high` / `medium` / `low` / `unknown`).

Named constants live here. Loop code reads `config.thresholds.review_driver_zero_novelty_passes` / `review_driver_max_passes` and passes them in — it does not hardcode 2 or 5.

#### 2. Tests

**File**: `tests/test_review_novelty.py` (new)

- `decide_stop` on `[3, 1, 0, 0]`, K=2, max=5 → `CONVERGED` (checked after each append; first `CONVERGED` is after pass 4).
- `decide_stop` on `[1, 1, 1]`, K=2, max=3 → `MAX_PASSES`.
- `[0]` + K=2 → `CONTINUE` (only one zero).
- `[0, 0]` + K=2 → `CONVERGED`.
- Hitting max-passes on the same pass as the K-th zero → `CONVERGED`.
- `count_high_signal_valid` fixture dir:
  - 2 high VALID + 1 medium VALID + 1 low VALID + 1 high BACKLOG + 1 VALID with empty severity → count **3**.
  - Empty dir / only `SUMMARY.md` → 0.
- `count_valid_by_severity` on the same fixture → `{high: 2, medium: 1, low: 1, unknown: 1}`.
- `parse_ocr_run_stats` on a plant-tracking-shaped JSON (`summary.total_tokens`, `elapsed`) returns those fields; JSON with only `comments` returns empty optionals.
- Missing directory → `FileNotFoundError` from `load_feedback_dir` (propagate; do not swallow).

Build fixtures with the same markdown shape `parse_markdown_finding` expects (`**File**`, `**Severity**`, `**Existing Code**`, `**Review Comment**`, `**Verdict**`).

### Success Criteria

#### Automated Verification

- [x] `uv run python -m pytest tests/test_review_novelty.py -v`
- [x] Sequences `[3,1,0,0]` and `[1,1,1]` match the ticket
- [x] OCR `summary` parser + VALID-by-severity histogram unit tests pass
- [x] `uv run ruff check deep_architect/review_novelty.py tests/test_review_novelty.py`
- [x] `uv run mypy deep_architect/review_novelty.py`

#### Manual Verification

- [ ] None this phase (pure functions)

**Implementation Note**: After automated verification passes, proceed to Phase 2.

---

## Phase 2: Orchestrator loop with injectable runners

### Overview

A `run_driver()` loop that owns pass numbering, prior-feedback accumulation, state persistence, and stop decisions. All external tools go through a `ReviewStepRunners` protocol so tests never call OCR or an LLM.

### Changes Required

#### 1. Progress model + atomic I/O

**File**: `deep_architect/review_driver.py` (new)

Do **not** add fields to `HarnessProgress`. Driver state is a separate Pydantic model written under the output dir.

```python
class DriverPassRecord(BaseModel):
    pass_index: int  # 1-based
    ocr_json: str
    feedback_dir: str
    novelty: int
    valid_total: int
    ocr_severity: dict[str, int] = Field(default_factory=dict)  # all comments
    valid_by_severity: dict[str, int] = Field(default_factory=dict)
    verdicts: dict[str, int] = Field(default_factory=dict)
    action_errors: int
    action_committed: int
    action_skipped: int = 0
    ocr_tokens_total: int | None = None
    ocr_elapsed_s: float | None = None  # from JSON summary, if present
    phase_seconds: dict[str, float] = Field(default_factory=dict)  # ocr/analyzer/action
    wall_seconds: float = 0.0
    action_cost_usd: float | None = None
    status: Literal["complete", "failed"]


class DriverProgress(BaseModel):
    status: Literal["running", "converged", "max_passes", "failed"] = "running"
    source: str
    target: str
    source_sha: str
    target_sha: str
    max_passes: int
    k: int
    current_pass: int = 0  # last *completed* pass; 0 if none
    consecutive_zero_novelty: int = 0
    novelty_history: list[int] = Field(default_factory=list)
    passes: list[DriverPassRecord] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    output_dir: str
```

Save/load (same atomic pattern as `io/files.py:166–177`):

```python
PROGRESS_FILENAME = "progress.json"

def save_driver_progress(output_dir: Path, progress: DriverProgress) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / PROGRESS_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(progress.model_dump_json(indent=2))
    os.replace(tmp, path)
    return path

def load_driver_progress(output_dir: Path) -> DriverProgress:
    path = output_dir / PROGRESS_FILENAME
    return DriverProgress.model_validate_json(path.read_text())
```

Artifact names (stable, 1-based):

- `{output_dir}/code-review-r{N}.json`
- `{output_dir}/feedback-r{N}/`
- `{output_dir}/logs/r{N}-ocr.log`
- `{output_dir}/logs/r{N}-analyzer.log`
- `{output_dir}/logs/r{N}-action.log`

#### 2. Runner protocol

```python
class ReviewStepRunners(Protocol):
    def run_ocr(
        self, *, source: str, target: str, output_json: Path, exclude: list[str]
    ) -> int: ...

    def run_analyzer(
        self,
        *,
        ocr_json: Path,
        feedback_dir: Path,
        prior_feedback: list[Path],
        knowledge_dir: Path | None,
        exclude: list[str],
    ) -> int: ...

    def run_action(self, *, feedback_dir: Path) -> int: ...
```

Return codes: `0` success; `130` interrupted; `1` failure. For **action only**, `1` means “pass finished with per-finding errors” (action already continues the batch) — the loop records errors and continues unless the process was interrupted.

#### 3. Loop

```python
def run_driver(
    *,
    source: str,
    target: str,
    output_dir: Path,
    runners: ReviewStepRunners,
    max_passes: int,
    k: int,
    resume: bool = False,
    knowledge_dir: Path | None = None,
    exclude: list[str] | None = None,
    source_sha: str = "",
    target_sha: str = "",
) -> DriverProgress:
```

Behavior:

1. If `resume`: `load_driver_progress`; fail with `FileNotFoundError` if missing (message names the path and says to omit `--resume`). Require `progress.source == source` and `progress.target == target`; mismatch → `ValueError`. Reset `status` to `"running"`. `start = progress.current_pass + 1`.
2. Else: new `DriverProgress`, `start = 1`.
3. For `pass_index` in `range(start, max_passes + 1)`:
   - `ocr_json = output_dir / f"code-review-r{pass_index}.json"`
   - `feedback_dir = output_dir / f"feedback-r{pass_index}"`
   - `prior = [output_dir / f"feedback-r{i}" for i in range(1, pass_index)]` (existing dirs only)
   - print pass header + `OCR starting…`
   - time `run_ocr` → non-zero (including 130) → mark pass `failed`, `progress.status = "failed"`, save, return
   - parse OCR JSON; print OCR phase summary (severity histogram from `comments[].severity`, tokens/elapsed from `summary` if present, driver wall-clock)
   - print `Analyzer starting…`; time `run_analyzer` → same hard-fail
   - print analyzer phase summary from `load_feedback_dir` (verdicts, VALID-by-severity)
   - print `Action starting…`; time `run_action` → `130` → failed/return; `1` → record errors, still count novelty; `0` → ok
   - print action phase summary (committed/skipped/errors; `cost_line` if parseable)
   - `novelty = count_high_signal_valid(feedback_dir)`
   - append history + `DriverPassRecord(status="complete")` including histograms, tokens, phase timings
   - `consecutive_zero_novelty = consecutive_zero_novelty(history)`
   - `current_pass = pass_index`
   - save progress
   - print pass rollup + trend vs previous `DriverPassRecord` (novelty, high/med/low, VALID)
   - refresh `REPORT.md` from `progress.passes`
   - `decide_stop` → break with `converged` / `max_passes`
4. Print a final stop line (`Converged (K=2).` / `Stopped: max-passes with novelty remaining.`) and the full pass table.

Phase-summary helpers (`format_ocr_summary`, `format_analyzer_summary`, `format_action_summary`, `format_pass_rollup`, `format_trend`) are pure functions: they take stats dataclasses and return multiline strings. The loop prints those strings. Tests assert on the formatters, not on `capsys` of a full live run.

`print` goes to stdout (CI logs). Do not use Rich live displays.

Mid-pass crash: that pass is not in `current_pass`. Resume **restarts** that pass and overwrites `code-review-rN.json` / `feedback-rN/`. Same atomic unit as ADR-011 (completed pass).

Do not swallow exceptions: log, set `status="failed"`, re-raise.

#### 4. Tests

**File**: `tests/test_review_driver.py` (new)

Fake runners that:

- Write a tiny valid OCR JSON (empty `comments` is enough if the analyzer is faked).
- Write fixture finding markdown into `feedback_dir` with a scripted novelty count per pass.
- Record call order and analyzer `prior_feedback` arguments.

Cases:

- Call order is always OCR → analyzer → action per pass.
- Pass 2 analyzer receives `prior_feedback == [output/feedback-r1]`.
- Scripted novelties `[3, 1, 0, 0]` → 4 OCR calls, status `converged`.
- Scripted `[1, 1, 1]` + max_passes=3 → status `max_passes`.
- OCR rc=1 on pass 2 → status `failed`, `current_pass == 1`, pass 2 record `failed` or absent.
- Action rc=1 then novelty 0,0 → still converges; progress records `action_errors`.
- Resume: seed `progress.json` with pass 1 complete; fake pass 2; assert OCR not called for r1; r2 prior includes feedback-r1.
- `--resume` with no file → `FileNotFoundError`.
- `save_driver_progress` leaves no `.tmp` remnant (mirror `test_files.py`).
- `format_ocr_summary` includes `tokens` when `total_tokens` is set and omits the word `tokens` when it is `None`.
- `format_trend` on two records with novelty 3→1 and high 7→5 contains `novelty 3→1` and `high 7→5`.
- Loop stdout (`capsys`) after a 2-pass fake run contains `Pass 1/`, `OCR starting`, `Analyzer starting`, `Action starting`, `novelty=`, and `Trend`; it does **not** contain `Processed ` or `Applying fixes`.

No live OCR/LLM. No `subprocess.run` for ocr in this phase (fakes only).

### Success Criteria

#### Automated Verification

- [x] `uv run python -m pytest tests/test_review_driver.py tests/test_review_novelty.py -v`
- [x] Orchestrator order + prior-feedback accumulation asserted
- [x] Resume and fail-fast covered
- [x] Phase-summary / trend formatters cover tokens-omitted and 3→1 novelty trend
- [x] Loop stdout is phase summaries, not child live progress
- [x] `uv run ruff check deep_architect/review_driver.py tests/test_review_driver.py`
- [x] `uv run mypy deep_architect/review_driver.py`

#### Manual Verification

- [ ] None this phase

**Implementation Note**: After automated verification passes, proceed to Phase 3 only if PROJ-0017 `--min-severity` exists on `review-action`.

---

## Phase 3: Production runners and preflight

### Overview

Real OCR subprocess, in-process analyzer/action, and git preflight. Still no public CLI (a thin `main` can exist for manual dogfood, but packaging/README wait for Phase 4).

### Changes Required

#### 1. Preflight

**File**: `deep_architect/review_driver.py`

Do **not** call `validate_git_repo` (wrong error text). Use GitPython directly.

```python
def preflight_driver(
    *,
    cwd: Path,
    source: str,
    target: str,
    output_dir: Path,
    ocr_bin: str,
) -> tuple[git.Repo, str, str]:
    """Return (repo, source_sha, target_sha). Raise DriverPreflightError on failure."""
```

Checks, in order:

1. `git.Repo(cwd, search_parent_directories=True)` — else clear “not a git repo” error.
2. `shutil.which(ocr_bin)` (default `"ocr"`, override `OCR_BIN`) — else “install OpenCodeReview `ocr` CLI”.
3. Resolve `source` and `target` via `repo.commit(ref).hexsha`. Missing ref → error naming the flag.
4. `repo.head.commit.hexsha == source_sha`. Detached HEAD at that SHA is OK. Wrong commit → error: check out `--source` first (driver does **not** checkout).
5. Dirty **tracked** files: union of `repo.index.diff(None)` (unstaged) and `repo.index.diff("HEAD")` (staged). Drop paths under `output_dir` (resolved relative to `repo.working_dir`). Any remainder → error listing paths. Untracked files are **not** blocked (planning decision); document the review-action footgun in README.

`DriverPreflightError` is a dedicated `Exception` subclass; `main()` maps it to stderr + exit 1.

#### 2. OCR runner

```python
def run_ocr_subprocess(
    *, source: str, target: str, output_json: Path, exclude: list[str],
    cwd: Path, ocr_bin: str = "ocr",
) -> int:
```

```text
ocr review --from {target} --to {source} --format json --audience agent --repo {cwd}
           [--exclude {comma-joined}] 
```

- `capture_output=True`, `text=True`, timeout from env `REVIEW_DRIVER_OCR_TIMEOUT` or a named constant (default 3600s). This timeout is a driver safety cap, not a novelty threshold — keep it as a module constant + env override; do not add a TOML key unless Phase 4 config work already touches the same area.
- On `FileNotFoundError` / `TimeoutExpired`: log and return 1 (or raise — either is fine if `run_driver` treats it as a failed pass; prefer return 1 so the loop’s status handling stays in one place).
- Success: write `result.stdout` to `output_json` (create parents). If stdout is empty, log stderr and return 1.
- **Quiet:** `ocr` stderr is appended to `{output_dir}/logs/r{N}-ocr.log`, not streamed to the terminal. `--audience agent` already suppresses OCR progress lines in stdout (which is the JSON payload).
- Never use `--from {source} --to {target}`.

#### 3. Analyzer runner

Call `review_analyzer.main([...])` with:

- positional `ocr_json`
- `--output-dir {feedback_dir}`
- `--no-tui`
- `--knowledge-dir {knowledge_dir}` if provided, else omit (analyzer defaults to `<cwd>/knowledge`)
- one `--prior-feedback {dir}` per prior dir
- one `--exclude {glob}` per exclude

Catch `SystemExit` from `load_ocr_json` and return `exc.code` (int or 1).

Redirect `sys.stdout` and `sys.stderr` to `{output_dir}/logs/r{N}-analyzer.log` for the duration of `main()` so `PlainReporter` (`Processed k/N`) does not hit the driver terminal. Restore streams in a `finally`. The driver prints its own analyzer summary after `main()` returns.

#### 4. Action runner (requires PROJ-0017)

Call `review_action_harness.main([...])` with:

- positional `feedback_dir`
- `--no-tui`
- `--min-severity medium` **(PROJ-0017 — must exist)**
- passthrough when the driver CLI has them: `--provider`, `--model`, `--config`

Do not pass `--force`. Do not pass `--tui`.

Same stdout/stderr redirect to `{output_dir}/logs/r{N}-action.log` so per-finding `PlainReporter` lines stay off the driver terminal. Parse `review-action_summary.md` afterwards (`action_report`) for committed/skipped/errors and optional `cost_line`.

If 0017 is not in tree, **stop implementation** rather than omitting the flag.

#### 5. Tests

- Preflight: tmp git repo on the wrong branch → error; matching SHA (branch or detached) → ok.
- Dirty tracked file outside output dir → error; dirty file *inside* output dir → ok; untracked file outside → ok.
- Missing `ocr` → error (`monkeypatch` `shutil.which`).
- OCR runner builds `--from target --to source --format json --audience agent` (`patch("subprocess.run")`).
- Analyzer runner: `patch("deep_architect.review_analyzer.main", return_value=0)` and assert argv contains `--no-tui` and prior-feedback dirs.
- Action runner: `patch("deep_architect.review_action_harness.main")` and assert `--no-tui` and `--min-severity medium`.

Use `git.Repo.init(tmp_path)` like `test_git_ops.py`.

### Success Criteria

#### Automated Verification

- [x] Preflight tests cover wrong SHA, dirty tracked, missing ocr, good detached HEAD
- [x] OCR argv maps `--source` → `--to`, `--target` → `--from`
- [x] Children always receive `--no-tui`
- [x] Action argv includes `--min-severity medium`
- [x] `uv run python -m pytest tests/test_review_driver.py tests/test_review_novelty.py -v`
- [x] ruff / mypy clean on touched files

#### Manual Verification

- [ ] From a throwaway clone: run the OCR runner only (`--preview` is OCR’s, not ours) with patched/real `ocr review --preview --from main --to HEAD` if a branch exists — confirm the flag order against `ocr review --help`. Optional this phase.

**Implementation Note**: Pause here if 0017 is missing. Do not ship an action runner without the severity gate flag.

---

## Phase 4: CLI, config, reports, packaging, docs

### Overview

Public `review-driver` command, thresholds in config, human report, README, quality bar.

### Changes Required

#### 1. Config thresholds

**File**: `deep_architect/config.py` — add to `ThresholdConfig` (`config.py:22–49`):

```python
review_driver_max_passes: int = 5
review_driver_zero_novelty_passes: int = 2
```

**File**: `config.toml.template` — document the two keys under `[thresholds]`.

CLI flags override config after load (same pattern as `cli.py` mutating `cfg`). Missing config file: follow **review-action**, not the architecture CLI — use `HarnessConfig()` defaults and log a warning (`review_action_harness.py:1383–1392`). The driver must be runnable in a repo that has no `~/.config/deep-architect/config.toml`.

#### 2. argparse CLI

**File**: `deep_architect/review_driver.py`

```text
review-driver --source BRANCH
              [--target main]
              [--output-dir .review-runs]
              [--max-passes N]
              [--zero-novelty-passes K]
              [--resume]
              [--exclude GLOB]          # repeatable; passed to ocr + analyzer
              [--knowledge-dir PATH]
              [--provider NAME]
              [--model NAME]
              [--config PATH]
              [--verbose]
```

`--source` is **required**. `--target` default `main`. `--output-dir` default `Path(".review-runs")`.

`main(argv) -> int`:

1. `parse_args`
2. `logging.basicConfig` (DEBUG if `--verbose`) — same format as other review CLIs
3. SIGINT → 130 after the current step returns (module-level flag is enough; do not add a TUI)
4. Load config (defaults if missing); apply CLI overrides for max-passes / K
5. `preflight_driver`
6. `run_driver(..., runners=ProductionRunners(...), resume=args.resume)`
7. Write full `REPORT.md`
8. Exit codes:
   - `0` — `status == "converged"` **and** no recorded `action_errors` across passes
   - `130` — interrupted
   - `1` — preflight failure, `failed`, `max_passes`, or any `action_errors`

Ticket: “0 if converged; non-zero if errors or max-passes with remaining novelty.” Converged-with-action-errors is non-zero.

Always unattended. No confirm. No TUI.

#### 3. `REPORT.md`

Written at `{output_dir}/REPORT.md` at the end (and refresh after each completed pass so a crash still has a partial report).

Contents (same numbers as the terminal, so a CI log and the file agree):

- Source / target / resolved SHAs
- K, max-passes, stop reason
- Per-pass copy of the phase-summary blocks (OCR / analyzer / action / rollup / trend)
- Table: pass, novelty, high/med/low, VALID, committed, errors, tokens, wall time, artifact paths
- Novelty history as a list
- Reminder: stop is high/medium VALID count, **not** OCR comment count

Also print the same table to stdout at the end (plain text).

#### 4. Packaging

**File**: `pyproject.toml`

```toml
review-driver = "deep_architect.review_driver:main"
```

**File**: `justfile` — add a `review-driver:` recipe that runs `uv run review-driver --help`.

**File**: `.gitignore` — add `.review-runs/` (dogfood the application-repo convention in this repo).

#### 5. README

**Files**: `README.md` install list (`:41–51`) and a new **Review Driver** section after Review Action.

Document:

- Purpose and position in the pipeline (does not replace analyzer/action)
- Prerequisites: `ocr` on PATH; run from **application repo root**; PROJ-0016 memory + PROJ-0017 gates required for the loop to terminate honestly; without 0016 the driver must not be treated as “converged means nothing left to discuss”
- `--source` / `--target` semantics and the OCR mapping (`--target` → `ocr --from`, `--source` → `ocr --to`)
- Default output layout:

```text
.review-runs/
  progress.json
  REPORT.md
  logs/r1-ocr.log
  logs/r1-analyzer.log
  logs/r1-action.log
  code-review-r1.json
  feedback-r1/
  code-review-r2.json
  feedback-r2/
```

- Add `.review-runs/` to the **application** `.gitignore`. Artifacts are CI-cacheable; committing them is the operator’s choice (same stance as feedback dirs today).
- Flags table (review-analyzer style: Flag | Default | Description)
- Stop rule: high/medium VALID count == 0 for K consecutive passes; never “OCR is empty”
- Preflight: HEAD SHA == `--source`; dirty tracked files refused; no auto-checkout
- Unattended: local and CI are the same command
- Terminal UX: quiet during a phase; summary after OCR, after analyzer, after action, and a pass trend (severity + novelty + time + tokens-when-known). Child live progress is in `logs/` only. `--verbose` tees logs to stderr.
- `--resume` and fail-fast if `progress.json` missing
- Exit codes: 0 converged (and no action errors); 1 otherwise; 130 interrupt
- Failure modes: missing ocr, wrong branch, dirty tree, OCR/analyzer hard fail, max-passes with novelty remaining
- Example:

```bash
# on the PR branch, clean tree
review-driver --source my-feature --target main --max-passes 5

# CI (same)
review-driver --source "$HEAD_BRANCH" --target main --output-dir .review-runs
```

Update the install verify block to include `review-driver --help`.

#### 6. CLI tests

Extend `tests/test_review_driver.py`:

- `parse_args(["--source", "feat"])` → target `main`, output `.review-runs`
- missing `--source` → `SystemExit`
- `--output-dir other/` honored
- `main([...])` with preflight + `run_driver` patched: `--resume` threaded through; exit 0 on converged progress; exit 1 on max_passes

### Success Criteria

#### Automated Verification

- [x] CLI requires `--source`; target defaults to `main`
- [x] Default output dir is `.review-runs`; override honored
- [x] Console script registered: `uv run review-driver --help` works
- [x] README documents usage, layout, source/target, 0016/0017 prerequisites, flags, failure modes, human vs CI (they are the same)
- [x] `uv run ruff check deep_architect/ tests/`
- [x] `uv run mypy deep_architect/`
- [x] `uv run python -m pytest tests/ -v`
- [x] `uv run bandit -r deep_architect/ -ll`

#### Manual Verification

- [ ] After 0016+0017 on a real PR branch (plant-tracking or similar): `review-driver --source <pr-branch> --max-passes 3` — artifacts under `.review-runs/`; terminal shows phase summaries (severity, novelty, time, tokens if OCR JSON has them), not per-finding lines; either `converged` or `max_passes` with a matching `REPORT.md`
- [ ] Wrong branch / dirty tracked file: preflight fails with a readable error, no OCR started
- [ ] Ctrl-C mid-pass: exit 130; `--resume` restarts that pass
- [ ] Confirm the driver does **not** claim convergence just because OCR still has low-severity / BACKLOG comments

**Implementation Note**: This phase is the dogfood gate. Ticket manual verification (“without 0016 memory, do not silently claim convergence while re-fixing backlog themes”) is an operator check, not a unit test.

---

## Testing Strategy

### Unit Tests

| Area | Cases |
|------|--------|
| `decide_stop` | `[3,1,0,0]` / `[1,1,1]` / single zero / converge-beats-max |
| `count_high_signal_valid` | high+medium count; low/BACKLOG/missing severity ignored |
| Histograms / OCR stats | VALID-by-severity; `parse_ocr_run_stats` optional fields |
| Phase formatters | tokens omitted when unknown; trend 3→1 / high 7→5 |
| Progress I/O | round-trip, no `.tmp` remnant |
| Loop | runner order, prior-feedback accumulation, resume, OCR fail |
| Preflight | SHA match, detached OK, dirty tracked, missing ocr |
| CLI | required `--source`, defaults, exit codes |
| OCR argv | `--from target --to source` |

No live OCR, opencode, or coding-agent calls. Patch `subprocess.run` and `*.main`.

### Integration Tests

Not required for MVP beyond the injectable-runner loop test (that *is* the integration seam).

### Manual Testing Steps

1. Check out a PR branch with 0016 catalog populated and 0017 gates in the installed package.
2. Ensure a clean tree; run `review-driver --source $(git branch --show-current) --max-passes 3`.
3. Inspect `.review-runs/REPORT.md` and `progress.json`.
4. Confirm `feedback-r2` analyzer logs / INDEX show prior-feedback influence (recurring themes BACKLOG).
5. Confirm action did not receive `--tui` and used `--min-severity medium`.
6. Interrupt and `--resume`.
7. Re-run on a dirty file; confirm refuse.

## Performance Considerations

- One OCR + one analyzer + one action per pass. No extra LLM in the driver.
- Novelty is a linear scan of one feedback dir (tens of files).
- `ocr review` is the expensive step; `--audience agent` avoids progress-line noise in captured stdout.
- Analyzer concurrency stays at analyzer’s default (5) unless we later add a passthrough (out of scope).

## Migration Notes

- New command only. Existing `review-analyzer` / `review-action` invocations unchanged.
- `.review-runs/` is new; add to app-repo `.gitignore`.
- Operators who still run the three commands by hand are unaffected.
- When PROJ-0018 lands, it should import `count_high_signal_valid` / `decide_stop` from `review_novelty.py` rather than fork the metric.
- When implementing, if `review-action --help` lacks `--min-severity`, stop and finish PROJ-0017 first.

## Locked Decisions (planning)

| Topic | Decision |
|-------|----------|
| CLI name | `review-driver` |
| Default output | `.review-runs/` at repo root; document gitignore |
| Novelty | Count of this-pass high/medium VALID (0016 already applied) |
| 0017 gap | Prerequisite only; no driver-level low skip |
| Checkout | Require `HEAD` SHA == `--source` SHA; no auto-checkout |
| Dirty tree | Refuse dirty **tracked** files outside output dir |
| UX | Always unattended (local == CI) |
| Live progress | Quiet during phases; summaries at phase boundaries |
| Convergence view | Novelty + OCR severity mix + VALID-by-severity + wall-clock + OCR tokens / action cost when present |
| OCR mapping | `--target` → `ocr --from`; `--source` → `ocr --to` |
| Analyzer/action | In-process `main(argv)` + `--no-tui` |
| OCR | Subprocess |
| Commits | Unchanged: one commit per finding inside review-action |
| State file | `{output_dir}/progress.json` (not `.checkpoints/`) |
| Defaults | K=2, max-passes=5 |
| Config missing | Defaults + warning (review-action style) |

## References

- Original ticket: `knowledge/tickets/PROJ-0020.md`
- Superseded novelty-loop spec: `knowledge/tickets/PROJ-0019.md`
- Dependencies: `knowledge/tickets/PROJ-0016.md`, `PROJ-0017.md`
- Metric sibling (not implemented): `knowledge/tickets/PROJ-0018.md`
- Catalog plan (analyzer memory already in tree): `knowledge/plans/2026-08-09-PROJ-0016-catalog-aware-triage.md`
- Resume pattern: `knowledge/adr/ADR-011-resume-via-progress-json.md`
- Exit-criteria pattern: `knowledge/adr/ADR-006-exit-criteria.md`
- Config split: `knowledge/adr/ADR-009-config-split-toml-vs-env.md`
- Code: `deep_architect/review_analyzer.py`, `review_action_harness.py`, `feedback_report.py`, `exit_criteria.py`, `io/files.py`, `config.py`
- Tests to mirror: `tests/test_exit_criteria.py`, `tests/test_review_analyzer.py` (`TestParseArgs*`), `tests/test_harness_retry.py` (resume), `tests/test_files.py` (atomic progress)
