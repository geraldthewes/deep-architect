# deep-architect

`deep-architect` produces a complete, peer-reviewed C4 architecture document — automatically, without human intervention. It works in two modes:

- **Greenfield** (`--prd`): takes a BMAD PRD and designs the architecture from scratch.
- **Reverse-engineer** (`--codebase`): reads an existing git repository and documents what is actually there.

It runs two Claude Code agents against each other: a **Generator** (Winston, the architect) who writes the architecture using real file tools, and a **Critic** (Boris, a hostile senior architect) who reads and tears it apart. They negotiate acceptance criteria, argue through rounds of feedback, and only stop when the architecture passes a rigorous quality bar. The result lands in `knowledge/architecture/` as a folder of Markdown + Mermaid files, ready for your development agents.

```
PRD        → adversarial-architect → knowledge/architecture/ (C4 diagrams + ADRs)
codebase/  → adversarial-architect → codebase/knowledge/architecture/
```

The agents are powered by the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) — the generator writes files directly using the `Write` and `Edit` tools, and the critic inspects them using `Read`, `Glob`, and `Grep`. This is not a prompt chain; these are real agentic loops.

---

## Prerequisites

- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**
- **Claude Code CLI** (`claude`) installed and accessible in your `PATH`
  ```bash
  claude --version
  ```
- **A Claude-compatible LLM endpoint** — either Anthropic's API directly, or a compatible proxy (e.g. LiteLLM in front of your cluster)
- **A git repository** — the tool auto-commits architecture files after each generator pass

---

## Installation

```bash
git clone https://github.com/geraldthewes/deep-architect
cd deep-architect
just install    # or: uv sync && uv tool install --editable .
```

`just install` does two things:
- `uv sync` — sets up `deep-architect/.venv` for local development (tests, lint, type check)
- `uv tool install --editable .` — installs `adversarial-architect`, `review-analyzer`, and `review-action` into `~/.local/bin` (via `uv`'s isolated tool environments), so they're on your `PATH` in **any** terminal, in **any** repository. This matters because `review-action` and `review-analyzer` are meant to be run from inside the repo you're applying fixes to, not from inside `deep-architect` itself.

`--editable` means source edits in `deep-architect/` are picked up immediately, with no reinstall needed. If you only ran `uv sync` (no `uv tool install`), the commands only exist inside `deep-architect/.venv/bin/` and must be invoked with `uv run <command>` from inside this directory — running them bare from another repo will fail with `command not found`.

Verify the install:

```bash
adversarial-architect --help
review-analyzer --help
review-action --help
```

---

## Configuration

### Step 1: Set environment variables

The tool picks up endpoint and auth configuration from environment variables — the same ones the `claude` CLI uses. Add these to your shell profile:

```bash
export ANTHROPIC_BASE_URL=http://your-llm-proxy:9999
export ANTHROPIC_AUTH_TOKEN=your-api-key          # used as Authorization: Bearer <token>
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-standard
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-standard
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-standard
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1  # disable telemetry/autoupdate
```

If you're using Anthropic's API directly, just set:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

The model aliases (`ANTHROPIC_DEFAULT_SONNET_MODEL`, etc.) tell the `claude` CLI what actual model ID to use when the config says `"sonnet"`, `"opus"`, or `"haiku"`. If you're using Anthropic's API without a proxy, these can be left unset and the defaults apply.

### Step 2: Create `~/.config/deep-architect/config.toml`

```bash
mkdir -p ~/.config/deep-architect
cp config.toml.template ~/.config/deep-architect/config.toml
```

(The legacy `~/.deep-architect.toml` path is still honored if that's the only config file present, but new setups should use the XDG path above.)

The TOML config controls which model alias to use for each agent and the quality thresholds:

```toml
[generator]
model             = "sonnet"    # resolves via ANTHROPIC_DEFAULT_SONNET_MODEL
max_turns         = 50          # max agentic tool-use steps per generator invocation
max_agent_retries = 2           # retry agent on CLI crash (e.g. disallowed tool call)
# context_window  = 200000      # optional: log context utilisation % per tool call

[critic]
model             = "sonnet"
max_turns         = 30
max_agent_retries = 2
# context_window  = 200000

[thresholds]
min_score                      = 9.0   # critic score required to pass (out of 10)
consecutive_passing_rounds     = 2     # must pass this many rounds in a row
max_rounds_per_sprint          = 6     # give up on a sprint after this many rounds
max_total_rounds               = 40    # hard limit across all sprints
timeout_hours                  = 3.0   # wall-clock timeout
ping_pong_similarity_threshold = 0.85  # auto-exit if feedback stops changing
max_round_retries              = 2     # retry a full generator+critic round on failure
```

You can use `"opus"` for a more capable (but slower/costlier) generator. For the critic, `"sonnet"` is usually sufficient.

#### Using a custom `claude` binary path

If `ANTHROPIC_BASE_URL` isn't being picked up (this can happen with some SDK installations), pin the CLI path explicitly:

```toml
cli_path = "/home/your-user/.local/bin/claude"
```

---

## Running

**Greenfield mode** — from inside a BMAD repo that has a PRD:

```bash
adversarial-architect --prd knowledge/prd.md --output knowledge/architecture
```

**Reverse-engineer mode** — from an existing git repository (no PRD needed):

```bash
# Output defaults to <repo>/knowledge/architecture/
adversarial-architect --codebase /path/to/existing-repo

# Explicit output override
adversarial-architect --codebase /path/to/existing-repo --output /custom/output/dir
```

> **Note:** `--prd` and `--codebase` are mutually exclusive. Both modes require the output directory to be inside a git repository (used for commit tracking).

> **Note:** Do not use `uv run adversarial-architect` when inside another project that has its own `pyproject.toml` — uv will try to build that project first and fail. Run the binary directly instead.

That's it. By default the tool stops after each sprint so you can review the output; pass `--yolo` to run all 7 sprints unattended. When the run finishes, `knowledge/architecture/` contains your architecture.

### All CLI Options

| Flag | Description |
|------|-------------|
| `--prd PATH` | Path to the PRD Markdown file (greenfield mode) |
| `--codebase PATH` | Path to the git repository to analyze (reverse-engineer mode) |
| `--output PATH` | Output directory (default: `<codebase>/knowledge/architecture/` in RE mode) |
| `--resume` | Resume an interrupted run from the last completed sprint |
| `--config PATH` | Config file path (default: `~/.config/deep-architect/config.toml`, falls back to legacy `~/.deep-architect.toml`) |
| `--model-generator TEXT` | Override the generator model alias for this run |
| `--model-critic TEXT` | Override the critic model alias for this run |
| `--context PATH` | Supplementary context file injected into every generator prompt (repeatable) |
| `--reset-sprint N` | Reset sprint N to its initial state and resume from it; deletes sprint N's contract, feedback, and history entries |
| `--strict` | Halt the run when a sprint cannot meet exit criteria, instead of accepting the best-effort result and continuing |
| `--yolo` | Run all sprints unattended with no pause between sprints (default: stop after every sprint for review) |

### Overriding Models Per-Run

```bash
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --model-generator opus \
  --model-critic sonnet
```

### Injecting Supplementary Context

Pass one or more `--context` files to inject additional material (e.g. an existing tech-stack doc, a security policy, or a prior architecture) into every generator prompt:

```bash
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --context knowledge/tech-stack.md \
  --context knowledge/security-policy.md
```

### Unattended vs sprint-by-sprint mode

By default, `adversarial-architect` stops after each of the 7 sprints, prints the
files it wrote, and exits so you can review (and optionally edit) the output.
Re-run the same command with `--resume` to continue with the next sprint.

Pass `--yolo` to skip the pauses and run the whole harness unattended:

```bash
adversarial-architect --prd knowledge/prd.md --output knowledge/architecture            # default: stop between sprints
adversarial-architect --prd knowledge/prd.md --output knowledge/architecture --yolo     # unattended
```

`--yolo` is per-invocation: combining it with `--resume` (`--resume --yolo`) runs
the remaining sprints unattended starting from the next sprint.

### Strict Mode

By default, when a sprint exhausts its maximum rounds without fully meeting exit criteria, the harness accepts the best-effort result and moves on. Use `--strict` to halt instead:

```bash
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --strict
```

---

## What Happens During a Run

The tool works through **7 sprints**, each producing a specific part of the architecture. For each sprint:

1. **Contract negotiation** — Generator proposes specific, testable acceptance criteria. Critic tightens them (adds edge cases, raises standards).
2. **Generator writes** — A Claude Code agent with `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep` tools creates Markdown files with C4 Mermaid diagrams and narrative directly on disk.
3. **Critic scores** — A separate Claude Code agent with `Read`, `Glob`, `Grep` tools inspects the files, evaluates every criterion 1–10, flags severity (Critical / High / Medium / Low), and gives detailed feedback with file references.
4. **Loop** — Generator revises based on feedback. Repeats until the average score reaches 9.0/10 with no Critical or High issues, held for 2 consecutive rounds.
5. **Auto-commit** — Each generator pass is committed to git with a message like `Generator pass 2 - sprint 1 (C1 System Context)`.

After all 7 sprints, both agents independently review the complete architecture and must output `READY_TO_SHIP` before the run is declared complete.

### Sprints

| # | Sprint | What Gets Produced | Output Files |
|---|--------|--------------------|----|
| 1 | C1 System Context | Top-level diagram: users, system boundary, external dependencies | `c1-context.md` |
| 2 | C2 Container Overview | All major containers, tech stack decisions | `c2-container.md` |
| 3 | Frontend Container | Frontend architecture, auth flows, routing, state management | `frontend/c2-container.md` (+ area docs) |
| 4 | Backend / Orchestration | API design, business logic, LLM orchestration, workers | `backend/c2-container.md` (+ area docs) |
| 5 | Database + Knowledge Base | Data stores, vector store, caching, schema overview | `database/c2-container.md` (+ area docs) |
| 6 | Edge / Deployment / Observability | Edge functions, deployment topology, auth, scaling, monitoring | `edge-functions/c2-container.md`, `deployment.md` |
| 7 | ADRs + Cross-Cutting Concerns | Architecture Decision Records, NFRs, security, compliance | `decisions/` (+ area docs) |

### Console Output

```
============================================================
SPRINT 1/7: C1 System Context
============================================================
[Sprint 1] Negotiating contract...
[Sprint 1] Round 1
[Sprint 1] Round 1: avg=7.2 passed=False
[Sprint 1] Round 2
[Sprint 1] Round 2: avg=8.8 passed=False
[Sprint 1] Round 3
[Sprint 1] Round 3: avg=9.3 passed=True
Consecutive passes: 1/2
[Sprint 1] Round 4
[Sprint 1] Round 4: avg=9.4 passed=True
Consecutive passes: 2/2
Sprint 1 PASSED
```

Logs are also written to `<output-dir>/logs/architect-run-YYYYMMDD-HHMMSS.log`.

---

## Output Structure

```
knowledge/architecture/
├── c1-context.md              # C4 Level 1: System Context diagram
├── c2-container.md            # C4 Level 2: Container overview + tech stack
├── frontend/
│   └── c2-container.md        # Frontend container detail
├── backend/
│   └── c2-container.md        # Backend/orchestration detail
├── database/
│   └── c2-container.md        # Data layer detail
├── edge-functions/
│   └── c2-container.md        # Edge/CDN/auth detail
├── deployment.md              # Deployment architecture
├── decisions/
│   ├── ADR-001.md             # e.g. "Database Choice: PostgreSQL"
│   ├── ADR-002.md             # e.g. "Auth: JWT with OIDC"
│   └── cross-cutting.md       # Security, performance, reliability NFRs
│
├── contracts/                 # Sprint contracts (JSON) — for inspection
├── feedback/                  # Critic feedback per round (JSON) — for debugging
└── logs/                      # Full run logs

.checkpoints/                  # At the git repo root (not inside knowledge/)
└── progress.json              # Run state — used by --resume and --reset-sprint
```

The Mermaid diagrams in these files render natively on GitHub.

---

## Resuming an Interrupted Run

If the run is interrupted (crash, timeout, Ctrl+C), the harness detects a prior checkpoint automatically on the next invocation and asks whether to continue:

```
Prior run detected: sprint 3/7, 2 sprint(s) completed, 11 round(s) run, status=running

Continue from where it left off? [Y/n]:
```

Answering **Y** resumes seamlessly. Answering **N** prompts you to confirm a clean start, which removes prior checkpoints, contracts, and feedback files.

You can also pass `--resume` explicitly to skip the prompt and always resume:

```bash
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --resume
```

Already-committed architecture files are left untouched.

### Resetting a Specific Sprint

To re-run a single sprint without discarding the rest of the run, use `--reset-sprint N`:

```bash
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --reset-sprint 3
```

This deletes sprint 3's contract, feedback files, and history entries, then automatically resumes from sprint 3. Sprints above 3 are untouched.

---

## Inspecting Results

### Critic Feedback

To understand why a sprint took many rounds:

```bash
cat knowledge/architecture/feedback/sprint-1-round-1.json
```

Each feedback file contains per-criterion scores, severity ratings, and specific comments with file references.

### Git History

Each generator pass is a separate commit:

```bash
git log --oneline knowledge/architecture/
```

```
a3f2c1e Generator pass 2 - sprint 3 (Frontend Container)
b9e4d2f Generator pass 1 - sprint 3 (Frontend Container)
c7a1e0b Generator pass 3 - sprint 2 (C2 Container Overview)
...
```

### Sprint Contracts

To see what acceptance criteria were negotiated for a sprint:

```bash
cat knowledge/architecture/contracts/sprint-1.json
```

---

## Troubleshooting

**`claude CLI not found in PATH`**
Install Claude Code: follow the instructions at [claude.ai/code](https://claude.ai/code). Then verify with `claude --version`.

**`Config file not found`**
Create `~/.config/deep-architect/config.toml` from the template: `mkdir -p ~/.config/deep-architect && cp config.toml.template ~/.config/deep-architect/config.toml`. The legacy `~/.deep-architect.toml` path also still works.

**`Error: PRD file not found`**
The `--prd` path must point to an existing file. Check the path is correct relative to your current directory.

**`Error: Codebase directory not found`**
The `--codebase` path must be an existing directory. Pass an absolute path to avoid ambiguity.

**`Either --prd (greenfield) or --codebase (reverse-engineer) is required`**
One of the two mode flags must be provided. They are mutually exclusive — don't pass both.

**`not inside a git repository`**
The output directory must be inside a git repo. Initialize one first: `git init`.

**ANTHROPIC_BASE_URL not respected**
The SDK may fall back to the bundled Claude binary, which can ignore custom env vars. Set `cli_path` in `~/.config/deep-architect/config.toml` to the output of `which claude`.

**A sprint fails after max rounds**
By default the harness accepts the best-effort result and continues (soft-fail). Pass `--strict` to halt instead. Check the feedback JSON for recurring issues. Common causes: the PRD lacks enough detail, or `max_turns` is too low for the model to complete a full architecture file in one agent loop. Try increasing `max_turns` in the config.

**The agent crashes mid-run with "Command failed with exit code 1"**
The model called a disallowed tool (e.g. `TodoWrite`). The harness will automatically retry the round up to `max_round_retries` times (default 2), and each agent call is retried up to `max_agent_retries` times (default 2). If crashes persist, check the logs for the "unexpected tool call" warning to identify which tool is being hallucinated. Setting `context_window` in the config helps spot high-context turns where hallucinations become more likely.

**Run stops mid-way due to timeout**
Use `--resume` to continue. If 3 hours is too tight, increase it:
```toml
[thresholds]
timeout_hours = 6.0
```

**`review-analyzer: command not found`**
The command is installed inside the project's virtual environment. Use `uv run review-analyzer` or activate the venv first with `source .venv/bin/activate`.

**`opencode binary not found`**
`review-analyzer` needs `opencode` to make LLM calls. Set the `OPENCODE_BIN` environment variable to the full path of your `opencode` binary:
```bash
export OPENCODE_BIN=$(which opencode)
```

**`grok binary not found`**
`review-action --provider grok` needs the Grok Build CLI. Install it (`curl -fsSL https://x.ai/cli/install.sh | bash`), or if it's installed somewhere not on `PATH`, set `GROK_BIN` to the full path:
```bash
export GROK_BIN=$(which grok)
```

**`review-action` finding stuck in `error` status: "Quality checks failed after N iteration(s)"**
The coding agent couldn't satisfy the target repo's checks within `check_max_fix_iterations`
(default 3). Read the `ErrorMessage` in the finding's `## Action Taken` block — it includes the
capped check output. Common fixes: raise `--max-check-iterations`, use a more capable
`--model`/`--provider`, or fix the underlying issue manually and re-run without `--force` (the
finding stays `error` and is retried automatically).

**`review-action` LLM judge call fails or the run hangs on `llm-judge:<file>`**
The judge runs through the same `--provider` CLI as the fix agent, so credentials follow
whichever provider is selected: `--provider claude` needs `ANTHROPIC_BASE_URL`/
`ANTHROPIC_AUTH_TOKEN` (or `ANTHROPIC_API_KEY`) set, same as
[Configuration, Step 1](#step-1-set-environment-variables) — a missing key now fails immediately
with a clear error instead of retrying. `--provider opencode`/`--provider grok` use their own CLI
auth (opencode's own config; `grok login` or `XAI_API_KEY`) — no separate credentials needed. If
you just want the programmatic checks, pass `--skip-llm-checks`.

**`Malformed .quality-checks.toml` / `Invalid .quality-checks.toml`**
The file exists but failed TOML parsing or Pydantic validation. Compare against
`.quality-checks.toml.template` — common mistakes: using `[profile]` instead of `[[profile]]`
(profiles are a list), or a `commands`/`paths` value that isn't an array of strings.

---

## Tips for Best Results

- **Use a capable generator model.** The generator needs to produce complete, well-structured Markdown with valid Mermaid syntax in a single agentic loop. `opus` gives the best results; `sonnet` is a good cost/quality balance.
- **Use a different model for critic and generator** if your setup allows it. The adversarial dynamic works best when the agents have different "opinions."
- **Make your PRD specific (greenfield).** The generator reads the PRD for every sprint. Concrete technology choices and user journeys lead to better architecture. Vague PRDs produce vague diagrams.
- **Point at a well-documented repo (reverse-engineer).** The generator discovers the codebase via its tools each round. Repos with a README, clear entry points, and conventional structure (e.g. `pyproject.toml`, `package.json`, IaC configs) yield the most accurate diagrams. If the repo is large, consider adding a `--context` file that describes the top-level layout.
- **Check Sprint 1 before walking away.** After sprint 1 completes, glance at `c1-context.md`. If the system boundary is wrong, the rest of the architecture will follow the wrong path — better to catch it early.
- **Raise `max_turns` for complex systems.** If the generator is consistently cutting off before producing all required files, increase `max_turns` in the `[generator]` section.

---

## Review Analyzer

`review-analyzer` takes an OCR (Open Code Review) JSON file and uses an LLM to triage each finding, classifying it as `VALID`, `REJECTED`, or `BACKLOG` with detailed reasoning. This is useful when OCR produces a large volume of findings and you need a second LLM opinion to separate real issues from false positives.

### Usage

```bash
uv run review-analyzer <ocr-file.json> [options]
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--include <glob>` | (none) | Only process findings matching this path pattern (repeatable) |
| `--exclude <glob>` | (none) | Skip findings matching this path pattern (repeatable) |
| `--model <name>` | `standard/coder` | LLM model for analysis |
| `--concurrency <n>` | `5` | Maximum parallel LLM requests |
| `--output-dir <dir>` | `feedback/` | Directory for per-finding Markdown reports |
| `--summary-only` | off | Print summary counts without writing individual files |

### Example

```bash
uv run review-analyzer code-review.json \
    --exclude '.agents/*' --exclude '.claude/*' \
    --model standard/coder \
    --output-dir feedback/
```

### Output

The tool writes one Markdown file per finding (`{filepath_hash}-{index}.md`) to the output directory, plus a `SUMMARY.md` with verdict counts and percentages.

### Configuration

`review-analyzer` invokes `opencode run` under the hood for its LLM calls. By default it expects the binary at `/home/gerald/.opencode/bin/opencode`. Override with the `OPENCODE_BIN` environment variable:

```bash
export OPENCODE_BIN=/path/to/your/opencode
```

---

## Review Action Harness

`review-action` consumes the Markdown output of `review-analyzer` and automatically applies the suggested fixes for `VALID` findings. It processes each finding sequentially through a coding agent, validates the changes, and creates an atomic git commit.

### Usage

```bash
uv run review-action <output-dir> [options]
```

Defaults to `feedback/` if no directory is specified.

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--model <name>` | (from config) | Model for the coding agent |
| `--provider <name>` | `opencode` | Agent provider (`opencode`, `claude`, or `grok`) |
| `--dry-run` | off | Show what would be done without making changes |
| `--verbose` | off | Enable verbose logging |
| `--config <path>` | `~/.config/deep-architect/config.toml` (legacy `~/.deep-architect.toml` fallback) | Configuration file path |
| `--force` | off | Re-process findings that were already completed |
| `--skip-errors` | off | Skip findings that previously failed instead of retrying them |
| `--max-check-iterations <n>` | (from config) | Post-fix quality-check retry cap; `0` = run checks but never block or retry |
| `--skip-llm-checks` | off | Run programmatic quality checks only, skip the LLM style-rule judge |
| `--quality-checks <path>` | (auto-discovered) | Explicit path to a `.quality-checks.toml` file |

### Example

```bash
# Apply fixes from review-analyzer output
uv run review-action feedback/

# Dry run to preview changes
uv run review-action feedback/ --dry-run

# Use Claude SDK instead of opencode
uv run review-action feedback/ --provider claude --model sonnet

# Use Grok Build (xAI) instead of opencode
uv run review-action feedback/ --provider grok --model grok-build
```

### Workflow

The typical pipeline is:

```bash
# 1. Generate OCR findings
opencode --ocr code-review.json

# 2. Analyze and triage findings
uv run review-analyzer code-review.json --output-dir feedback/

# 3. Apply VALID fixes automatically
uv run review-action feedback/
```

### Output

`review-action` prints a summary of items processed, committed, skipped, and errors, and
writes the same counters plus a per-finding table to `<output-dir>/review-action_summary.md`
(updated after every finding, so a crash mid-run still leaves an accurate record):

```
# Review Action Summary

Restored:   0
Processed:  2
Committed:  1
Skipped:    1
Errors:     0
Interrupted: no
Progress: 2 out of 2 findings processed

## Findings

| Finding | File | Outcome | Commit | What was done |
|---------|------|---------|--------|---------------|
| [abc12345-0](./abc12345-0.md) | src/example.py | Fixed | `85ef21e1` | Fix applied and committed: fix: Rename variable for clarity... [abc12345-0] |
| [def67890-0](./def67890-0.md) | src/other.py | Rejected (BACKLOG) | — | Verdict BACKLOG — not actioned |
```

Each successful fix is committed atomically, with a subject line, the full review comment,
and trailers identifying the commit as review-action's and naming the source finding file —
so `git log` alone traces a commit back to its review without cross-referencing the output
directory:

```
fix: Rename variable for clarity... [abc12345-0]

Rename `x` to `total_count` for clarity

Review-Finding: abc12345-0.md
Generated-by: deep-architect review-action
```

### Quality checks

After a coding agent applies a fix, `review-action` runs the **target repo's own quality
checks** before ever committing — a finding is never marked resolved while checks it
introduced are failing (fail-closed). Two kinds of checks are supported:

- **Programmatic** — lint/format/type-check/security/test commands (ruff, mypy, black,
  bandit, pytest, ...), declared per glob-scoped profile.
- **LLM-judged** — style/convention rules that need judgment rather than a CLI tool (e.g. a
  `python-style.md` guide), judged one modified file's diff at a time.

The LLM judge runs through the **same coding-agent CLI as the fix agent** — whichever
`--provider` (opencode/claude/grok) is selected — so its endpoint and credentials always match
the fix agent's; there's no separate credential setup. Judge output is validated as JSON with a
parse-and-retry loop bounded by `thresholds.judge_parse_retries` (default 2 retries, i.e. 3
attempts) in `~/.config/deep-architect/config.toml`. If you want a cheaper/faster first pass, pass
`--skip-llm-checks` to run programmatic checks only.

**Declaring checks.** Create `.quality-checks.toml` at the repo root (see
`.quality-checks.toml.template` for the full format):

```toml
[[profile]]
name = "backend-api"
paths = ["backend/fastapi/**"]
commands = [
  "ruff check backend/fastapi/src/",
  "mypy backend/fastapi/src/",
  "uv run pytest backend/fastapi/tests/ -v",
]

[llm_rules]
source = ".opencodereview/rule.json"
```

A file can match more than one profile; every matching profile's commands run against it.
Commands run with `shell=False`; a command containing the literal `{files}` placeholder is
expanded with the space-joined, repo-relative list of modified files matching that profile.

This convention is **agent-agnostic** — any coding agent can read the same file (a repo can
reference it from its `CLAUDE.md`/`AGENTS.md`), not just `review-action`.

**Auto-detection fallback.** If `.quality-checks.toml` is absent, `review-action` walks the
repo for `pyproject.toml` files (max depth 3, skipping hidden/`.venv`/`node_modules`/`build`/
`dist` dirs) and emits one file-scoped profile per directory that declares `[tool.ruff]`,
`[tool.mypy]`, `[tool.black]`, or `[tool.bandit]` config (or lists them as a dependency).
**Test commands are never auto-detected** — test scope can't be inferred safely; declare them
explicitly in `.quality-checks.toml`. If `.opencodereview/rule.json` exists, it's picked up
automatically even in auto-detect mode.

**LLM rules discovery.** `[llm_rules].source` (default `.opencodereview/rule.json`) is read as
a JSON list of `{"path": "<glob>", "rule": "<markdown>"}` entries (or `{"rules": [...]}`, the
shape produced by `opencodereview`'s `generate-rules.py`). If absent, `.opencodereview/rules/
**/*.md` files are used instead, each scoped to `**/*.py`. If neither exists, LLM checks are
silently skipped — nothing declared, nothing enforced. The judge is told that **programmatic
tool config wins on overlap** (e.g. a rule mandating 80-char lines is ignored if ruff/black are
already configured for 100).

**Baseline-diff semantics.** A pre-fix baseline is captured before the agent touches
anything; only failures *introduced by the fix* block. A failure that was already present at
baseline is advisory (logged as a warning) unless the post-fix output has new lines mentioning
a file the fix modified — then it blocks.

**The fix loop.** On a check failure, the failure report (programmatic output + cited style
violations) is fed back to the coding agent via a `fix_check_failures` call, and checks rerun.
This repeats up to `thresholds.check_max_fix_iterations` (default 3; `0` = checks run and are
logged but never block/retry — a report-only escape hatch). If checks are still failing when
the cap is hit, the fix's modified files are restored via git (fail-closed) and the finding is
marked `error` — a later run (without `--skip-errors`) retries it.

Relevant config keys (`~/.config/deep-architect/config.toml`, `[thresholds]`) — see
`config.toml.template`:

```toml
check_max_fix_iterations = 3    # post-fix quality-check retry cap; 0 = report-only
check_command_timeout    = 120  # default per-command timeout (seconds) for auto-detected checks
coding_agent_timeout     = 300  # per coding-agent call timeout (seconds); omit for per-agent default (opencode 120, claude/grok 300)
judge_parse_retries      = 2    # LLM style-judge JSON parse retries (attempts = retries + 1)
```

### Grok Build backend

`review-action` can drive [Grok Build](https://docs.x.ai/build/overview) (xAI's terminal-native
coding agent CLI) as a third coding-agent backend, alongside `opencode` and `claude`.

**Install.**

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
```

**Auth.** Either an interactive `grok login` session (cached on the machine) or, for
headless/CI use, the `XAI_API_KEY` environment variable — a pay-as-you-go console.x.ai key,
env-var-only per this project's convention:

```bash
export XAI_API_KEY=<your-key>
```

**Binary override.** `review-action` expects `grok` on `PATH`. Override with `GROK_BIN`:

```bash
export GROK_BIN=/path/to/your/grok
```

**Model selection.** Pass `--model` to select a specific model (e.g. `grok-build`); if
omitted, grok uses its own configured default model.

```bash
uv run review-action feedback/ --provider grok --model grok-build
```

**Timeout.** Governed by the same `[thresholds] coding_agent_timeout` key as the other
backends (default 300s when unset).

---

## Development

```bash
git clone https://github.com/geraldthewes/deep-architect
cd deep-architect
uv sync

uv run python -m pytest tests/ -v        # run tests
uv run ruff check deep_architect/        # lint
uv run mypy deep_architect/              # type check
uv run bandit -r deep_architect/ -ll     # security scan
```

All four must pass before committing.
