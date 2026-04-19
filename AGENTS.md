<!-- OCR:START -->
## Open Code Review Instructions

These instructions are for AI assistants handling code review in this project.

Always open `.ocr/skills/SKILL.md` when the request:
- Asks for code review, PR review, or feedback on changes
- Mentions "review my code" or similar phrases
- Wants multi-perspective analysis of code quality
- Asks to map, organize, or navigate a large changeset

Use `.ocr/skills/SKILL.md` to learn:
- How to run the 8-phase review workflow
- How to generate a Code Review Map for large changesets
- Available reviewer personas and their focus areas
- Session management and output format

Keep this managed block so `ocr init` can refresh the instructions.

<!-- OCR:END -->

## Usage and Development

- **Installation**: Clone repo, then `uv sync` to install dependencies
- **Environment**: Set `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` (same as `claude` CLI)
- **Run tool**: `uv run adversarial-architect --help` for options
- **Required args**: `--prd <path> --output <path>` (PRD Markdown file and output directory)
- **Common options**: `--resume`, `--strict`, `--model-generator opus`, `--model-critic sonnet`, `--context <path>`
- **Run tests**: `pytest`
- **Lint**: `ruff check .`
- **Typecheck**: `mypy .`
- **Output**: Generated architecture files written to `knowledge/architecture/`
- **Git**: Tool auto-commits after each generator pass; work in a git repository
- **Config**: Optional `~/.deep-architect.toml` for harness configuration (see `.deep-architect.toml.template`)
- **Architecture**: Runs 7 fixed sprints with Generator (Winston) and Critic (Boris) agents in adversarial loop

## Circuit Breaker Pattern

The system implements a circuit breaker pattern with exponential backoff to handle transient failures in LLM provider communication:

- **Purpose**: Prevents repeated failed requests during temporary outages, rate limiting, or network issues
- **Configuration**: Adjustable via `~/.deep-architect.toml`:
  - `model_comm_failure_threshold`: Consecutive failures before opening circuit (default: 3)
  - `model_comm_base_backoff`: Initial backoff delay in seconds (default: 1.0)
  - `model_comm_max_backoff`: Maximum backoff delay in seconds (default: 60.0)
- **Behavior**:
  - Tracks consecutive failures for Generator and Critic separately
  - Implements exponential backoff with jitter for retry delays
  - Opens circuit after threshold failures, preventing further attempts
  - Automatically resets after successful requests
  - Handles transient errors (network issues, rate limits) vs permanent errors (invalid requests)
  - Logs detailed information when circuit breaker opens
  - Maintains backward compatibility when circuit breaker state is not provided