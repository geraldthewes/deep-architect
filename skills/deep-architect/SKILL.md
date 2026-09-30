---
name: deep-architect
description: Generate or reverse-engineer a C4 architecture document with the adversarial-architect CLI (deep-architect). Use when the user asks to "generate architecture", "reverse-engineer the architecture", "run deep architect", "run adversarial-architect", resume or reset an architecture sprint, or check the status of an architecture run.
allowed-tools: Bash Read Grep Glob
argument-hint: "[--prd <file> | --codebase <dir>] [resume | status | reset N]"
---

# Deep Architect

Drive `adversarial-architect`: a Generator ↔ Critic loop across 7 fixed sprints that writes a
C4 architecture (Markdown + Mermaid) into `knowledge/architecture/`. Runs take hours and cost
LLM time, so run **one sprint at a time**, review it with the user, then continue.

Sprints: 1 C1 System Context · 2 C2 Container Overview · 3 Frontend · 4 Backend/Orchestration ·
5 Database + Knowledge Base · 6 Edge/Deployment/Observability · 7 ADRs + Cross-Cutting.
After sprint 7, one more `--resume` runs the final mutual-agreement round (`READY_TO_SHIP`).

## 1. Preflight

Run these read-only checks. Stop at the first failure and give the fix.

```bash
command -v adversarial-architect claude
ls ~/.config/deep-architect/config.toml ~/.deep-architect.toml 2>/dev/null
for v in ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY; do
  [ -n "${!v}" ] && echo "$v=set" || echo "$v=unset"
done
```

| Failure | Fix |
|---|---|
| `adversarial-architect` missing | `cd ~/repos/deep-architect && just install` |
| `claude` missing | Install Claude Code CLI |
| No config file | `mkdir -p ~/.config/deep-architect && cp ~/repos/deep-architect/config.toml.template ~/.config/deep-architect/config.toml` |
| Neither `ANTHROPIC_BASE_URL`+`ANTHROPIC_AUTH_TOKEN` nor `ANTHROPIC_API_KEY` set | Export them in the shell that launched Claude Code |

Never print the values of the auth variables — only whether they are set.

The output directory must be inside a git repository (every generator pass is auto-committed):
`git -C <output-parent> rev-parse --show-toplevel`.

## 2. Resolve the mode

| Argument | Mode | Command shape |
|---|---|---|
| A file (PRD) | Greenfield | `adversarial-architect --prd <file> --output knowledge/architecture` (`--output` is **required**) |
| A directory | Reverse-engineer | `adversarial-architect --codebase <abs-dir>` (output defaults to `<dir>/knowledge/architecture`) |
| `status` | Report only | Go to section 7 — do not launch anything |
| `resume` | Continue | Add `--resume` |
| `reset N` | Redo sprint N | Add `--reset-sprint N` |
| None | Ask | If `knowledge/prd.md` exists offer greenfield; otherwise offer reverse-engineering the current repo |

`--prd` and `--codebase` are mutually exclusive. Pass absolute paths for `--codebase`.

Optional flags to offer when relevant:
- `--context <file>` (repeatable) — tech-stack doc, security policy, or a top-level layout note for large repos.
- `--model-generator opus` / `--model-critic sonnet` — per-run model override.
- `--strict` — halt when a sprint can't meet exit criteria instead of accepting best effort.

Do **not** use `uv run adversarial-architect` from inside another project with a `pyproject.toml`;
call the installed binary directly.

## 3. Check for an existing run

The checkpoint is `<git-root>/.checkpoints/progress.json` (git root of the output dir).

**Important:** without `--resume`, the CLI calls an interactive `typer.confirm` when this file
exists. Under Claude stdin is not a TTY, so the prompt aborts the run. Always decide up front:

- Read `progress.json` and report `status`, `current_sprint`/`total_sprints`,
  `completed_sprints`, `total_rounds`.
- Ask the user (AskUserQuestion): **Resume** (`--resume`) / **Reset sprint N**
  (`--reset-sprint N`) / **Start fresh**.
- **Start fresh is destructive** — it deletes the checkpoint, contracts and feedback. Confirm
  explicitly, then either have the user run the command themselves with `! adversarial-architect ...`
  (answering the prompts), or pipe the answers: `printf 'n\ny\n' | adversarial-architect ...`.
  Never do this without that explicit confirmation.

## 4. Launch one sprint

Show the user the exact command and get a go-ahead before the first launch of a session.

Run it with the Bash tool using `run_in_background: true` and `timeout: 7200000`. Never run it in
the foreground. Do not add `--yolo` unless the user asked for an unattended run.

- The CLI stops by itself after each sprint and prints the files it wrote.
- Do not poll or sleep — wait for the background-task completion notification.
- The harness log is at `<output>/logs/architect-run-YYYYMMDD-HHMMSS.log` if you need detail.
- If the 2-hour tool limit is hit, the process gets SIGTERM, which the CLI turns into a clean
  interrupt. Relaunch the same command with `--resume`.

## 5. Review the sprint with the user

After the process exits:

1. Read `<git-root>/.checkpoints/progress.json`. For the sprint just run, report from
   `sprint_statuses[]`: `sprint_name`, `status` (`passed` / `accepted` = best-effort /
   `failed`), `rounds_completed`, `final_score`, `best_round`.
2. Read the latest critic feedback: `ls -v <output>/feedback/sprint-N-round-*.json | grep -v -- -log.json | tail -1`.
   It is a `CriticResult`: `average_score`, `passed`, `overall_summary`, and `feedback[]` with
   `criterion`, `score`, `severity` (Critical/High/Medium/Low), `details`.
3. List what changed: `git log --oneline -- <output>` and `git show --stat HEAD`.
4. Summarize concisely: sprint, outcome, score, rounds, files written, and any remaining
   Critical/High/Medium critic items (quote the `details` briefly).
5. **After sprint 1**, read `c1-context.md` and say whether the system boundary and external
   actors look right. A wrong boundary poisons every later sprint — this is the cheapest
   point to fix it.
6. Ask (AskUserQuestion):
   - **Continue** — relaunch with `--resume` (next sprint)
   - **Reset this sprint** — relaunch with `--reset-sprint N`
   - **Stop here** — user edits files first, then comes back with `resume`
   - **Run the rest unattended** — relaunch with `--resume --yolo`

## 6. Finish

When all 7 sprints are done, the next `--resume` runs the final mutual-agreement round. Report
whether both agents said `READY_TO_SHIP` and `progress.json` shows `status: complete`.

Suggest next steps: `/architecture_critique` on the result, then `/triage_critique` to turn
findings into tickets or backlog items.

## 7. Status only

For `status`, read `progress.json` and print a table of `sprint_statuses[]` (number, name,
status, rounds, score). Launch nothing.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Config file not found` | See preflight |
| `not inside a git repository` | `git init` the output repo |
| Run aborts right after "Prior run detected" | Missing `--resume` — see section 3 |
| Sprint exhausts max rounds | Default accepts best effort; inspect feedback JSON. Often the PRD is vague or `[generator] max_turns` is too low |
| Generator stops before writing all files | Raise `[generator] max_turns` in the config |
| Run stops on wall-clock timeout | `--resume`; raise `[thresholds] timeout_hours` |
| `ANTHROPIC_BASE_URL` ignored | Set `cli_path = "<output of which claude>"` in the config |
| "Command failed with exit code 1" mid-run | Hallucinated disallowed tool; harness retries automatically. Check the log for "unexpected tool call" |

Full reference: `~/repos/deep-architect/README.md`.
