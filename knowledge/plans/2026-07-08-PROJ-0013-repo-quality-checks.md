# Repo Quality Checks for review-action Implementation Plan

## Overview

Give `review-action` (the review-action harness in `deep_architect/review_action_harness.py`)
a real post-fix quality gate: after a coding agent addresses a review finding, discover and run
the target repo's declared quality checks — both **programmatic** (ruff/mypy/pytest/bandit/…)
and **LLM-judged** (`.opencodereview/rules/*.md`-style style guides) — and loop failures back
into the fix agent until clean or a bounded iteration cap is hit. Fail-closed: a finding is
never committed/marked resolved while checks it introduced are failing.

## Current State Analysis

- The hook point is `_process_single_finding()` (`deep_architect/review_action_harness.py:666`):
  fix → `run_validation()` (`review_action_harness.py:798`) → commit. On validation failure the
  commit is **skipped** — there is no feedback to the agent and no retry of the fix.
- Validation commands are hardcoded to `[["ruff", "check"], ["mypy"]]` with the single finding
  file appended (`review_action_harness.py:1297`, `run_validation` at line 631). Per-repo /
  per-subproject toolchains are ignored entirely.
- The existing retry loop (`review_action_harness.py:731`) retries **agent-invocation failures**
  only (subprocess crash, SDK error) — a check-driven fix loop is a separate, new concern.
- The `CodingAgent` protocol (`review_action_harness.py:95`) has a single `apply_fix()` method;
  both `OpencodeAgent` (line 114) and `ClaudeSDKAgent` (line 1049) implement it.
- `deep_architect/agents/client.py` provides `run_simple_structured()` (line 143): a direct
  Anthropic API call with Pydantic-validated JSON output, retries, optional circuit breaker,
  and RunStats accumulation — the right vehicle for a no-tool LLM style judgment (ADR-005).
- `HarnessConfig.thresholds` (`deep_architect/config.py:17`) is where all tunables live;
  review-action already reads it in `main()` (`review_action_harness.py:1292`).
- `git_ops.py` provides `validate_git_repo()`, `git_commit()`, `get_modified_files()` (git
  status–based modified-file detection, used by the main harness).

### Reference repo findings (`~/repos/plant-tracking`)

- Programmatic checks are declared in one root `Justfile`:
  - `just check` (root CLI): `ruff check commands/ tests/`, `black --check commands/ tests/`,
    `mypy commands/`, `bandit -r commands/ -s B403,B404,B405,B406,B407,B408,B409`,
    `uv run pytest tests/ -v`
  - `just api-check` (FastAPI backend): `ruff check backend/fastapi/src/`,
    `black --check backend/fastapi/src/`, `mypy backend/fastapi/src/`,
    `uv run pytest backend/fastapi/tests/ -v` (no bandit)
  - `packages/plant_service/` has tool configs in its `pyproject.toml` (ruff select E,F,W,C90 /
    ignore C901; bandit as dev dep) but **no runnable recipe at all** — pure auto-detection
    from Justfiles cannot cover it. An explicit declaration file is required to close this gap.
- Path→check mapping is only implied by hardcoded directory args in recipes; no mapping file.
- LLM rules: `.opencodereview/rules/*.md` are compiled by `.opencodereview/generate-rules.py`
  into `.opencodereview/rule.json` — a JSON list of `{"path": "<glob>", "rule": "<markdown>"}`
  entries, scoped by gitwildmatch-style globs (`**/*.py`, `**/main.py`, `**/tests/**/*.py`,
  `**/routers/**/*.py`), NOT by subproject directory. Rules carry citable IDs (`PY-LANG-*`,
  `PY-STY-*`, `PYSCG-*`) with a **MUST > SHOULD > MAY > NIT** severity ladder.
- Conflict wrinkle: `python-style.md` PY-STY-002 mandates 80-char lines (MUST) while both
  backend `pyproject.toml`s set ruff `line-length = 100`. The LLM judge must be told
  programmatic tool config wins on overlap, or it will flag every long line.

## Desired End State

After a fix is applied to a target repo, `review-action`:

1. Discovers the repo's checks from `.quality-checks.toml` (explicit, glob-scoped profiles),
   falling back to auto-detection from `pyproject.toml` when the file is absent.
2. Runs the programmatic commands of every profile matching a modified file; only failures
   **introduced by the fix** (baseline diff) block.
3. Once programmatic checks are clean, runs one LLM judgment per modified `.py` file against
   the matching `.opencodereview` rule entries; MUST/SHOULD violations block, MAY/NIT are
   advisory.
4. Feeds any blocking failure back to the coding agent and retries, up to
   `thresholds.check_max_fix_iterations` (default 3).
5. On exhaustion: restores the modified files (fail-closed — no commit, finding marked
   `error` so a rerun retries it), records the failing checks in `## Action Taken`.

Verify: unit tests green, all four repo gates pass, and a manual run against plant-tracking
exercises all three subproject profiles plus the LLM rules.

### Key Discoveries:
- Feedback-loop gap: `run_validation` failure only skips the commit
  (`review_action_harness.py:798-814`) — the fix agent never hears about it.
- `run_simple_structured()` (`agents/client.py:143`) already gives structured pass/fail LLM
  calls with retries + cost tracking; no new SDK plumbing needed for the judge.
- `rule.json` is the machine-readable artifact to consume for LLM rules; its glob convention
  (gitwildmatch) matches what `pathspec` implements — one matching mechanism serves both
  check profiles and rule scoping.
- A failed finding currently leaves the working tree dirty, which would poison the next
  finding's `get_modified_files()` and baseline — the new loop must restore files on final
  failure.
- Thresholds must come from `HarnessConfig.thresholds` (CLAUDE.md: never hardcode).

## What We're NOT Doing

- Non-Python toolchains/languages (ticket: out of scope).
- Parsing Justfiles for auto-detection (fragile; explicit file covers Justfile-based repos).
- Auto-detecting test commands (test scope can't be inferred safely; auto-detect covers
  ruff/black/mypy/bandit only, file-scoped).
- Fine-grained output diffing for baseline (line-level normalization) — command-level baseline
  with a modified-file-line refinement (see Phase 2); deeper diffing is a future refinement.
- Writing an ADR (can follow after the pattern proves out) or touching the generator/critic
  harness loop.
- Consuming `.opencodereview/rule.json` for anything other than the rule text + glob scoping
  (no integration with the `ocr` CLI itself).
- Baseline caching across findings (performance optimization, deferred — see Performance
  Considerations).

## Implementation Approach

Two new modules keep the harness surgical:

- `deep_architect/quality_checks.py` — convention loading (`.quality-checks.toml`),
  auto-detection fallback, glob→profile matching, command execution, baseline capture/diff.
- `deep_architect/llm_judge.py` — `.opencodereview/rule.json` (or `rules/*.md`) loading,
  per-file judgment via `run_simple_structured()`, severity gating.
- Pydantic models for both live in `deep_architect/models/checks.py` (matches `models/`
  layout; round-trip-testable like the other models).

`review_action_harness.py` changes are confined to: a new `fix_check_failures()` method on
the `CodingAgent` protocol + both implementations, a new check-loop inside
`_process_single_finding()` replacing the `run_validation` call, config/CLI wiring, and
removal of the superseded `ValidationConfig`/`run_validation`.

New dependency: `pathspec>=0.12` (pure-Python gitwildmatch matching, used for both profile
paths and rule globs; stdlib `fnmatch`/`PurePath.match` don't handle `**` correctly on the
Python versions this repo supports).

Settled design decisions (from planning discussion):
- Declaration: explicit `.quality-checks.toml` + auto-detect fallback.
- Pre-existing failures: baseline diff — only failures introduced by the fix block.
- LLM judge: one `run_simple_structured` call per modified file, diff-focused, all matching
  rules concatenated; MUST/SHOULD block, MAY/NIT advisory.
- Loop order: LLM judge gated behind clean programmatic checks each iteration.
- Bounded exit: simple iteration cap `thresholds.check_max_fix_iterations` (default 3);
  ping-pong similarity machinery is overkill for concrete check signals.
- Judge model: `harness_config.critic` (`AgentConfig`) — the judge is a reviewer role.
- Final-failure status: `error` (so reruns retry by default; `--skip-errors` skips), with
  modified files restored via git so the tree stays clean for subsequent findings.

---

## Phase 1: Declared-checks convention (`quality_checks.py` — models, loader, matching)

### Overview
Define the `.quality-checks.toml` schema, its loader, auto-detection fallback, and
glob→profile matching. Pure logic, fully unit-testable, no harness changes yet.

### Changes Required:

#### 1. Models
**File**: `deep_architect/models/checks.py` (new)

```python
from __future__ import annotations

from pydantic import BaseModel, Field


class CheckProfile(BaseModel):
    """One glob-scoped group of programmatic check commands."""

    name: str
    paths: list[str]                      # gitwildmatch globs, repo-root-relative
    commands: list[str]                   # shell-less command strings; may contain {files}
    timeout: int = 120                    # per-command timeout in seconds


class LLMRulesConfig(BaseModel):
    """Where LLM-judged rules live."""

    source: str = ".opencodereview/rule.json"


class QualityChecksConfig(BaseModel):
    profiles: list[CheckProfile] = Field(default_factory=list)
    llm_rules: LLMRulesConfig | None = None
    auto_detected: bool = False           # True when built by fallback detection
```

TOML shape (documented in README + template):

```toml
[[profile]]
name = "backend-api"
paths = ["backend/fastapi/**"]
commands = [
  "ruff check backend/fastapi/src/",
  "black --check backend/fastapi/src/",
  "mypy backend/fastapi/src/",
  "uv run pytest backend/fastapi/tests/ -v",
]

[llm_rules]
source = ".opencodereview/rule.json"
```

`{files}` placeholder: a command containing `{files}` is expanded with the space-joined,
repo-relative modified files matching that profile (enables file-scoped commands; used by
auto-detection). Commands without it run as-is.

#### 2. Loader + auto-detection + matching
**File**: `deep_architect/quality_checks.py` (new)

```python
QUALITY_CHECKS_FILENAME = ".quality-checks.toml"

def load_quality_checks(repo_root: Path, override: Path | None = None) -> QualityChecksConfig:
    """Load .quality-checks.toml; fall back to auto-detection when absent."""

def autodetect_checks(repo_root: Path) -> QualityChecksConfig:
    """Build file-scoped profiles from pyproject.toml files (nice-to-have fallback)."""

def match_profiles(
    config: QualityChecksConfig, files: list[Path], repo_root: Path
) -> dict[str, list[Path]]:
    """Map profile name -> modified files matching its globs (pathspec gitwildmatch)."""
```

- `load_quality_checks`: parse with `tomllib`, validate via
  `QualityChecksConfig.model_validate`; malformed file → raise `ValueError` with a clear
  message (never swallow). Missing file → `autodetect_checks()`. An `[llm_rules]` section is
  honored even in auto-detect mode if `.opencodereview/rule.json` exists (auto-detect sets
  `llm_rules` when that file is present).
- `autodetect_checks`: walk for `pyproject.toml` (max depth 3; skip hidden dirs, `.venv`,
  `node_modules`, `build`, `dist`). For each, inspect `[tool.ruff]`/`[tool.mypy]`/
  `[tool.black]`/`[tool.bandit]` sections and dev-dependency lists; emit one profile per
  pyproject dir with **file-scoped** commands only, e.g.
  `["ruff check {files}", "mypy {files}"]`, `paths = ["<dir>/**"]` (root pyproject →
  `["**"]`). Never auto-detect pytest (test scope can't be inferred) — log
  `"auto-detected checks from <n> pyproject.toml file(s); tests not auto-detected — create
  .quality-checks.toml to declare them"`.
- `match_profiles`: `pathspec.PathSpec.from_lines("gitwildmatch", profile.paths)` per
  profile; a file may match multiple profiles; profiles with no matching file are skipped.

#### 3. Dependency
**File**: `pyproject.toml`
**Changes**: add `pathspec>=0.12` to `[project.dependencies]` (check latest stable on PyPI at
implementation time).

#### 4. Tests
**File**: `tests/test_quality_checks.py` (new)

- TOML round-trip: full file, minimal file, missing `llm_rules`, malformed TOML raises.
- Auto-detection against tmp trees: root-only pyproject; nested pyprojects (plant-tracking
  shape); pyproject with no tool sections → no commands; skip-dirs honored;
  `.opencodereview/rule.json` presence sets `llm_rules`.
- `match_profiles`: single/multi-profile matches, `**` semantics, file matching two profiles,
  no-match returns empty.
- Model round-trip serialization (mirrors `tests/test_files.py` style).

### Success Criteria:

#### Automated Verification:
- [x] Unit tests pass: `uv run python -m pytest tests/test_quality_checks.py -v`
- [x] Full suite passes: `uv run python -m pytest tests/ -v`
- [x] Lint passes: `uv run ruff check deep_architect/ tests/` (new files clean; 12 pre-existing
      errors in `review_action_harness.py`/`test_review_action_harness.py` predate this work —
      rewritten in Phase 4)
- [x] Type check passes: `uv run mypy deep_architect/quality_checks.py deep_architect/models/checks.py`
- [x] Security scan passes: `uv run bandit -r deep_architect/quality_checks.py deep_architect/models/checks.py -ll`

#### Manual Verification:
- [ ] `load_quality_checks(Path("~/repos/plant-tracking"))` in a REPL: finds 2 of the 3
      pyproject dirs (`backend/fastapi`, `packages/plant_service`) — the root `pyproject.toml`
      declares no `[tool.ruff]`/`[tool.mypy]`/`[tool.black]`/`[tool.bandit]` section and no
      matching dev dependency, so it correctly emits no profile (root checks live only in the
      Justfile, which this plan explicitly excludes from auto-detection — see "What We're NOT
      Doing"). `.opencodereview/rule.json` is picked up (`llm_rules.source` set). Pending human
      confirmation this is the expected behavior.

**Implementation Note**: After completing this phase and all automated verification passes,
pause here for manual confirmation from the human before proceeding to the next phase.

---

## Phase 2: Programmatic check runner with baseline diff

### Overview
Execute matched profiles' commands, normalize failures, and implement the pre-fix baseline so
only failures introduced by the fix block.

### Changes Required:

#### 1. Runner + baseline
**File**: `deep_architect/quality_checks.py` (extend)

```python
@dataclass
class CheckFailure:
    profile: str
    command: str
    returncode: int
    output: str          # tail of stdout+stderr, capped (e.g. last 4000 chars)
    pre_existing: bool   # True when present in baseline (advisory only)


@dataclass
class CheckBaseline:
    """Pre-fix snapshot: command -> (returncode, output)."""
    results: dict[str, tuple[int, str]]


def run_checks(
    matched: dict[str, list[Path]],
    config: QualityChecksConfig,
    repo_root: Path,
) -> list[CheckFailure]:
    """Run every command of every matched profile; return all failures."""

def capture_baseline(
    matched: dict[str, list[Path]],
    config: QualityChecksConfig,
    repo_root: Path,
) -> CheckBaseline:
    """Run the same commands pre-fix and record outcomes."""

def new_failures(
    failures: list[CheckFailure],
    baseline: CheckBaseline,
    modified_files: list[Path],
) -> list[CheckFailure]:
    """Return only failures introduced by the fix (baseline diff)."""
```

- Commands run via `subprocess.run(shlex.split(cmd), cwd=repo_root, capture_output=True,
  text=True, timeout=profile.timeout)` — `shell=False`. `{files}` expanded first with the
  profile's matched files (repo-relative). A command timeout or spawn error is a
  `CheckFailure` (fail-closed), logged via `logger.error`, never swallowed.
- Baseline-diff rules in `new_failures()`:
  1. Command failed post-fix, **passed** in baseline → new failure (blocks).
  2. Command failed post-fix **and** in baseline → blocks only if the post-fix output
     contains lines mentioning any modified file path that are absent from the baseline
     output; otherwise marked `pre_existing=True` and logged as a warning (advisory).
  3. A profile matched post-fix but absent from the baseline (agent modified an unexpected
     file in a different profile) → strict fail-closed: any failure blocks, with a warning
     explaining why.
- Bandit note: the `subprocess` usage will need targeted `# nosec B603` (or documented
  justification) — commands come from the repo's own declared config, equivalent trust level
  to a Justfile. Verify `uv run bandit -r deep_architect/ -ll` stays clean.

#### 2. Tests
**File**: `tests/test_quality_checks.py` (extend)

- `run_checks` with real trivial commands (`python -c "exit(0)"` / `exit(1)"`) in tmp dirs:
  pass, fail, timeout, `{files}` expansion, output capping.
- `new_failures`: all three baseline-diff rules, including the modified-file-line refinement
  (baseline output vs post-fix output fixtures) and the missing-baseline-profile strict path.
- `capture_baseline` round-trip.

### Success Criteria:

#### Automated Verification:
- [x] Unit tests pass: `uv run python -m pytest tests/test_quality_checks.py -v` (35 passed)
- [x] Full gate passes (new/changed files): ruff, mypy, bandit all clean on `quality_checks.py`
      / `models/checks.py`; `uv run python -m pytest tests/ -v` — 368 passed. (The 12
      pre-existing ruff errors in `review_action_harness.py`/`test_review_action_harness.py`
      predate this work and are addressed in Phase 4.)

#### Manual Verification:
- [x] Scratch demo (two-file `ruff check {files}` profile): baseline captured with one
      pre-existing unused-import violation in `pre_existing.py`; (1) introducing a new
      violation in `target.py` correctly blocks (new_failures returns it, output shows both
      files' violations since ruff scans them in one invocation, but the new lines mentioning
      `target.py` trigger the block); (2) fixing `target.py` while leaving the unrelated
      pre-existing violation untouched correctly returns zero blocking failures and marks the
      single CheckFailure `pre_existing=True`.

**Implementation Note**: Pause for manual confirmation before Phase 3.

---

## Phase 3: LLM-judged style rules (`llm_judge.py`)

### Overview
Load `.opencodereview` rules, judge each modified Python file's diff against matching rules
via one structured LLM call, and gate on MUST/SHOULD violations.

### Changes Required:

#### 1. Models
**File**: `deep_architect/models/checks.py` (extend)

```python
from typing import Literal


class StyleViolation(BaseModel):
    rule_id: str                                    # e.g. "PY-STY-017"; "GENERAL" if uncited
    severity: Literal["MUST", "SHOULD", "MAY", "NIT"]
    description: str
    line: int | None = None


class StyleVerdict(BaseModel):
    violations: list[StyleViolation] = Field(default_factory=list)

    @property
    def blocking(self) -> list[StyleViolation]:
        return [v for v in self.violations if v.severity in ("MUST", "SHOULD")]
```

#### 2. Rule loading + judgment
**File**: `deep_architect/llm_judge.py` (new)

```python
@dataclass
class RuleEntry:
    path_glob: str
    rule_text: str


def load_llm_rules(repo_root: Path, config: QualityChecksConfig) -> list[RuleEntry]:
    """Load rule.json entries; fall back to rules/*.md mapped to **/*.py; [] if neither."""

def rules_for_file(rules: list[RuleEntry], file: Path, repo_root: Path) -> list[RuleEntry]:
    """pathspec gitwildmatch match of a repo-relative file against rule globs."""

async def judge_file(
    file: Path,
    diff: str,
    rules: list[RuleEntry],
    agent_config: AgentConfig,           # harness_config.critic
    repo_root: Path,
) -> StyleVerdict:
    """One run_simple_structured call judging the diff against the concatenated rules."""
```

- `load_llm_rules`: read `config.llm_rules.source` (default `.opencodereview/rule.json`) as
  the JSON list of `{"path", "rule"}`. If missing, glob
  `.opencodereview/rules/**/*.md` and map each to `**/*.py`. If neither exists → `[]`
  (LLM checks silently absent — nothing declared, nothing to enforce; log at INFO).
  Malformed JSON → raise `ValueError` (never swallow).
- `judge_file` prompt contract (system prompt as a module constant or
  `deep_architect/prompts/llm_judge_system.md` following the prompts convention —
  implementer's choice; if a prompt file is added, register it in `EXPECTED_PROMPTS` in
  `tests/test_prompts.py`):
  - Judge **only the changed lines/regions shown in the diff** (full file content included
    for context, truncated to a sane cap, e.g. 2000 lines).
  - **Programmatic tool config wins on overlap**: "If a rule conflicts with the repo's
    configured linter/formatter behavior (e.g. line length), do NOT flag it — ruff/black
    configuration is authoritative for anything they check."
  - Cite exact rule IDs; assign the rule's own severity; output JSON matching `StyleVerdict`.
- Judgment call: `run_simple_structured(agent_config, system_prompt, prompt, StyleVerdict,
  label=f"llm-judge:{file.name}")` (`agents/client.py:143`) — inherits retries and RunStats
  cost accumulation. Only `.py` files are judged (matches rule globs; non-Python modified
  files skip the judge).
- Diff obtained via GitPython: `repo.git.diff(None, "--", str(file))` (uncommitted changes,
  since the fix isn't committed until checks pass).

#### 3. Tests
**File**: `tests/test_llm_judge.py` (new)

- `load_llm_rules`: rule.json happy path, rules/*.md fallback (incl. nested subdirs like
  `python-secure-coding/`), neither present → `[]`, malformed JSON raises.
- `rules_for_file`: `**/*.py`, `**/main.py`, `**/tests/**/*.py` scoping against sample paths.
- `judge_file`: `run_simple_structured` mocked (`AsyncMock`) — verify prompt contains diff,
  rules, and the tool-config-wins instruction; verdict passthrough; `StyleVerdict.blocking`
  severity gating (MUST/SHOULD block, MAY/NIT don't).
- No live LLM calls (repo convention: no mocking-free LLM e2e in unit tests).

### Success Criteria:

#### Automated Verification:
- [x] Unit tests pass: `uv run python -m pytest tests/test_llm_judge.py -v` (16 passed)
- [x] Full gate passes (new/changed files): ruff, mypy, bandit clean on `llm_judge.py` /
      `models/checks.py`; `uv run python -m pytest tests/ -v` — 384 passed.
- [x] Discovered `~/repos/plant-tracking/.opencodereview/rule.json`'s real shape is
      `{"rules": [...]}` (a dict wrapper), not the bare list the plan assumed —
      `load_llm_rules` supports both. Verified against the real fixture: 8 rules load, glob
      scoping (`**/*.py`, `**/main.py`, `**/tests/**/*.py`) matches correctly.

#### Manual Verification:
- [ ] One live `judge_file` call (scratch script, litellm endpoint env set) against a
      plant-tracking file with a seeded violation (e.g. bare `except:`) returns a MUST/SHOULD
      violation citing a plausible rule ID; a clean diff returns no blocking violations.
      **Not runnable in this session — no `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` in this
      shell's environment.** Scratch script left at
      `/tmp/claude-1000/-home-gerald-repos-deep-architect/3b5458ab-3365-49b9-aebb-d4eacb46c91a/scratchpad/live_judge_check.py`
      for Gerald to run with credentials sourced.

**Implementation Note**: Pause for manual confirmation before Phase 4.

---

## Phase 4: Integration into the review-action fix loop

### Overview
Wire discovery, baseline, runner, and judge into `_process_single_finding()`; add the
check-feedback retry loop with its bounded exit; fail-closed on exhaustion; remove the
superseded `ValidationConfig`/`run_validation`.

### Changes Required:

#### 1. Config
**File**: `deep_architect/config.py`
**Changes**: add to `ThresholdConfig` (line 17):

```python
check_max_fix_iterations: int = 3   # post-fix quality-check retry cap (0 = checks report-only)
check_command_timeout: int = 120    # default per-command timeout (seconds)
```

Also update `.deep-architect.toml.template` with the new keys.

#### 2. CodingAgent protocol + implementations
**File**: `deep_architect/review_action_harness.py`
**Changes**: add to the `CodingAgent` protocol (line 95) and both agents:

```python
async def fix_check_failures(
    self,
    files: list[Path],
    failure_report: str,     # rendered programmatic failures + LLM violations
    context: str = "",       # original finding context
) -> bool:
    """Address quality-check failures introduced by a prior fix attempt."""
```

- `OpencodeAgent`: same subprocess flow as `apply_fix`, feedback file contains the failure
  report ("Your previous fix introduced these quality-check failures — fix them without
  reverting the intent of the original change: …"). Success = agent completed
  (`_parse_opencode_ndjson`); no `_file_reflects_fix` here — the check rerun IS the
  verification.
- `ClaudeSDKAgent`: same `make_agent_options`/`run_agent` flow as `apply_fix`, allowed tools
  `["Read", "Edit", "Write"]`, prompt = failure report + file list, `max_turns=MAX_TURNS`.
- Failure report rendering helper: `_render_failure_report(prog: list[CheckFailure],
  style: list[tuple[Path, StyleViolation]]) -> str` — command, exit code, capped output;
  rule ID, severity, description, line per violation.

#### 3. The check loop
**File**: `deep_architect/review_action_harness.py`
**Changes**: in `_process_single_finding()` (line 666), replace the
`run_validation(...)` block (lines 797-814) with:

```
# once per finding, before apply_fix:
checks_cfg  = load_quality_checks(repo_root)          # repo_root = Path.cwd()
pre_matched = match_profiles(checks_cfg, [finding.file_path], repo_root)
baseline    = capture_baseline(pre_matched, checks_cfg, repo_root)
rules       = load_llm_rules(repo_root, checks_cfg)

# after apply_fix succeeds:
for iteration in range(1, check_max_fix_iterations + 1):
    if _shutdown_requested: ... (interrupted path, as existing)
    modified = get_modified_files(repo)               # git status, uncommitted
    matched  = match_profiles(checks_cfg, modified, repo_root)
    failures = new_failures(run_checks(matched, checks_cfg, repo_root),
                            baseline, modified)
    style: list[tuple[Path, StyleViolation]] = []
    if not failures:                                  # LLM judge gated on clean programmatic
        for f in (m for m in modified if m.suffix == ".py"):
            verdict = asyncio.run(judge_file(f, diff_for(f), rules_for_file(rules, f, ...),
                                             critic_config, repo_root))
            style.extend((f, v) for v in verdict.blocking)
            # advisory MAY/NIT violations logged at INFO
    if not failures and not style:
        break                                          # clean — proceed to commit
    logger.info("Check iteration %d/%d: %d programmatic, %d style failure(s)", ...)
    report = _render_failure_report(failures, style)
    ok = asyncio.run(agent.fix_check_failures(modified, report, finding.analysis))
    if not ok: ...                                     # count as a failed iteration, continue
else:
    git_restore_files(repo, modified)                  # fail-closed: discard the dirty fix
    write_action_taken(md_file, FindingStatus(status="error", ...,
        summary=f"Quality checks failed after {check_max_fix_iterations} iteration(s)",
        error_message=<capped failure report>))
    return ("error", False, ...)
# commit path unchanged (existing code from line 817)
```

- Loop structure notes:
  - `check_max_fix_iterations = 0` → run checks once, log results, but don't block or retry
    (report-only escape hatch); document in README.
  - Empty config (no profiles matched, no rules) → loop trivially passes; behavior identical
    to today minus the hardcoded ruff/mypy (log INFO that no checks were discovered).
  - Baseline is captured **before** `apply_fix` for profiles matching `finding.file_path`;
    profiles first matched by unexpected extra modified files use the strict path from
    Phase 2 rule 3.
- New helper in **`deep_architect/git_ops.py`**: `git_restore_files(repo, files)` —
  `repo.git.checkout("--", *paths)` (+ handle untracked new files via clean or unlink,
  logged). Unit-tested in `tests/test_git_ops.py` with real tmp repos (existing pattern).
- Remove `ValidationConfig` (line 70), `run_validation` (line 631), and the hardcoded
  `validation_config` wiring in `main()` (line 1297); update all call sites and drop
  `TestValidationConfig` + `test_validation_failure_skips_commit` in favor of new tests.
  (Orphan removal only — these are superseded by this change.)

#### 4. CLI wiring
**File**: `deep_architect/review_action_harness.py` (`parse_args`, line 1158; `main`, line 1250)

- `--max-check-iterations N` — overrides `thresholds.check_max_fix_iterations`.
- `--skip-llm-checks` — programmatic checks only (cost escape hatch).
- `--quality-checks PATH` — explicit config file override (useful for testing).
- `main()` passes `harness_config` (thresholds + critic AgentConfig) down to
  `process_findings` → `_process_single_finding`.

#### 5. Tests
**File**: `tests/test_review_action_harness.py` (extend/modify)

Following the existing mock conventions (agents stubbed with `AsyncMock`, subprocess patched
at `deep_architect.review_action_harness.subprocess.run`; new modules patched at
`deep_architect.quality_checks.*` / `deep_architect.llm_judge.*`):

- Clean first iteration → commit (no `fix_check_failures` call).
- Programmatic failure → `fix_check_failures` called with a report containing the command
  output → clean second iteration → commit.
- Style-only failure (programmatic clean) → judge invoked → retry → commit.
- Persistent failure → exactly `check_max_fix_iterations` iterations → files restored →
  status `error` written → no commit.
- Pre-existing (baseline) failure alone → does NOT block → commit; logged as warning.
- `--skip-llm-checks` → judge never invoked.
- `check_max_fix_iterations = 0` → checks run, never block.
- SIGINT during check loop → `interrupted` status (existing convention).
- `fix_check_failures` unit tests for both `OpencodeAgent` and `ClaudeSDKAgent` (mocked
  subprocess / client), plus protocol-satisfaction update in `TestCodingAgentProtocol`.

### Success Criteria:

#### Automated Verification:
- [x] Unit tests pass: `uv run python -m pytest tests/test_review_action_harness.py tests/test_git_ops.py -v` (70 + 27 passed)
- [x] Full gate passes: `uv run ruff check deep_architect/ tests/ && uv run mypy deep_architect/ && uv run python -m pytest tests/ -v && uv run bandit -r deep_architect/ -ll` — 399 tests passed, ruff/mypy/bandit all clean.
  - Fixed 10 pre-existing ruff line-length errors in `review_action_harness.py` while
    rewriting the file (mechanical line-wraps only, no logic change).
  - Also fixed `deep_architect/review_action.py`, a thin re-export shim that still imported
    the now-removed `ValidationConfig` (mypy caught it; no other consumers found via grep).

#### Manual Verification:
- [x] `review-action test_feedback --dry-run --verbose` runs cleanly end-to-end (existing
      finding is REJECTED-verdict so it's skipped, but confirms the CLI/new flags don't
      break the dry-run path).
- [x] Real single-finding run against a scratch git repo (`.quality-checks.toml` with a real
      `ruff check {files}` profile): agent introduces an unused import, `fix_check_failures`
      is invoked with the exact ruff output, agent never fixes it, loop exhausts at
      `check_max_fix_iterations=2`, `target.py` is restored to its committed content via
      `git_restore_files`, status is `error`, no commit is made, working tree left clean.

**Implementation Note**: Pause for manual confirmation before Phase 5.

---

## Phase 5: Dogfood, docs, and end-to-end validation

### Overview
Declare deep-architect's own checks, document the convention, and validate end-to-end against
plant-tracking's three subproject profiles.

### Changes Required:

#### 1. Dogfood config
**File**: `.quality-checks.toml` (new, repo root of deep-architect)

```toml
[[profile]]
name = "deep-architect"
paths = ["deep_architect/**", "tests/**"]
commands = [
  "uv run ruff check deep_architect/ tests/",
  "uv run mypy deep_architect/",
  "uv run python -m pytest tests/ -v",
  "uv run bandit -r deep_architect/ -ll",
]
```

#### 2. Documentation
**Files**: `README.md`, `CLAUDE.md`

- README: new "Quality checks" section under review-action — the `.quality-checks.toml`
  format, `{files}` placeholder, auto-detection fallback and its limits (no pytest, no
  Justfile parsing), LLM rules discovery (`rule.json` → `rules/*.md` → none), baseline-diff
  semantics, loop bounds and new CLI flags, fail-closed behavior. Note the convention is
  agent-agnostic: any coding agent can read the same file (a repo can reference it from its
  CLAUDE.md/AGENTS.md).
- CLAUDE.md: brief pointer in the review-action notes (config keys + where the logic lives).
- Add `.quality-checks.toml.template` documenting all fields (mirrors the existing
  `.deep-architect.toml.template` convention).

#### 3. plant-tracking validation config
**File**: `~/repos/plant-tracking/.quality-checks.toml` (new, in the reference repo —
written during manual verification, not committed to deep-architect)

Three profiles mirroring the Justfile expansion (root CLI incl. bandit skips, backend
api-check set, plant_service ad-hoc commands) + `[llm_rules] source = ".opencodereview/rule.json"`.

### Success Criteria:

#### Automated Verification:
- [x] Full gate passes: `uv run ruff check deep_architect/ tests/ && uv run mypy deep_architect/ && uv run python -m pytest tests/ -v && uv run bandit -r deep_architect/ -ll` — 401 tests passed, ruff/mypy/bandit clean.
- [x] `load_quality_checks(Path("."))` on deep-architect returns the dogfood profile
      (`tests/test_quality_checks.py::TestDogfoodConfig::test_load_quality_checks_returns_dogfood_profile`),
      and the non-pytest dogfood commands (ruff/mypy/bandit) pass for real through `run_checks`
      (`test_dogfood_non_test_commands_pass_for_real`). The `pytest tests/ -v` command is
      deliberately excluded from the subprocess-execution test — running it for real from
      inside the test suite would spawn a nested `pytest tests/` that re-runs this same test,
      which spawns another, unboundedly. Verified separately via a one-off scratch script
      (not committed) that the full dogfood profile including pytest passes.

#### Manual Verification:
- [ ] Run `review-action` against plant-tracking with seeded findings touching all three
      subprojects (`commands/`, `backend/fastapi/src/`, `packages/plant_service/src/`) and
      observe: correct profile selection per file, baseline exclusion of any pre-existing
      failures, LLM judge invoked with the right rule sets, retry loop on an induced failure,
      fail-closed `error` + file restore when the cap is hit.
      **Not run in this session** — needs a real coding agent (opencode binary or
      `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`), neither available here (same constraint as
      the Phase 3 live-LLM check). Profile *selection* was verified for real (see note below).
- [ ] Cost/latency of a full plant-tracking run is acceptable (RunStats summary shows judge
      call costs). Blocked on the same missing credentials.

**What I verified instead**: `.quality-checks.toml` for plant-tracking (mirroring the
Justfile's `check`/`api-check`/`plant_service` split, matching the ticket's reference
description) was written to the scratchpad — auto-mode's permission classifier blocked writing
directly into `~/repos/plant-tracking` as out-of-scope modification of another repo. Loaded it
via `load_quality_checks(pt_root, override=scratch_path)` and confirmed `match_profiles()`
correctly routes one file from each of the three subprojects to its own profile. Gerald can
copy the scratchpad file into `~/repos/plant-tracking/.quality-checks.toml` to run the full
manual verification himself.

---

## Testing Strategy

### Unit Tests:
- `tests/test_quality_checks.py`: TOML parsing, auto-detection heuristics, glob matching,
  `{files}` expansion, runner (real trivial subprocesses), baseline-diff rules 1–3,
  timeout/error paths.
- `tests/test_llm_judge.py`: rule loading (json/md-fallback/none/malformed), glob scoping,
  prompt assembly assertions, severity gating; LLM calls mocked.
- `tests/test_review_action_harness.py`: loop behavior matrix (clean / prog-fail / style-fail
  / persistent-fail / pre-existing-only / skip-flags / cap=0 / SIGINT), new agent methods,
  status persistence.
- `tests/test_git_ops.py`: `git_restore_files` with real tmp repos (tracked modified +
  untracked new file).

### Key edge cases:
- File matching multiple profiles (commands from both run, deduped by command string).
- Agent modifies a file outside any profile (no programmatic checks; still LLM-judged if a
  rule glob matches).
- Baseline command timeout (treated as baseline failure — post-fix same timeout doesn't block).
- `rule.json` present but `[llm_rules]` absent in auto-detect mode (still picked up).
- Non-`.py` modified file (skips judge).

### Manual Testing Steps:
1. Phase-gated manual checks listed per phase above.
2. Final: plant-tracking end-to-end (Phase 5 manual criteria) with `-v` logging, reviewing
   the per-iteration log lines and the `## Action Taken` blocks written to finding files.
3. Dogfood: run review-action on deep-architect itself with a trivial seeded finding and
   confirm the dogfood profile gates it.

## Performance Considerations

- Baseline capture runs each matched profile's full command list once per finding — with
  pytest in a profile this can dominate runtime for multi-finding runs. Accepted for v1; a
  future optimization is caching baselines per (profile, HEAD sha) since the tree is clean
  between findings. Log per-command durations to make the cost visible.
- LLM judge cost is bounded by gating behind clean programmatic checks and one call per
  modified file per iteration; rule texts can be large (~60k tokens for plant-tracking's full
  set) — RunStats surfaces the spend in the run summary.

## Migration Notes

- `ValidationConfig`/`run_validation` are removed; no external consumers exist (module-local
  only). Users relying on the implicit hardcoded `ruff check`/`mypy` get equivalent-or-better
  behavior from auto-detection; repos wanting exact control add `.quality-checks.toml`.
- New config keys have defaults — existing `~/.deep-architect.toml` files keep working.
- `deep_architect/review_action_harness.py.backup` / `.clean` are stale pre-refactor
  snapshots (verified byte-identical to each other, strictly behind the tracked file) — not
  touched by this plan; flag for deletion separately.

## References

- Original ticket: `knowledge/tickets/PROJ-0013.md`
- Predecessor plan: `knowledge/plans/2026-06-28-PROJ-0012-review-action-harness.md`
- Hook point: `deep_architect/review_action_harness.py:666` (`_process_single_finding`),
  `:631` (`run_validation`), `:1297` (hardcoded commands)
- Structured LLM call: `deep_architect/agents/client.py:143` (`run_simple_structured`)
- Thresholds: `deep_architect/config.py:17` (`ThresholdConfig`)
- ADR-005 (two LLM call patterns), ADR-012 (structured output), ADR-015 (retry layers),
  ADR-019 (severity blocking / fail-closed precedent)
- Reference convention: `~/repos/plant-tracking/Justfile`,
  `~/repos/plant-tracking/.opencodereview/generate-rules.py`,
  `~/repos/plant-tracking/.opencodereview/rule.json`
