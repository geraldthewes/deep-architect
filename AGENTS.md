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