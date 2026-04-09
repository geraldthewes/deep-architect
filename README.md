# deep-researcher

Autonomous adversarial C4 architecture harness for BMAD projects.

Takes a BMAD PRD as input and produces a hardened `knowledge/architecture/` folder using a **Generator ↔ Critic adversarial loop** across 7 sprints.

## How It Works

1. **Contract Negotiation**: For each sprint, Generator proposes acceptance criteria; Critic tightens them
2. **Generator builds**: Winston (senior architect persona) writes C4 Mermaid diagrams and docs
3. **Critic evaluates**: Hostile senior architect scores each criterion and provides specific feedback
4. **Loop repeats** until scores ≥ 9.0 with no Critical/High issues (or ping-pong detected)
5. **Final agreement**: Both agents independently confirm `READY_TO_SHIP`

Architecture files are auto-committed to git after each generator pass.

## Installation

```bash
pip install git+https://github.com/geraldthewes/deep-researcher
```

Or in editable mode for development:

```bash
git clone https://github.com/geraldthewes/deep-researcher
cd deep-researcher
pip install -e ".[dev]"
```

## Configuration

Create `~/.deep-researcher.toml` (copy from `.deep-researcher.toml.template`):

```toml
[generator]
base_url = "https://your-generator-endpoint/v1"
api_key  = "your-generator-api-key"
model    = "nemotron-122b"

[critic]
base_url = "https://your-critic-endpoint/v1"
api_key  = "your-critic-api-key"
model    = "qwen-27b"

[thresholds]
min_score                      = 9.0
consecutive_passing_rounds     = 2
max_rounds_per_sprint          = 6
max_total_rounds               = 40
timeout_hours                  = 3.0
ping_pong_similarity_threshold = 0.85
```

Any OpenAI-compatible endpoint works for both generator and critic.

## Usage

```bash
adversarial-architect --prd knowledge/prd.md --output knowledge/architecture
```

### CLI Options

| Flag | Description |
|------|-------------|
| `--prd PATH` | Path to the PRD Markdown file (required) |
| `--output PATH` | Output directory for architecture files (required) |
| `--resume` | Resume an interrupted run from the last completed sprint |
| `--config PATH` | Config file path (default: `~/.deep-researcher.toml`) |
| `--model-generator TEXT` | Override generator model name |
| `--model-critic TEXT` | Override critic model name |

### Example

```bash
# Run inside a BMAD repo
cd my-project
adversarial-architect \
  --prd knowledge/prd.md \
  --output knowledge/architecture \
  --model-generator nemotron-122b \
  --model-critic qwen-27b
```

## Output Structure

```
knowledge/architecture/
├── c1-context.md            # C4 Level 1: System Context
├── c2-container.md          # C4 Level 2: Container Overview
├── frontend/
│   └── c2-container.md      # Frontend container detail
├── backend/
│   └── c2-container.md      # Backend/orchestration detail
├── database/
│   └── c2-container.md      # Data layer detail
├── edge-functions/
│   └── c2-container.md      # Edge/deployment detail
├── deployment.md            # Deployment architecture
├── decisions/
│   ├── ADR-001.md           # Architecture Decision Records
│   └── cross-cutting.md     # NFRs and cross-cutting concerns
├── contracts/               # Sprint contracts (JSON)
├── feedback/                # Critic feedback rounds (JSON)
├── logs/                    # Run logs
└── progress.json            # Run progress / resume state
```

## Sprints

| # | Sprint | Primary Output |
|---|--------|----------------|
| 1 | C1 System Context | `c1-context.md` |
| 2 | C2 Container Overview | `c2-container.md` |
| 3 | Frontend Container | `frontend/c2-container.md` |
| 4 | Backend / Orchestration | `backend/c2-container.md` |
| 5 | Database + Knowledge Base | `database/c2-container.md` |
| 6 | Edge / Deployment / Auth | `edge-functions/c2-container.md`, `deployment.md` |
| 7 | ADRs + Cross-Cutting | `decisions/` |

## Environment Variables

None — all configuration is via `~/.deep-researcher.toml`.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Lint
ruff check deep_researcher/ tests/

# Type check
mypy deep_researcher/

# Security scan
bandit -r deep_researcher/ -ll
```

## Resuming Interrupted Runs

If a run is interrupted, resume from the last completed sprint:

```bash
adversarial-architect --prd knowledge/prd.md --output knowledge/architecture --resume
```

The harness reads `progress.json` to determine the starting sprint.
