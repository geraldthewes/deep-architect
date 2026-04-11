# CLAUDE.md — deep-researcher

## Project Overview

`deep-researcher` is a standalone Python package (`adversarial-architect` CLI) that takes a BMAD PRD and autonomously produces a hardened C4 architecture document using a Generator ↔ Critic adversarial loop across 7 fixed sprints. Output lands in `knowledge/architecture/` as Mermaid + Markdown files.

## Package Layout

```
deep_researcher/
├── cli.py            # Typer entry point (adversarial-architect)
├── config.py         # HarnessConfig loaded from ~/.deep-researcher.toml
├── harness.py        # Main orchestration loop (run_harness)
├── sprints.py        # SPRINTS list — 7 fixed SprintDefinition objects
├── exit_criteria.py  # sprint_passes(), should_ping_pong_exit()
├── git_ops.py        # validate_git_repo(), git_commit(), get_modified_files()
├── logger.py         # setup_logging(), get_logger()
├── agents/
│   ├── client.py     # make_agent_options(), run_agent(), run_agent_text(), run_agent_structured()
│   ├── generator.py  # run_generator(), propose_contract()
│   └── critic.py     # run_critic(), review_contract(), check_ping_pong()
├── models/
│   ├── contract.py   # SprintContract, SprintCriterion
│   ├── feedback.py   # CriticResult, CriterionScore, PingPongResult
│   └── progress.py   # HarnessProgress, SprintStatus
├── io/
│   └── files.py      # save/load for contracts, feedback, progress; init_workspace()
└── prompts/
    ├── __init__.py   # load_prompt(name, **kwargs) — loads .md files at runtime
    └── *.md          # 13 prompt templates (generator_system, critic_system, sprints 1-7, etc.)
```

## Development Commands

All commands run inside a `uv` virtual environment — never use the global Python.

```bash
# Install (editable + dev deps)
uv sync

# Test
uv run python -m pytest tests/ -v

# Lint
uv run ruff check deep_researcher/ tests/

# Type check (strict)
uv run mypy deep_researcher/

# Security scan
uv run bandit -r deep_researcher/ -ll
```

All four must pass clean before committing. Run them together:

```bash
uv run ruff check deep_researcher/ tests/ && uv run mypy deep_researcher/ && uv run python -m pytest tests/ -v && uv run bandit -r deep_researcher/ -ll
```

## Architecture Decision Records

All architectural decisions are documented in `knowledge/adr/`. **Review the ADR index (`knowledge/adr/README.md`) before designing any new feature or making structural changes.** Each ADR captures the context, decision, rationale, and consequences for a specific aspect of the system. Superseded ADRs (e.g. ADR-004) are retained for historical context but must not be treated as active guidance.

## Key Architectural Decisions

**claude-agent-sdk** — agents run via the Claude Code CLI as a real agentic loop with tool use.
- Python import: `claude_agent_sdk`
- Core API: `query(prompt, options)` returns an async generator of messages
- Options built via `ClaudeAgentOptions` (permission_mode, allowed_tools, model, max_turns, cwd, etc.)
- Final result is a `ResultMessage` with `.result` (text) and `.structured_output` (parsed JSON)

**System-installed CLI binary** — always use `cli_path=shutil.which("claude")` (done automatically in `client.py`). The bundled SDK binary may ignore `ANTHROPIC_BASE_URL`; the system-installed binary respects all env vars.

**Generator writes files directly via tools** — `run_generator()` gives the agent `["Read", "Write", "Edit", "Bash", "Glob", "Grep"]`. The agent writes architecture files to disk using the Write tool. The harness detects written files via `get_modified_files()` (git status) after the agent finishes. Each generator round starts a **fresh session** — `session_id` is never reused across rounds (ADR-021). Prior-round context is carried forward via two files: `generator-history.md` (harness-written, structured per-round record of files changed and feedback addressed) and `generator-learnings.md` (agent-written free-form working memory, fully injected into the next prompt). Both files survive crashes; `--resume` loads them automatically with no special-case logic.

**Critic reads files via tools** — `run_critic()` gives the agent `["Read", "Bash", "Glob", "Grep"]` (no Write/Edit). The agent inspects files directly rather than having content pasted into the prompt. Structured output is obtained via `output_format` with a JSON schema derived from `CriticResult.model_json_schema()`.

**Endpoint configuration is via environment variables** — not in the TOML config:
```bash
export ANTHROPIC_BASE_URL=http://litellm.cluster:9999
export ANTHROPIC_AUTH_TOKEN=$(vault kv get -field=master_key secret/litellm/app)
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-standard
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-standard
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

**Prompts are `.md` files** — loaded at runtime via `load_prompt(name, **kwargs)`. Edit prompts without touching Python. All 13 prompt files must exist or `test_prompts.py` will fail.

**No framework** — no LangGraph, LangChain, CrewAI, etc. Orchestration is plain async Python in `harness.py`.

## Adding a New Sprint

1. Add a `SprintDefinition` entry to `SPRINTS` in `sprints.py`
2. Create `deep_researcher/prompts/sprint_N_name.md`
3. Add the prompt name to `EXPECTED_PROMPTS` in `tests/test_prompts.py`
4. Update the sprint count references in `harness.py` (progress total) and `README.md`

## Modifying Exit Criteria

Logic lives entirely in `exit_criteria.py`. The thresholds (`min_score`, `ping_pong_similarity_threshold`, etc.) come from `HarnessConfig.thresholds` — never hardcode them. Unit tests for all combinations are in `tests/test_exit_criteria.py`.

## Config File

Users create `~/.deep-researcher.toml` (template: `.deep-researcher.toml.template`). The TOML config controls model aliases and thresholds only. Authentication and endpoint are set via environment variables (see above). `load_config()` in `config.py` raises `FileNotFoundError` with a clear message if the file is missing.

## Testing Notes

- `test_git_ops.py` creates real temporary git repos via `git.Repo.init(tmp_path)`
- `test_files.py` tests full round-trip serialization for all Pydantic models
- No mocking of LLM calls — claude-agent-sdk calls are not tested end-to-end in unit tests
- `asyncio_mode = "auto"` is set in `pyproject.toml`; async tests need no decorator

## What NOT to Do

- Do not add LangGraph, LangChain, or any agent framework
- Do not add more than 7 sprints without a strong reason — they are fixed by design
- Do not hardcode threshold values; always read from `config.thresholds`
- Do not use `result.data`, `result.output`, or `result_type=` — those are the old pydantic-ai API
- Do not store endpoint URLs or API keys in the TOML config — use environment variables
- Do not pass file contents as prompt text to the critic — it has Read tools for that
- Do not swallow exceptions; log them and let them propagate
