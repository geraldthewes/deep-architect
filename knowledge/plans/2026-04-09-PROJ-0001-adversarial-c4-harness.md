# Adversarial C4 Architecture Harness Implementation Plan

## Overview

Build `deep-architect`: a standalone Python package that takes a BMAD PRD as input and autonomously produces a hardened C4 architecture document in `knowledge/architecture/` using a Generator ↔ Critic adversarial loop. Installable via `pip install` and usable in any repo.

## Current State Analysis

Greenfield project. The repo contains only `docs/prd.md` and the ticket. No existing code.

## Desired End State

Running:
```bash
adversarial-architect --prd knowledge/prd.md --output knowledge/architecture
```
inside a BMAD repo produces a complete, hardened `knowledge/architecture/` folder with C1/C2 Mermaid diagrams and ADRs — no human intervention required.

### Key Discoveries

- **adversarial-dev** (reference): TypeScript + Claude Agent SDK. Three agents: Planner → Generator ↔ Evaluator. File comms: `contracts/sprint-{n}.json`, `feedback/sprint-{n}-round-{m}.json`, `progress.json`. Contract negotiation: Generator proposes JSON → Evaluator outputs `APPROVED` or revised JSON. Eval: structured JSON `{passed, scores{}, feedback[], overallSummary}`.
- **deepagents**: Does not exist on PyPI. PRD hallucinated it. Replaced with **pydantic-ai 1.78.0** for structured LLM output.
- **pydantic-ai**: Supports any OpenAI-compatible endpoint via `OpenAIModel(model, base_url=..., api_key=...)`. `result_type=CriticResult` makes the LLM output a validated Pydantic model — eliminates manual JSON parsing and fragile fallback strategies.
- **Mermaid C4**: GitHub renders natively. `C4Context` (C1), `C4Container` (C2), `C4Component` (C3). Always end diagrams with `UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")`. Avoid `%%{init}%%` theming.
- **BMAD output**: The `knowledge/architecture/` folder structure is deep-architect's output format, not BMAD's native output. Generator decides which area-specific files to create within each sprint's target directory.
- **Dependency versions**: openai 2.31.0, pydantic 2.12.5, pydantic-ai 1.78.0, typer 0.24.1, gitpython 3.1.46, rich 14.3.3.

## Prompt Bootstrap Strategy

Prompts live in `deep_architect/prompts/` as `.md` files loaded at runtime. This keeps them editable without touching Python code and version-controlled separately from logic. Each prompt file documents its bootstrap source so future maintainers know what it's derived from.

### Bootstrap Sources

| Prompt File | Bootstrap Source | Content |
|---|---|---|
| `generator_system.md` | `llm-switch/_bmad/bmm/3-solutioning/bmad-agent-architect/SKILL.md` + PRD §5.1 + adversarial-dev `GENERATOR_SYSTEM_PROMPT` | Winston persona, C4 diagram rules, file output rules |
| `critic_system.md` | PRD §5.1 + adversarial-dev `EVALUATOR_SYSTEM_PROMPT` (adapted code→architecture) | Hostile senior architect persona, scoring 1-10, severity rules, JSON output format |
| `contract_proposal.md` | adversarial-dev `CONTRACT_NEGOTIATION_GENERATOR_PROMPT` + PRD §5.3 | JSON contract structure, criterion specificity requirements |
| `contract_review.md` | adversarial-dev `CONTRACT_NEGOTIATION_EVALUATOR_PROMPT` | APPROVED or revised JSON, adversarial tightening |
| `ping_pong_check.md` | PRD §5.4 ping-pong criteria (new — no direct reference) | Similarity scoring, diminishing returns detection |
| `final_agreement.md` | PRD §5.4 "Mutual Agreement" round (new — no direct reference) | READY_TO_SHIP protocol |
| `sprint_1_c1_context.md` | PRD §5.2 Sprint 1 + `bmad-create-architecture/steps/step-02-context.md` | C1 diagram generation guidance |
| `sprint_2_c2_container.md` | PRD §5.2 Sprint 2 + `bmad-create-architecture/steps/step-04-decisions.md` | C2 diagram + tech stack decisions |
| `sprint_3_frontend.md` | PRD §5.2 Sprint 3 + `bmad-create-architecture/steps/step-05-patterns.md` | Frontend container + area docs |
| `sprint_4_backend.md` | PRD §5.2 Sprint 4 | Backend/orchestration container |
| `sprint_5_database.md` | PRD §5.2 Sprint 5 | Database + knowledge base container |
| `sprint_6_edge.md` | PRD §5.2 Sprint 6 + `bmad-create-architecture/steps/step-06-structure.md` | Edge functions, deployment, auth, observability |
| `sprint_7_adrs.md` | PRD §5.2 Sprint 7 + `bmad-create-architecture/architecture-decision-template.md` | ADR format and cross-cutting concerns |

### What Each Bootstrap Source Contributes

**Winston persona** (`bmad-agent-architect/SKILL.md`):
- Identity: "Senior architect. Balances vision with pragmatism."
- Principles: boring technology for stability, simple solutions, user journeys drive technical decisions
- Communication style: calm, pragmatic, grounded in real-world trade-offs

**adversarial-dev prompts** (adapted from code evaluation → C4 architecture):
- Generator: build one artifact at a time, git commit after each, self-evaluate before declaring done
- Evaluator: do NOT be generous, test EVERY criterion, provide file:line references, structured JSON output only
- Contract negotiation: specific testable criteria (not vague), 5-15 criteria per sprint, coverage of edge cases

**BMAD create-architecture steps**:
- Step 2 (Context): how to extract architectural implications from PRD
- Step 4 (Decisions): decision categories: data, auth, API, frontend, infrastructure
- Step 5 (Patterns): implementation patterns and consistency rules  
- Step 6 (Structure): complete project tree, boundary definitions
- ADR template: Status / Context / Decision / Consequences format

**PRD §5.1 system prompt seeds** (exact quotes to preserve):
- Generator: *"You are Winston, the BMAD Architect. Produce the highest-quality C4 architecture possible."*
- Critic: *"You are a hostile senior architect. Ruthlessly try to kill this design before it reaches production. Be exhaustive and specific."*

### Future Enhancement Path
The bootstrap prompts are intentionally minimal — a v1 that works. Future versions can incorporate patterns from the broader community (e.g. awesome-claude-prompts, LangChain Hub architecture templates, Anthropic's prompt engineering guides). The `.md` file format makes drop-in replacement easy.

### Prompt Loader

```python
# deep_architect/prompts/__init__.py
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template and substitute {variables}."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {name}")
    template = path.read_text()
    return template.format_map(kwargs) if kwargs else template
```

---

## What We're NOT Doing

- No LangGraph, LangChain, deepagents, smolagents, or CrewAI
- No C4 Code-level (C4) diagrams
- No code generation
- No UI or web interface
- No per-repo config files
- No LangGraph fallback
- No dynamic sprint generation (sprints are fixed at 7)

## Implementation Approach

Custom Python orchestration using pydantic-ai for LLM calls. The harness manages the adversarial loop directly — no framework required beyond pydantic-ai's structured output. This mirrors adversarial-dev's plain TypeScript approach.

---

## Phase 1: Package Scaffolding & Core Infrastructure

### Overview
Create the installable package structure, CLI entry point, config loading, logging, and git validation. After this phase, `adversarial-architect --help` works.

### Changes Required

#### 1. `pyproject.toml`
**File**: `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "deep-architect"
version = "0.1.0"
description = "Autonomous adversarial C4 architecture harness for BMAD projects"
requires-python = ">=3.12"
dependencies = [
    "pydantic-ai>=1.78.0",
    "pydantic>=2.12.5",
    "typer>=0.24.1",
    "gitpython>=3.1.46",
    "rich>=14.3.3",
    "openai>=2.31.0",
]

[project.scripts]
adversarial-architect = "deep_architect.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["deep_architect"]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "mypy>=1.10",
    "ruff>=0.5",
    "bandit>=1.8",
]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP"]

[tool.mypy]
python_version = "3.12"
strict = true
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

#### 2. Package directory structure
Create all `__init__.py` files and subdirectories:

```
deep_architect/
├── __init__.py
├── __main__.py
├── cli.py
├── config.py
├── logger.py
├── harness.py
├── sprints.py
├── exit_criteria.py
├── git_ops.py
├── agents/
│   ├── __init__.py
│   ├── client.py
│   ├── generator.py
│   └── critic.py
├── models/
│   ├── __init__.py
│   ├── contract.py
│   ├── feedback.py
│   └── progress.py
├── io/
│   ├── __init__.py
│   └── files.py
└── prompts/                        ← prompt .md files, loaded at runtime
    ├── __init__.py                 ← load_prompt() function
    ├── generator_system.md         ← Winston persona + C4 rules
    ├── critic_system.md            ← Hostile architect + scoring rubric
    ├── contract_proposal.md        ← Generator proposes contract JSON
    ├── contract_review.md          ← Critic tightens contract
    ├── ping_pong_check.md          ← Similarity detection
    ├── final_agreement.md          ← READY_TO_SHIP round
    ├── sprint_1_c1_context.md      ← C1 diagram guidance
    ├── sprint_2_c2_container.md    ← C2 + tech stack guidance
    ├── sprint_3_frontend.md        ← Frontend container guidance
    ├── sprint_4_backend.md         ← Backend/orchestration guidance
    ├── sprint_5_database.md        ← Database + knowledge base guidance
    ├── sprint_6_edge.md            ← Edge/deployment guidance
    └── sprint_7_adrs.md            ← ADR format + cross-cutting concerns
tests/
├── __init__.py
├── test_config.py
├── test_exit_criteria.py
├── test_files.py
├── test_git_ops.py
├── test_models.py
└── test_prompts.py                 ← verify all prompts load + variables substitute
```

#### 3. `deep_architect/config.py`
**File**: `deep_architect/config.py`

```python
from __future__ import annotations

import tomllib
from pathlib import Path
from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    base_url: str
    api_key: str
    model: str


class ThresholdConfig(BaseModel):
    min_score: float = 9.0
    consecutive_passing_rounds: int = 2
    max_rounds_per_sprint: int = 6
    max_total_rounds: int = 40
    timeout_hours: float = 3.0
    ping_pong_similarity_threshold: float = 0.85


class HarnessConfig(BaseModel):
    generator: AgentConfig
    critic: AgentConfig
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)


def load_config(config_path: Path | None = None) -> HarnessConfig:
    """Load config from ~/.deep-architect.toml, with optional override path."""
    if config_path is None:
        config_path = Path.home() / ".deep-architect.toml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Create ~/.deep-architect.toml with [generator] and [critic] sections."
        )

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    return HarnessConfig.model_validate(raw)
```

**`~/.deep-architect.toml` template** (document in `.env.template` equivalent):
```toml
# ~/.deep-architect.toml — deep-architect configuration

[generator]
base_url = "https://your-generator-endpoint/v1"
api_key  = "your-generator-api-key"
model    = "nemotron-122b"

[critic]
base_url = "https://your-critic-endpoint/v1"
api_key  = "your-critic-api-key"
model    = "qwen-27b"

[thresholds]
min_score                     = 9.0
consecutive_passing_rounds    = 2
max_rounds_per_sprint         = 6
max_total_rounds              = 40
timeout_hours                 = 3.0
ping_pong_similarity_threshold = 0.85
```

#### 4. `deep_architect/logger.py`
**File**: `deep_architect/logger.py`

```python
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()
_file_handler: logging.FileHandler | None = None


def setup_logging(log_dir: Path) -> Path:
    """Initialize rich console + rotating file logger. Returns log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"architect-run-{run_id}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            RichHandler(console=console, rich_tracebacks=True),
            logging.FileHandler(log_file),
        ],
    )
    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
```

#### 5. `deep_architect/git_ops.py`
**File**: `deep_architect/git_ops.py`

```python
from __future__ import annotations

from pathlib import Path

import git


def validate_git_repo(path: Path) -> git.Repo:
    """Fail fast with clear error if path is not inside a git repo."""
    try:
        repo = git.Repo(path, search_parent_directories=True)
        return repo
    except git.InvalidGitRepositoryError:
        raise SystemExit(
            f"Error: {path} is not inside a git repository.\n"
            "adversarial-architect requires a git repo for auto-commits."
        )


def git_commit(repo: git.Repo, message: str, paths: list[Path]) -> None:
    """Stage the given paths and create a commit."""
    repo.index.add([str(p) for p in paths])
    if repo.index.diff("HEAD") or repo.untracked_files:
        repo.index.commit(message)
```

#### 6. `deep_architect/cli.py`
**File**: `deep_architect/cli.py`

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from deep_architect.config import load_config
from deep_architect.harness import run_harness

app = typer.Typer(help="Adversarial C4 architecture harness for BMAD projects.")
console = Console()


@app.command()
def main(
    prd: Path = typer.Option(..., help="Path to the PRD Markdown file"),
    output: Path = typer.Option(..., help="Output directory for architecture files"),
    resume: bool = typer.Option(False, "--resume", help="Resume an interrupted run"),
    config_file: Optional[Path] = typer.Option(None, "--config", help="Config file path (default: ~/.deep-architect.toml)"),
    model_generator: Optional[str] = typer.Option(None, "--model-generator", help="Override generator model name"),
    model_critic: Optional[str] = typer.Option(None, "--model-critic", help="Override critic model name"),
) -> None:
    """Run the adversarial C4 architecture harness."""
    if not prd.exists():
        console.print(f"[red]Error:[/red] PRD file not found: {prd}")
        raise typer.Exit(1)

    cfg = load_config(config_file)
    if model_generator:
        cfg.generator.model = model_generator
    if model_critic:
        cfg.critic.model = model_critic

    asyncio.run(run_harness(prd=prd, output_dir=output, resume=resume, config=cfg))
```

### Success Criteria

#### Automated Verification
- [x] `pip install -e .` succeeds
- [x] `adversarial-architect --help` prints usage without errors
- [x] `python -m pytest tests/test_config.py -v` passes
- [x] `ruff check deep_architect/` passes
- [x] `mypy deep_architect/` passes

#### Manual Verification
- [ ] Running with no config file gives a clear, actionable error message
- [ ] `--help` output is readable and complete

**Implementation Note**: After automated verification passes, confirm manually that the CLI help text is clear before proceeding to Phase 2.

---

## Phase 2: Pydantic Schemas and File I/O

### Overview
Define all data models and file I/O functions. After this phase, all data can be serialized/deserialized to disk correctly.

### Changes Required

#### 1. `deep_architect/models/contract.py`

```python
from __future__ import annotations

from pydantic import BaseModel, Field


class SprintCriterion(BaseModel):
    name: str
    description: str
    threshold: float = Field(default=9.0, ge=1.0, le=10.0)


class SprintContract(BaseModel):
    sprint_number: int
    sprint_name: str
    files_to_produce: list[str]
    criteria: list[SprintCriterion] = Field(min_length=3)
```

#### 2. `deep_architect/models/feedback.py`

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CriterionScore(BaseModel):
    criterion: str
    score: float = Field(ge=1.0, le=10.0)
    severity: Literal["Critical", "High", "Medium", "Low"]
    details: str = Field(description="Specific feedback with file:line references where applicable")


class CriticResult(BaseModel):
    scores: dict[str, float]
    feedback: list[CriterionScore] = Field(min_length=1)
    overall_summary: str
    # Computed fields
    average_score: float = Field(default=0.0)
    passed: bool = Field(default=False)

    @model_validator(mode="after")
    def compute_pass(self) -> "CriticResult":
        if self.feedback:
            self.average_score = sum(f.score for f in self.feedback) / len(self.feedback)
        has_critical_high = any(f.severity in ("Critical", "High") for f in self.feedback)
        self.passed = self.average_score >= 9.0 and not has_critical_high
        return self


class PingPongResult(BaseModel):
    similarity_score: float = Field(ge=0.0, le=1.0)
    reasoning: str
```

#### 3. `deep_architect/models/progress.py`

```python
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SprintStatus(BaseModel):
    sprint_number: int
    sprint_name: str
    status: Literal["pending", "negotiating", "building", "evaluating", "passed", "failed"] = "pending"
    rounds_completed: int = 0
    final_score: float | None = None


class HarnessProgress(BaseModel):
    status: Literal["running", "complete", "failed"] = "running"
    current_sprint: int = 1
    total_sprints: int
    completed_sprints: int = 0
    total_rounds: int = 0
    started_at: datetime = Field(default_factory=datetime.utcnow)
    sprint_statuses: list[SprintStatus]
```

#### 4. `deep_architect/io/files.py`

```python
from __future__ import annotations

import json
from pathlib import Path

from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult
from deep_architect.models.progress import HarnessProgress


def init_workspace(output_dir: Path) -> None:
    """Create the architecture output directory and harness artifact subdirs."""
    for subdir in ["contracts", "feedback", "decisions"]:
        (output_dir / subdir).mkdir(parents=True, exist_ok=True)


def save_contract(output_dir: Path, contract: SprintContract) -> Path:
    path = output_dir / "contracts" / f"sprint-{contract.sprint_number}.json"
    path.write_text(contract.model_dump_json(indent=2))
    return path


def load_contract(output_dir: Path, sprint_number: int) -> SprintContract:
    path = output_dir / "contracts" / f"sprint-{sprint_number}.json"
    return SprintContract.model_validate_json(path.read_text())


def save_feedback(output_dir: Path, sprint_number: int, round_num: int, result: CriticResult) -> Path:
    path = output_dir / "feedback" / f"sprint-{sprint_number}-round-{round_num}.json"
    path.write_text(result.model_dump_json(indent=2))
    return path


def load_feedback(output_dir: Path, sprint_number: int, round_num: int) -> CriticResult:
    path = output_dir / "feedback" / f"sprint-{sprint_number}-round-{round_num}.json"
    return CriticResult.model_validate_json(path.read_text())


def save_progress(output_dir: Path, progress: HarnessProgress) -> Path:
    path = output_dir / "progress.json"
    path.write_text(progress.model_dump_json(indent=2))
    return path


def load_progress(output_dir: Path) -> HarnessProgress:
    path = output_dir / "progress.json"
    return HarnessProgress.model_validate_json(path.read_text())


def save_round_log(output_dir: Path, sprint_number: int, round_num: int, data: dict) -> None:
    """Append structured round log for reproducibility."""
    path = output_dir / "feedback" / f"sprint-{sprint_number}-round-{round_num}-log.json"
    path.write_text(json.dumps(data, indent=2, default=str))
```

### Success Criteria

#### Automated Verification
- [x] `python -m pytest tests/test_models.py tests/test_files.py -v` passes
- [x] Round-trip: save contract → load contract returns identical object
- [x] `CriticResult.passed` is `False` when any `severity == "Critical"` even if avg ≥ 9.0
- [x] `CriticResult.passed` is `False` when avg < 9.0 even with no Critical/High
- [x] `mypy deep_architect/models/ deep_architect/io/` passes

---

## Phase 3: LLM Client and Agent Implementations

### Overview
Wire up pydantic-ai agents for Generator and Critic using OpenAI-compatible endpoints. Define system prompts and contract negotiation logic.

### Changes Required

#### 1. `deep_architect/sprints.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SprintDefinition:
    number: int
    name: str
    description: str
    primary_files: list[str]
    allow_extra_files: bool = False  # Generator may create area-specific docs


SPRINTS: list[SprintDefinition] = [
    SprintDefinition(
        number=1,
        name="C1 System Context",
        description="Generate the C4 Level 1 System Context diagram and narrative. "
                    "Show the system, its users, and external dependencies.",
        primary_files=["c1-context.md"],
    ),
    SprintDefinition(
        number=2,
        name="C2 Container Overview",
        description="Generate the overall C4 Level 2 Container diagram and tech stack decisions. "
                    "Show all major containers and their relationships.",
        primary_files=["c2-container.md"],
    ),
    SprintDefinition(
        number=3,
        name="Frontend Container",
        description="Generate the frontend container details. "
                    "Create frontend/c2-container.md and any area-specific documents "
                    "(e.g. auth.md, schemas.md) as warranted by the PRD.",
        primary_files=["frontend/c2-container.md"],
        allow_extra_files=True,
    ),
    SprintDefinition(
        number=4,
        name="Backend / Orchestration Container",
        description="Generate the backend and orchestration container details. "
                    "Create backend/c2-container.md and area-specific documents as needed.",
        primary_files=["backend/c2-container.md"],
        allow_extra_files=True,
    ),
    SprintDefinition(
        number=5,
        name="Database + Knowledge Base",
        description="Generate the database and knowledge base container details. "
                    "Create database/c2-container.md and any supporting documents.",
        primary_files=["database/c2-container.md"],
        allow_extra_files=True,
    ),
    SprintDefinition(
        number=6,
        name="Edge Functions / Deployment / Auth / Scaling / Observability",
        description="Generate edge-functions/c2-container.md and deployment.md covering "
                    "auth, scaling, and observability cross-cutting concerns.",
        primary_files=["edge-functions/c2-container.md", "deployment.md"],
    ),
    SprintDefinition(
        number=7,
        name="ADRs + Cross-Cutting Concerns",
        description="Generate Architecture Decision Records in the decisions/ folder "
                    "and document non-functional requirements and cross-cutting concerns.",
        primary_files=["decisions/"],
        allow_extra_files=True,
    ),
]
```

#### 2. `deep_architect/agents/client.py`
**File**: `deep_architect/agents/client.py`

```python
from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel

from deep_architect.config import AgentConfig


def make_openai_model(config: AgentConfig) -> OpenAIModel:
    return OpenAIModel(
        config.model,
        base_url=config.base_url,
        api_key=config.api_key,
    )


def make_text_agent(config: AgentConfig, system_prompt: str) -> Agent[None, str]:
    return Agent(
        model=make_openai_model(config),
        result_type=str,
        system_prompt=system_prompt,
    )
```

#### 3. `deep_architect/agents/generator.py`
**File**: `deep_architect/agents/generator.py`

```python
from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult
from deep_architect.sprints import SprintDefinition

GENERATOR_SYSTEM_PROMPT = """You are Winston, the BMAD Architect.
Your role is to produce the highest-quality C4 architecture documentation possible.

You will receive a PRD, a sprint contract, and optionally critic feedback from a previous round.

## C4 Diagram Rules
- C1 System Context: use `C4Context` Mermaid block
- C2 Container: use `C4Container` Mermaid block
- Always end diagrams with `UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")`
- Use `_Ext` suffix (System_Ext, Person_Ext) for external systems
- Include technology string in C2: Container(alias, "Name", "Tech", "Description")
- Do NOT use %%{init}%% directives — GitHub ignores them

## Output Rules
- Write complete, standalone Markdown files
- Each file must have a title heading, a brief narrative, the Mermaid diagram, and a section describing relationships
- When a Critic round provides feedback, address EVERY specific issue mentioned with file references

## File Path Rules
Output paths are relative to the output_dir provided. Create parent directories as needed.
"""

CONTRACT_PROPOSAL_PROMPT = """You are proposing a sprint contract for the following sprint.

## PRD
{prd}

## Sprint Definition
Sprint {sprint_number}: {sprint_name}
{sprint_description}

Primary files to produce: {primary_files}

Propose a sprint contract as a JSON object with this structure:
{{
  "sprint_number": {sprint_number},
  "sprint_name": "{sprint_name}",
  "files_to_produce": [...],
  "criteria": [
    {{"name": "...", "description": "Specific, testable criterion", "threshold": 9.0}},
    ...
  ]
}}

Include 5-10 criteria. Each must be SPECIFIC and TESTABLE — not vague.
Criteria must cover: Mermaid diagram validity, C4 completeness, narrative quality, 
accuracy to PRD, and relationship documentation.
Output ONLY the JSON."""


async def run_generator(
    agent: Agent[None, str],
    sprint: SprintDefinition,
    contract: SprintContract,
    prd_content: str,
    previous_feedback: CriticResult | None,
    output_dir: Path,
    round_num: int,
) -> list[Path]:
    """Run the Generator for one round. Returns list of files written."""
    feedback_section = ""
    if previous_feedback:
        feedback_section = f"""
## Critic Feedback from Round {round_num - 1} (MUST ADDRESS ALL ISSUES)

Average Score: {previous_feedback.average_score:.1f}/10
{chr(10).join(f"- [{f.severity}] {f.criterion} ({f.score}/10): {f.details}" for f in previous_feedback.feedback)}

Overall: {previous_feedback.overall_summary}
"""

    prompt = f"""## PRD
{prd_content}

## Sprint Contract
{contract.model_dump_json(indent=2)}

## Output Directory
{output_dir}

{feedback_section}

Generate the architecture files specified in the contract.
Write each file to {output_dir}/<filename>.
Return a summary of what you wrote and any design decisions made."""

    result = await agent.run(prompt)

    # Collect files written
    written = []
    for f in contract.files_to_produce:
        path = output_dir / f
        if path.exists():
            written.append(path)

    return written


async def propose_contract(
    agent: Agent[None, str],
    sprint: SprintDefinition,
    prd_content: str,
) -> str:
    """Generator proposes a sprint contract. Returns raw JSON string."""
    prompt = CONTRACT_PROPOSAL_PROMPT.format(
        prd=prd_content,
        sprint_number=sprint.number,
        sprint_name=sprint.name,
        sprint_description=sprint.description,
        primary_files=sprint.primary_files,
    )
    result = await agent.run(prompt)
    return result.data
```

#### 4. `deep_architect/agents/critic.py`
**File**: `deep_architect/agents/critic.py`

```python
from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult, PingPongResult

CRITIC_SYSTEM_PROMPT = """You are a hostile senior architect. Your job is to ruthlessly 
critique C4 architecture documents before they reach production. Be exhaustive and specific.

## Scoring Guidelines
- 9-10: Exceptional. Handles all edge cases, complete, no gaps.
- 7-8: Good. Minor issues only.
- 5-6: Partial. Significant gaps.
- 3-4: Poor. Fundamental issues.
- 1-2: Failed. Not implemented or broken.

## Severity Rules
- Critical: Fundamental flaw causing production failures or misrepresents the system
- High: Significant gap causing serious problems or missing key relationships
- Medium: Notable issue that should be addressed
- Low: Minor improvement opportunity

## Rules
- Do NOT be generous. Resist the urge to praise mediocre work.
- Include file:line references in your feedback where possible.
- Test EVERY criterion in the contract.
- If a Mermaid diagram has syntax errors, mark it Critical.
- If relationships between containers are missing or wrong, mark it High.
"""

CONTRACT_REVIEW_PROMPT = """Review this proposed sprint contract. Make criteria more specific,
add adversarial edge cases, and raise thresholds where needed.

If the contract is sufficiently rigorous, output exactly: APPROVED

Otherwise output a revised JSON contract with the same structure.
Output ONLY "APPROVED" or the revised JSON — nothing else.

## Proposed Contract
{contract_json}"""

PING_PONG_PROMPT = """Compare these two rounds of critic feedback. 
Estimate the semantic similarity (0.0 = completely different, 1.0 = identical issues).

## Previous Round Feedback
{previous_summary}

## Current Round Feedback
{current_summary}

Output a JSON object: {{"similarity_score": <float>, "reasoning": "<brief explanation>"}}"""


async def review_contract(
    agent: Agent[None, str],
    proposal_json: str,
) -> str:
    """Critic reviews the proposed contract. Returns 'APPROVED' or revised JSON."""
    prompt = CONTRACT_REVIEW_PROMPT.format(contract_json=proposal_json)
    result = await agent.run(prompt)
    return result.data.strip()


async def run_critic(
    agent: Agent[None, CriticResult],
    contract: SprintContract,
    output_dir: Path,
    round_num: int,
) -> CriticResult:
    """Run the Critic against the current architecture files."""
    # Build file content summary for the critic
    file_contents = []
    for fname in contract.files_to_produce:
        fpath = output_dir / fname
        if fpath.exists():
            content = fpath.read_text()
            file_contents.append(f"### {fname}\n```markdown\n{content}\n```")
        else:
            file_contents.append(f"### {fname}\n[FILE NOT FOUND]")

    prompt = f"""Evaluate these architecture files against the sprint contract.

## Sprint Contract
{contract.model_dump_json(indent=2)}

## Architecture Files (Round {round_num})

{chr(10).join(file_contents)}

Score each criterion. Return a CriticResult JSON object."""

    result = await agent.run(prompt)
    return result.data


async def check_ping_pong(
    agent: Agent[None, PingPongResult],
    current: CriticResult,
    previous: CriticResult,
) -> PingPongResult:
    """Use critic LLM to detect ping-pong / diminishing returns."""
    prompt = PING_PONG_PROMPT.format(
        previous_summary=previous.overall_summary,
        current_summary=current.overall_summary,
    )
    result = await agent.run(prompt)
    return result.data
```

### Success Criteria

#### Automated Verification
- [x] `mypy deep_architect/agents/ deep_architect/sprints.py` passes
- [x] `ruff check deep_architect/agents/` passes
- [x] All 7 sprints have non-empty `primary_files`

#### Manual Verification
- [ ] System prompts reviewed and confirm adversarial tone for Critic
- [ ] Contract proposal prompt reviewed for specificity requirements

---

## Phase 4: Exit Criteria and Harness Orchestration

### Overview
Implement the full adversarial loop: contract negotiation, per-sprint loop, exit criteria, ping-pong detection, global safety limits, and resume logic.

### Changes Required

#### 1. `deep_architect/exit_criteria.py`

```python
from __future__ import annotations

from deep_architect.models.feedback import CriticResult


def has_critical_or_high(result: CriticResult) -> bool:
    return any(f.severity in ("Critical", "High") for f in result.feedback)


def sprint_passes(result: CriticResult, min_score: float) -> bool:
    return result.average_score >= min_score and not has_critical_or_high(result)


def should_ping_pong_exit(
    similarity_score: float,
    current: CriticResult,
    previous: CriticResult,
    threshold: float,
) -> bool:
    """Exit if similarity above threshold AND no meaningful score improvement."""
    score_improvement = current.average_score - previous.average_score
    return similarity_score >= threshold and score_improvement < 0.1
```

#### 2. `deep_architect/harness.py`

```python
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from rich.console import Console

from deep_architect.agents.client import make_text_agent
from deep_architect.agents.critic import (
    check_ping_pong, review_contract, run_critic,
)
from deep_architect.agents.generator import propose_contract, run_generator
from deep_architect.config import AgentConfig, HarnessConfig
from deep_architect.exit_criteria import should_ping_pong_exit, sprint_passes
from deep_architect.git_ops import git_commit, validate_git_repo
from deep_architect.io.files import (
    init_workspace, load_progress, save_contract, save_feedback,
    save_progress, save_round_log,
)
from deep_architect.logger import get_logger, setup_logging
from deep_architect.models.contract import SprintContract
from deep_architect.models.feedback import CriticResult, PingPongResult
from deep_architect.models.progress import HarnessProgress, SprintStatus
from deep_architect.sprints import SPRINTS, SprintDefinition

logger = get_logger(__name__)
console = Console()


async def negotiate_contract(
    generator_agent: Agent,
    critic_agent: Agent,
    sprint: SprintDefinition,
    prd_content: str,
) -> SprintContract:
    """Generator proposes, Critic tightens. Returns final locked contract."""
    logger.info(f"[Sprint {sprint.number}] Negotiating contract...")
    proposal = await propose_contract(generator_agent, sprint, prd_content)

    review = await review_contract(critic_agent, proposal)
    final_json = proposal if review.upper() == "APPROVED" else review

    # Parse with fallback
    import json
    import re
    candidates = [final_json.strip()]
    for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", final_json):
        candidates.insert(0, block.strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            return SprintContract.model_validate(data)
        except Exception:
            continue

    raise ValueError(f"Could not parse contract for sprint {sprint.number}")


async def run_harness(
    prd: Path,
    output_dir: Path,
    resume: bool,
    config: HarnessConfig,
) -> None:
    log_dir = output_dir / "logs"
    log_file = setup_logging(log_dir)
    logger.info(f"Log file: {log_file}")

    repo = validate_git_repo(output_dir)
    init_workspace(output_dir)
    prd_content = prd.read_text()

    # Setup agents
    generator_text = make_text_agent(config.generator, system_prompt=...)
    # Critic uses result_type for structured output
    critic_model = OpenAIModel(
        config.critic.model,
        base_url=config.critic.base_url,
        api_key=config.critic.api_key,
    )
    critic_agent = Agent(model=critic_model, result_type=CriticResult, system_prompt=...)
    ping_pong_agent = Agent(model=critic_model, result_type=PingPongResult, system_prompt=...)

    # Resume or init progress
    if resume and (output_dir / "progress.json").exists():
        progress = load_progress(output_dir)
        start_sprint_idx = progress.current_sprint - 1
        logger.info(f"Resuming from sprint {progress.current_sprint}")
    else:
        progress = HarnessProgress(
            total_sprints=len(SPRINTS),
            sprint_statuses=[
                SprintStatus(sprint_number=s.number, sprint_name=s.name)
                for s in SPRINTS
            ],
        )
        start_sprint_idx = 0

    start_time = time.time()
    t = config.thresholds

    for sprint in SPRINTS[start_sprint_idx:]:
        logger.info(f"{'='*60}")
        logger.info(f"SPRINT {sprint.number}/{len(SPRINTS)}: {sprint.name}")
        logger.info(f"{'='*60}")

        # Contract negotiation
        contract = await negotiate_contract(generator_text, critic_agent, sprint, prd_content)
        save_contract(output_dir, contract)

        sprint_status = progress.sprint_statuses[sprint.number - 1]
        sprint_status.status = "building"
        progress.current_sprint = sprint.number

        last_result: CriticResult | None = None
        consecutive_passes = 0

        for round_num in range(1, t.max_rounds_per_sprint + 1):
            # Global safety checks
            if progress.total_rounds >= t.max_total_rounds:
                logger.error("Max total rounds reached — stopping.")
                progress.status = "failed"
                save_progress(output_dir, progress)
                return

            elapsed = time.time() - start_time
            if elapsed > t.timeout_hours * 3600:
                logger.error("3-hour timeout reached — stopping.")
                progress.status = "failed"
                save_progress(output_dir, progress)
                return

            logger.info(f"[Sprint {sprint.number}] Round {round_num}")

            # Generator builds
            sprint_status.status = "building"
            save_progress(output_dir, progress)
            written = await run_generator(
                generator_text, sprint, contract, prd_content,
                last_result, output_dir, round_num,
            )

            # Auto-commit
            git_commit(
                repo,
                f"Generator pass {round_num} - sprint {sprint.number} ({sprint.name})",
                written,
            )

            # Critic evaluates
            sprint_status.status = "evaluating"
            result = await run_critic(critic_agent, contract, output_dir, round_num)
            save_feedback(output_dir, sprint.number, round_num, result)
            save_round_log(output_dir, sprint.number, round_num, {
                "sprint": sprint.number,
                "round": round_num,
                "average_score": result.average_score,
                "passed": result.passed,
                "feedback_count": len(result.feedback),
            })

            progress.total_rounds += 1
            sprint_status.rounds_completed = round_num
            logger.info(
                f"[Sprint {sprint.number}] Round {round_num}: "
                f"avg={result.average_score:.1f} passed={result.passed}"
            )

            # Exit criteria
            if sprint_passes(result, t.min_score):
                consecutive_passes += 1
                logger.info(f"Consecutive passes: {consecutive_passes}/{t.consecutive_passing_rounds}")
                if consecutive_passes >= t.consecutive_passing_rounds:
                    logger.info(f"Sprint {sprint.number} PASSED")
                    sprint_status.status = "passed"
                    sprint_status.final_score = result.average_score
                    break
            else:
                consecutive_passes = 0

            # Ping-pong detection (after round 3)
            if round_num >= 3 and last_result is not None:
                pp = await check_ping_pong(ping_pong_agent, result, last_result)
                if should_ping_pong_exit(pp.similarity_score, result, last_result, t.ping_pong_similarity_threshold):
                    logger.warning(
                        f"Ping-pong detected (similarity={pp.similarity_score:.2f}) — "
                        f"auto-exiting sprint {sprint.number} as good enough"
                    )
                    sprint_status.status = "passed"
                    sprint_status.final_score = result.average_score
                    break

            last_result = result
        else:
            # Max rounds exhausted
            if sprint_status.status != "passed":
                logger.error(f"Sprint {sprint.number} FAILED after {t.max_rounds_per_sprint} rounds")
                sprint_status.status = "failed"
                progress.status = "failed"
                save_progress(output_dir, progress)
                return

        progress.completed_sprints += 1
        save_progress(output_dir, progress)

    # Final mutual agreement round
    await run_final_agreement(generator_text, critic_agent, prd_content, output_dir)
    progress.status = "complete"
    save_progress(output_dir, progress)
    logger.info("Harness COMPLETE — architecture is production-ready")


async def run_final_agreement(
    generator_agent: Agent,
    critic_agent: Agent,
    prd_content: str,
    output_dir: Path,
) -> None:
    """Both agents must independently output READY_TO_SHIP."""
    logger.info("Running final mutual agreement round...")

    FINAL_PROMPT = (
        "Review the complete architecture in {output_dir}. "
        "If it is production-ready and all C4 levels are complete, output exactly: READY_TO_SHIP\n"
        "Otherwise describe what is missing."
    ).format(output_dir=output_dir)

    gen_result = await generator_agent.run(FINAL_PROMPT)
    critic_result = await critic_agent.run(FINAL_PROMPT)

    gen_ready = "READY_TO_SHIP" in gen_result.data
    critic_ready = "READY_TO_SHIP" in critic_result.data

    if gen_ready and critic_ready:
        logger.info("Mutual agreement reached: READY_TO_SHIP")
    else:
        logger.warning(
            f"Final agreement: Generator={'READY' if gen_ready else 'NOT READY'}, "
            f"Critic={'READY' if critic_ready else 'NOT READY'}"
        )
```

### Success Criteria

#### Automated Verification
- [x] `python -m pytest tests/test_exit_criteria.py -v` passes
- [x] `sprint_passes` returns False when avg < 9.0
- [x] `sprint_passes` returns False when avg ≥ 9.0 but a Critical issue exists
- [x] `should_ping_pong_exit` returns True only when both conditions met
- [x] `mypy deep_architect/harness.py deep_architect/exit_criteria.py` passes

#### Manual Verification
- [ ] Dry-run with mock endpoints shows correct logging output
- [ ] Resume correctly skips already-completed sprints

**Implementation Note**: After verifying exit criteria logic with unit tests, confirm the harness loop structure is correct before wiring real LLM endpoints in Phase 5.

---

## Phase 5: Auto-Commit, Final Agreement, and Reproducibility

### Overview
Finalize git auto-commit logic, ensure JSON round logs are written, and validate resume works on a real interrupted run.

### Changes Required

#### 1. Update `deep_architect/git_ops.py` with better staging

```python
def git_commit(repo: git.Repo, message: str, paths: list[Path]) -> None:
    """Stage the given paths and create a commit. No-op if nothing changed."""
    str_paths = [str(p) for p in paths if p.exists()]
    if not str_paths:
        return
    repo.index.add(str_paths)
    # Also stage contracts and feedback written this round
    try:
        diff = repo.index.diff("HEAD")
        if diff or repo.untracked_files:
            repo.index.commit(message)
    except git.BadName:
        # Initial commit (no HEAD yet)
        repo.index.commit(message)
```

#### 2. Seed for reproducibility

Add a `seed` field to `HarnessProgress` and pass it consistently to ensure round logs are fully reproducible. Store the seed in `progress.json`.

```python
class HarnessProgress(BaseModel):
    ...
    seed: int = Field(default_factory=lambda: int(time.time()))
```

### Success Criteria

#### Automated Verification
- [x] `python -m pytest tests/test_git_ops.py -v` passes (uses a temp git repo)
- [ ] A commit is created after each generator pass in integration test
- [x] `progress.json` contains `seed` field

#### Manual Verification
- [ ] Interrupt a run mid-sprint, verify `--resume` picks up from the correct sprint
- [ ] Git log shows one commit per generator pass with correct message format

---

## Phase 6: Tests, Linting, and Packaging

### Overview
Complete unit test suite, run all linting and type checking, verify pip install, and document the package.

### Changes Required

#### 1. `tests/test_config.py`

```python
import tomllib
from pathlib import Path
import pytest
from deep_architect.config import load_config, HarnessConfig

def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config(tmp_path / "nonexistent.toml")

def test_load_config_valid(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
base_url = "http://gen.example.com/v1"
api_key  = "gen-key"
model    = "nemotron"

[critic]
base_url = "http://crit.example.com/v1"
api_key  = "crit-key"
model    = "qwen"
""")
    cfg = load_config(cfg_file)
    assert cfg.generator.model == "nemotron"
    assert cfg.thresholds.min_score == 9.0

def test_load_config_custom_thresholds(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
base_url = "http://gen" 
api_key = "k"
model = "m"
[critic]
base_url = "http://crit"
api_key = "k"
model = "m"
[thresholds]
min_score = 8.5
max_rounds_per_sprint = 4
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.min_score == 8.5
    assert cfg.thresholds.max_rounds_per_sprint == 4
```

#### 2. `tests/test_exit_criteria.py`

```python
from deep_architect.exit_criteria import sprint_passes, should_ping_pong_exit
from deep_architect.models.feedback import CriticResult, CriterionScore


def make_result(avg: float, severities: list[str]) -> CriticResult:
    feedback = [
        CriterionScore(criterion=f"c{i}", score=avg, severity=s, details="test")
        for i, s in enumerate(severities)
    ]
    return CriticResult(scores={}, feedback=feedback, overall_summary="test")


def test_passes_when_high_score_no_critical():
    r = make_result(9.5, ["Medium", "Low"])
    assert sprint_passes(r, 9.0) is True


def test_fails_when_score_below_threshold():
    r = make_result(8.9, ["Medium"])
    assert sprint_passes(r, 9.0) is False


def test_fails_when_critical_even_high_score():
    r = make_result(9.5, ["Critical"])
    assert sprint_passes(r, 9.0) is False


def test_fails_when_high_severity():
    r = make_result(9.5, ["High"])
    assert sprint_passes(r, 9.0) is False


def test_ping_pong_exit_both_conditions():
    curr = make_result(9.0, ["Medium"])
    prev = make_result(8.95, ["Medium"])
    assert should_ping_pong_exit(0.90, curr, prev, 0.85) is True


def test_no_ping_pong_when_improving():
    curr = make_result(9.5, ["Low"])
    prev = make_result(8.0, ["Medium"])
    assert should_ping_pong_exit(0.90, curr, prev, 0.85) is False  # score improved > 0.1
```

#### 3. `tests/test_models.py`

```python
import pytest
from deep_architect.models.feedback import CriticResult, CriterionScore
from deep_architect.models.contract import SprintContract, SprintCriterion


def test_critic_result_passed_computed():
    feedback = [
        CriterionScore(criterion="c1", score=9.5, severity="Low", details="ok"),
        CriterionScore(criterion="c2", score=9.2, severity="Medium", details="ok"),
    ]
    r = CriticResult(scores={"c1": 9.5, "c2": 9.2}, feedback=feedback, overall_summary="good")
    assert r.passed is True
    assert abs(r.average_score - 9.35) < 0.01


def test_critic_result_fails_on_critical():
    feedback = [
        CriterionScore(criterion="c1", score=9.5, severity="Critical", details="bad"),
    ]
    r = CriticResult(scores={}, feedback=feedback, overall_summary="bad")
    assert r.passed is False


def test_sprint_contract_validation():
    with pytest.raises(Exception):
        # criteria must have at least 3 items
        SprintContract(
            sprint_number=1, sprint_name="test",
            files_to_produce=["c1-context.md"],
            criteria=[SprintCriterion(name="c1", description="d")],
        )
```

#### 4. `tests/test_git_ops.py`

```python
import pytest
import git
from pathlib import Path
from deep_architect.git_ops import validate_git_repo, git_commit


def test_validate_git_repo_success(tmp_path):
    repo = git.Repo.init(tmp_path)
    result = validate_git_repo(tmp_path)
    assert result is not None


def test_validate_git_repo_fails_outside_git(tmp_path):
    with pytest.raises(SystemExit, match="not inside a git repository"):
        validate_git_repo(tmp_path)


def test_git_commit_creates_commit(tmp_path):
    repo = git.Repo.init(tmp_path)
    # Initial empty commit
    repo.index.commit("init")
    
    test_file = tmp_path / "test.md"
    test_file.write_text("# Test")
    git_commit(repo, "test commit", [test_file])
    
    assert repo.head.commit.message == "test commit"
```

#### 5. `README.md` updates

Document:
- Installation: `pip install git+https://github.com/geraldthewes/deep-architect`
- Config file format (`~/.deep-architect.toml`)
- All CLI flags
- ENV vars used (none — config is via TOML only)
- How to add to a new BMAD repo

### Success Criteria

#### Automated Verification
- [x] `python -m pytest tests/ -v` — all tests pass
- [x] `ruff check deep_architect/ tests/` — zero errors
- [x] `mypy deep_architect/` — zero errors
- [x] `bandit -r deep_architect/ -ll` — zero medium/high issues
- [x] `pip install -e .` succeeds in a fresh virtualenv
- [x] `adversarial-architect --help` works after fresh install

#### Manual Verification
- [ ] End-to-end test run on llm-switch PRD produces files in `knowledge/architecture/`
- [ ] All Mermaid diagrams render correctly in GitHub
- [ ] Run completes without human intervention
- [ ] Git log shows one commit per generator pass

---

## Testing Strategy

### Unit Tests
- `test_config.py`: Config loading, validation, missing file error
- `test_exit_criteria.py`: All exit condition combinations
- `test_models.py`: Pydantic validators, computed fields, edge cases
- `test_git_ops.py`: Repo validation, commit creation (uses tmp git repo)
- `test_files.py`: Round-trip serialize/deserialize for all models

### Integration Test
- Single-sprint dry run against a mock OpenAI-compatible server (e.g. `pytest-httpserver`) to verify the full loop without real LLM calls

### Manual Testing
1. Install in fresh venv: `pip install -e .`
2. Create `~/.deep-architect.toml` with real endpoints
3. Run against `llm-switch` PRD
4. Verify git commits appear after each generator pass
5. Verify `knowledge/architecture/` folder structure matches spec
6. Check Mermaid diagrams render on GitHub

## Performance Considerations

- LLM calls are the bottleneck. Each round involves 2 calls (Generator + Critic). 7 sprints × ~3 rounds × 2 = ~42 LLM calls typical.
- 3-hour timeout is a hard ceiling. At 2-4 minutes per LLM call, this is conservative.
- File I/O is trivial.

## References

- Original ticket: `knowledge/tickets/PROJ-0001.md`
- PRD: `docs/prd.md`
- Reference implementation: `https://github.com/coleam00/adversarial-dev` (cloned to `/tmp/adversarial-dev`)
- BMAD create-architecture skill: `/home/gerald/repos/llm-switch/_bmad/bmm/3-solutioning/bmad-create-architecture/`
- pydantic-ai docs: `https://docs.pydantic.dev/latest/concepts/agents/`
