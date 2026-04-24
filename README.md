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
uv sync
```

Verify the install:

```bash
uv run adversarial-architect --help
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

### Step 2: Create `~/.deep-architect.toml`

```bash
cp .deep-architect.toml.template ~/.deep-architect.toml
```

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

That's it. The tool runs unattended. When it finishes, `knowledge/architecture/` contains your architecture.

### All CLI Options

| Flag | Description |
|------|-------------|
| `--prd PATH` | Path to the PRD Markdown file (greenfield mode) |
| `--codebase PATH` | Path to the git repository to analyze (reverse-engineer mode) |
| `--output PATH` | Output directory (default: `<codebase>/knowledge/architecture/` in RE mode) |
| `--resume` | Resume an interrupted run from the last completed sprint |
| `--config PATH` | Config file path (default: `~/.deep-architect.toml`) |
| `--model-generator TEXT` | Override the generator model alias for this run |
| `--model-critic TEXT` | Override the critic model alias for this run |
| `--context PATH` | Supplementary context file injected into every generator prompt (repeatable) |
| `--reset-sprint N` | Reset sprint N to its initial state and resume from it; deletes sprint N's contract, feedback, and history entries |
| `--strict` | Halt the run when a sprint cannot meet exit criteria, instead of accepting the best-effort result and continuing |

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
Create `~/.deep-architect.toml` from the template: `cp .deep-architect.toml.template ~/.deep-architect.toml`.

**`Error: PRD file not found`**
The `--prd` path must point to an existing file. Check the path is correct relative to your current directory.

**`Error: Codebase directory not found`**
The `--codebase` path must be an existing directory. Pass an absolute path to avoid ambiguity.

**`Either --prd (greenfield) or --codebase (reverse-engineer) is required`**
One of the two mode flags must be provided. They are mutually exclusive — don't pass both.

**`not inside a git repository`**
The output directory must be inside a git repo. Initialize one first: `git init`.

**ANTHROPIC_BASE_URL not respected**
The SDK may fall back to the bundled Claude binary, which can ignore custom env vars. Set `cli_path` in `~/.deep-architect.toml` to the output of `which claude`.

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

---

## Tips for Best Results

- **Use a capable generator model.** The generator needs to produce complete, well-structured Markdown with valid Mermaid syntax in a single agentic loop. `opus` gives the best results; `sonnet` is a good cost/quality balance.
- **Use a different model for critic and generator** if your setup allows it. The adversarial dynamic works best when the agents have different "opinions."
- **Make your PRD specific (greenfield).** The generator reads the PRD for every sprint. Concrete technology choices and user journeys lead to better architecture. Vague PRDs produce vague diagrams.
- **Point at a well-documented repo (reverse-engineer).** The generator discovers the codebase via its tools each round. Repos with a README, clear entry points, and conventional structure (e.g. `pyproject.toml`, `package.json`, IaC configs) yield the most accurate diagrams. If the repo is large, consider adding a `--context` file that describes the top-level layout.
- **Check Sprint 1 before walking away.** After sprint 1 completes, glance at `c1-context.md`. If the system boundary is wrong, the rest of the architecture will follow the wrong path — better to catch it early.
- **Raise `max_turns` for complex systems.** If the generator is consistently cutting off before producing all required files, increase `max_turns` in the `[generator]` section.

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
