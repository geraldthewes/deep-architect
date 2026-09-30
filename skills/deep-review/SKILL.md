---
name: deep-review
description: Run the deep-architect code-review pipeline — review-driver (OCR → review-analyzer → review-action loop), or review-analyzer and review-action individually — and report the results. Use when the user asks to "run the review loop", "run review-driver", "triage OCR findings", "analyze code-review.json", "apply review fixes", or summarize a previous review run.
allowed-tools: Bash Read Grep Glob
argument-hint: "[driver | analyze <ocr.json> | action <feedback-dir> | report]"
---

# Deep Review

Drive the deep-architect review tools against the **target application repo** (not
deep-architect itself). Always run from the target repo root.

| Tool | Does |
|---|---|
| `review-driver` | Unattended loop: `ocr review` → `review-analyzer` → `review-action`, until high/medium `VALID` findings are 0 for K consecutive passes or `--max-passes` is hit |
| `review-analyzer` | Triage an OCR JSON file: `VALID` / `REJECTED` / `BACKLOG` / `TIMEOUT` / `DUPLICATE` |
| `review-action` | Apply fixes for `VALID` findings, run the repo's quality checks, **commit each fix** |
| `review-feedback-browse` | View-only TUI over a feedback dir |

`review-action` and `review-driver` create git commits in the target repo. Get explicit user
approval before running either, and show the exact command first.

## 1. Preflight

```bash
command -v review-driver review-analyzer review-action ocr
ls "${OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
git rev-parse --show-toplevel && git branch --show-current
git status --porcelain --untracked-files=no
grep -qxF '.review-runs/' .gitignore && echo gitignored || echo "NOT gitignored"
```

| Failure | Fix |
|---|---|
| Tools missing | `cd ~/repos/deep-architect && just install` |
| `ocr` missing | Install OpenCodeReview, or set `OCR_BIN` |
| opencode binary missing | `export OPENCODE_BIN=$(which opencode)` |
| Tracked files dirty | Commit or stash first — the driver refuses a dirty tracked tree (untracked is fine) |
| `.review-runs/` not gitignored | Warn and offer to add it; do not edit `.gitignore` without asking |

The driver does not check out branches: `HEAD` must already be the `--source` branch.

## 2a. Driver (default)

```bash
review-driver --source <current-branch> --target main --no-tui
```

Run with the Bash tool using `run_in_background: true` and `timeout: 7200000`. Do not poll —
wait for the completion notification.

Useful options (ask only when relevant):
- `--max-passes N` (default 5; `0` = unlimited), `--zero-novelty-passes K` (default 2)
- `--provider opencode|claude|grok`, `--model NAME` — passed to `review-action`
- `--exclude GLOB` (repeatable) — e.g. generated API clients
- `--no-resume` — start a new timestamped run (default resumes a stopped run for this branch)
- `--ocr-timeout` is **minutes**; `--ocr-llm-timeout` and analyzer timeouts are **seconds**

**Change intent.** If the branch name contains `PROJ-####`, the driver loads
`knowledge/tickets/PROJ-####.md` automatically. Otherwise offer `--background "<why>"` or
`--background-file <path>`. Under Claude stdin is not a TTY, so an intent over 8000 characters
is auto-summarized without asking — tell the user, and point them at `intent-source.md` /
`intent.md` in the run dir.

## 2b. Manual pipeline

1. **Analyze** (no commits, safe to run after a go-ahead on cost):
   ```bash
   review-analyzer <ocr.json> --output-dir feedback/ --no-tui
   ```
   Add `--intent-file <ticket.md>` for change intent, `--prior-feedback <dir>` (repeatable) for
   multi-pass memory, `--retry-timeouts` to re-triage only `TIMEOUT` findings. `BACKLOG`
   findings are promoted into `knowledge/backlog/` by default (`--no-write-backlog` to disable).
2. Summarize `feedback/SUMMARY.md` (verdict and severity counts) and the `VALID` rows of
   `feedback/INDEX.md`.
3. **Act** — offer a dry run first, then ask before the real run (it commits):
   ```bash
   review-action feedback/ --dry-run --no-tui
   review-action feedback/ --no-tui [--provider claude --model sonnet] [--min-severity medium]
   ```
   Other flags: `--force` (reprocess done findings), `--skip-errors`, `--skip-llm-checks`,
   `--max-check-iterations N`.
4. Summarize the **last** `<!-- review-action-run: ... -->` block of
   `feedback/review-action_summary.md` (the file accumulates one block per run).

## 3. Report

After a driver run, find the run dir and read its report:

```bash
root=.review-runs; ls "$root"             # one folder per source branch (target appended if not main)
cat "$root/<source-folder>/LATEST"         # run id
# then read $root/<source-folder>/<run-id>/REPORT.md and progress.json
```

Report:
- Stop reason from `progress.json` `status`: `converged`, `max_passes`, `failed`, or `running` (interrupted)
- Exit code: `0` converged with no action errors · `1` preflight/step failure, max passes with
  novelty left, or any action errors · `130` interrupted (rerun resumes)
- Fix commits: `git log --grep='Generated-by: deep-architect review-action' --oneline main..HEAD`
- Errors and `TIMEOUT` counts per pass; suggest `review-analyzer ... --retry-timeouts` (or a
  higher `--timeout`) for timeouts
- Per-pass logs are in `<run-dir>/logs/rN-{ocr,analyzer,action}.log`

To browse interactively, the user runs the TUI themselves:
`! review-feedback-browse <run-dir>/feedback-rN/` (or `feedback/`).

## Quality checks

`review-action` runs the target repo's own checks before committing each fix: `.quality-checks.toml`
at the repo root, or auto-detected ruff/mypy/black/bandit from `pyproject.toml`. **Test commands
are never auto-detected** — if the repo has no `.quality-checks.toml`, tell the user that fixes are
committed without running tests, and point to `~/repos/deep-architect/.quality-checks.toml.template`.
LLM style rules come from `.opencodereview/rule.json` or `.opencodereview/rules/*.md` if present.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Driver refuses to start: dirty tree | Commit/stash tracked changes |
| `HEAD` is not `--source` | Check out the branch first |
| Resume state is for a different source/target | `--no-resume` |
| Many `TIMEOUT` verdicts | `--retry-timeouts`, raise `--timeout` or lower `--concurrency` |
| OCR killed / `context deadline exceeded` | Raise `--ocr-timeout` (minutes) or `--ocr-llm-timeout` (seconds) |
| `grok binary not found` | Install Grok Build CLI or set `GROK_BIN` |
| Finding marked `error` | Quality checks kept failing after `check_max_fix_iterations`; fix was restored. Rerun without `--skip-errors` to retry |

Full reference: `~/repos/deep-architect/README.md` (Review Analyzer, Review Action Harness,
Review Driver sections).
