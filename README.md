# deep-researcher

`deep-researcher` turns a BMAD Product Requirements Document (PRD) into a complete, peer-reviewed C4 architecture document — automatically, without human intervention.

It runs two LLM agents against each other: a **Generator** (Winston, the architect) who writes the architecture, and a **Critic** (a hostile senior architect) who tears it apart. They negotiate acceptance criteria, argue through rounds of feedback, and only stop when the architecture passes a rigorous quality bar. The result lands in `knowledge/architecture/` as a folder of Markdown + Mermaid files, ready for your development agents.

```
PRD → adversarial-architect → knowledge/architecture/ (C4 diagrams + ADRs)
```

---

## Prerequisites

- **Python 3.12+**
- **A git repository** — the tool auto-commits architecture files after each generator pass
- **Two LLM endpoints** with OpenAI-compatible APIs (one for the generator, one for the critic)
  - The generator benefits from a large, creative model (e.g. Nemotron 70B+)
  - The critic benefits from a sharp, analytical model (e.g. Qwen 32B+)
  - Both can point to the same endpoint and model if needed

---

## Installation

```bash
pip install git+https://github.com/geraldthewes/deep-researcher
```

Verify the install:

```bash
adversarial-architect --help
```

---

## Configuration

The tool reads credentials and model settings from `~/.deep-researcher.toml`. Create this file once and it applies to all your projects.

Copy the template to get started:

```bash
cp .deep-researcher.toml.template ~/.deep-researcher.toml
```

Then edit it with your endpoints and model names:

```toml
[generator]
base_url = "https://your-llm-endpoint/v1"
api_key  = "your-api-key"
model    = "nemotron-70b"

[critic]
base_url = "https://your-llm-endpoint/v1"
api_key  = "your-api-key"
model    = "qwen-32b"
```

The `[thresholds]` section is optional — the defaults work well for most projects:

```toml
[thresholds]
min_score                      = 9.0   # Critic score required to pass (out of 10)
consecutive_passing_rounds     = 2     # Must pass this many rounds in a row
max_rounds_per_sprint          = 6     # Give up on a sprint after this many rounds
max_total_rounds               = 40    # Hard limit across all sprints
timeout_hours                  = 3.0   # Wall-clock timeout
ping_pong_similarity_threshold = 0.85  # Auto-exit if feedback stops changing
```

---

## Running

From inside a BMAD repo that has a PRD:

```bash
adversarial-architect --prd knowledge/prd.md --output knowledge/architecture
```

That's it. The tool runs unattended. When it finishes, `knowledge/architecture/` contains your architecture.

### All CLI Options

| Flag | Description |
|------|-------------|
| `--prd PATH` | Path to the PRD Markdown file **(required)** |
| `--output PATH` | Output directory for architecture files **(required)** |
| `--resume` | Resume an interrupted run from the last completed sprint |
| `--config PATH` | Config file path (default: `~/.deep-researcher.toml`) |
| `--model-generator TEXT` | Override the generator model for this run |
| `--model-critic TEXT` | Override the critic model for this run |

### Overriding Models Per-Run

You can try a different model without editing your config:

```bash
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --model-generator llama-3.3-70b \
  --model-critic mistral-large
```

---

## What Happens During a Run

The tool works through **7 sprints**, each producing a specific part of the architecture. For each sprint:

1. **Contract negotiation** — Generator proposes specific, testable acceptance criteria for the sprint. Critic tightens them (adds edge cases, raises standards).
2. **Generator writes** — Produces Markdown files with C4 Mermaid diagrams and narrative.
3. **Critic scores** — Evaluates every criterion 1–10, flags severity (Critical / High / Medium / Low), and gives detailed feedback with file references.
4. **Loop** — Generator revises based on feedback. Repeats until the average score reaches 9.0/10 with no Critical or High issues, held for 2 consecutive rounds.
5. **Auto-commit** — Each generator pass is committed to git with a message like `Generator pass 2 - sprint 1 (C1 System Context)`.

After all 7 sprints, both agents independently review the complete architecture and must output `READY_TO_SHIP` before the run is declared complete.

### Sprints

| # | Sprint | What Gets Produced |
|---|--------|--------------------|
| 1 | C1 System Context | Top-level diagram: users, system boundary, external dependencies |
| 2 | C2 Container Overview | All major containers, tech stack decisions |
| 3 | Frontend Container | Frontend architecture, auth flows, routing, state management |
| 4 | Backend / Orchestration | API design, business logic, LLM orchestration, workers |
| 5 | Database + Knowledge Base | Data stores, vector store, caching, schema overview |
| 6 | Edge / Deployment / Observability | Edge functions, deployment topology, auth, scaling, monitoring |
| 7 | ADRs + Cross-Cutting Concerns | Architecture Decision Records, NFRs, security, compliance |

### Console Output

You'll see live progress like this:

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

Logs are also written to `knowledge/architecture/logs/architect-run-YYYYMMDD-HHMMSS.log`.

### Typical Run Time

Expect 1–3 hours depending on model speed and how many revision rounds are needed. The 3-hour hard timeout prevents runaway costs.

---

## Output Structure

```
knowledge/architecture/
├── c1-context.md              # C4 Level 1: System Context diagram
├── c2-container.md            # C4 Level 2: Container overview + tech stack
├── frontend/
│   └── c2-container.md        # Frontend container detail
│   └── auth.md                # (if warranted by the PRD)
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
├── logs/                      # Full run logs
└── progress.json              # Run state — used by --resume
```

The Mermaid diagrams in these files render natively on GitHub.

---

## Resuming an Interrupted Run

If the run is interrupted (crash, timeout, Ctrl+C), resume from where it left off:

```bash
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --resume
```

The harness reads `progress.json` to find the last completed sprint and picks up from there. Already-committed architecture files are left untouched.

---

## Inspecting Results

### Critic Feedback

To understand why a sprint took many rounds, read the feedback files:

```bash
cat knowledge/architecture/feedback/sprint-1-round-1.json
```

Each feedback file contains per-criterion scores, severity ratings, and specific comments.

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

**`Config file not found`**
Create `~/.deep-researcher.toml` with your `[generator]` and `[critic]` sections. Copy `.deep-researcher.toml.template` as a starting point.

**`Error: PRD file not found`**
The `--prd` path must point to an existing file. Check the path is correct relative to your current directory.

**`not inside a git repository`**
The output directory must be inside a git repo. Initialize one first: `git init`.

**A sprint fails after max rounds**
The critic's bar wasn't met in 6 rounds. Check the feedback JSON for the sprint to understand the recurring issues. Common causes: the PRD lacks enough detail for the generator to work from, or the models are too small for reliable structured output.

**Run stops mid-way due to timeout**
Use `--resume` to continue. If the 3-hour limit is too tight for your models, increase it in `~/.deep-researcher.toml`:
```toml
[thresholds]
timeout_hours = 6.0
```

---

## Tips for Best Results

- **Use a strong generator model.** The generator needs to produce complete, well-structured Markdown with valid Mermaid syntax. Models with 70B+ parameters tend to be more reliable.
- **Use a different model for critic and generator.** Having both agents on the same model creates an echo chamber. The adversarial dynamic works best when the models have different "opinions."
- **Make your PRD specific.** The generator reads the PRD for every sprint. Vague PRDs produce vague architecture. Concrete technology choices and user journeys in the PRD lead to better output.
- **Check Sprint 1 before walking away.** After sprint 1 completes, glance at `c1-context.md`. If the system boundary is wrong, the rest of the architecture will follow the wrong path — better to catch it early.

---

## Development

```bash
git clone https://github.com/geraldthewes/deep-researcher
cd deep-researcher
pip install -e ".[dev]"

python -m pytest tests/ -v        # run tests
ruff check deep_researcher/        # lint
mypy deep_researcher/              # type check
bandit -r deep_researcher/ -ll     # security scan
```
