from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import git
import pathspec

from deep_architect.agents.client import run_simple_structured
from deep_architect.config import AgentConfig
from deep_architect.logger import get_logger
from deep_architect.models.checks import QualityChecksConfig, StyleVerdict
from deep_architect.prompts import load_prompt

logger = get_logger(__name__)

# Full file content is included for context, capped to keep the prompt bounded.
_FILE_CONTENT_LINE_CAP = 2000


@dataclass
class RuleEntry:
    path_glob: str
    rule_text: str


def load_llm_rules(repo_root: Path, config: QualityChecksConfig) -> list[RuleEntry]:
    """Load rule.json entries; fall back to rules/*.md mapped to **/*.py; [] if neither."""
    source = config.llm_rules.source if config.llm_rules else ".opencodereview/rule.json"
    rule_json_path = repo_root / source
    if rule_json_path.exists():
        return _load_rule_json(rule_json_path)

    rules_dir = repo_root / ".opencodereview" / "rules"
    if rules_dir.exists():
        entries = _load_rules_markdown(rules_dir)
        if entries:
            return entries

    logger.info(
        "No LLM-judged rules found under %s (%s or .opencodereview/rules/*.md) — "
        "nothing to enforce",
        repo_root, source,
    )
    return []


def _load_rule_json(path: Path) -> list[RuleEntry]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed {path}: {exc}") from exc

    # generate-rules.py emits {"rules": [...]}; also accept a bare list.
    entries = raw if isinstance(raw, list) else raw.get("rules", [])
    try:
        return [RuleEntry(path_glob=e["path"], rule_text=e["rule"]) for e in entries]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Malformed rule entries in {path}: {exc}") from exc


def _load_rules_markdown(rules_dir: Path) -> list[RuleEntry]:
    return [
        RuleEntry(path_glob="**/*.py", rule_text=md_file.read_text(encoding="utf-8"))
        for md_file in sorted(rules_dir.rglob("*.md"))
    ]


def rules_for_file(rules: list[RuleEntry], file: Path, repo_root: Path) -> list[RuleEntry]:
    """pathspec gitwildmatch match of a repo-relative file against rule globs."""
    try:
        rel = file.resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel = file

    matched: list[RuleEntry] = []
    for rule in rules:
        spec = pathspec.PathSpec.from_lines("gitignore", [rule.path_glob])
        if spec.match_file(rel.as_posix()):
            matched.append(rule)
    return matched


def git_diff_for_file(repo: git.Repo, file: Path) -> str:
    """Return the uncommitted diff for a single file (working tree vs HEAD)."""
    working_dir = Path(repo.working_dir)
    try:
        rel = file.resolve().relative_to(working_dir.resolve())
    except ValueError:
        rel = file
    return str(repo.git.diff(None, "--", str(rel)))


async def judge_file(
    file: Path,
    diff: str,
    rules: list[RuleEntry],
    agent_config: AgentConfig,
    repo_root: Path,
) -> StyleVerdict:
    """One run_simple_structured call judging the diff against the concatenated rules."""
    if not rules:
        return StyleVerdict()

    try:
        rel = file.resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel = file

    try:
        content = file.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s for LLM judgment: %s", file, exc)
        content = ""
    truncated_content = "\n".join(content.splitlines()[:_FILE_CONTENT_LINE_CAP])

    rule_text = "\n\n---\n\n".join(r.rule_text for r in rules)

    system_prompt = load_prompt("llm_judge_system")
    prompt = (
        f"## File: {rel.as_posix()}\n\n"
        "## Diff (uncommitted changes)\n"
        f"```diff\n{diff}\n```\n\n"
        "## Full file content (context only, truncated to "
        f"{_FILE_CONTENT_LINE_CAP} lines)\n"
        f"```\n{truncated_content}\n```\n\n"
        f"## Applicable rules\n{rule_text}\n"
    )

    return await run_simple_structured(
        agent_config,
        system_prompt,
        prompt,
        StyleVerdict,
        label=f"llm-judge:{file.name}",
    )
