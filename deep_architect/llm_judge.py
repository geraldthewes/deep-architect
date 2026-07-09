from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import git
import pathspec

from deep_architect.agents.client import _extract_json
from deep_architect.coding_agents.base import CodingAgent
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


async def _judge_with_retries(
    agent: CodingAgent,
    system_prompt: str,
    prompt: str,
    label: str,
    max_retries: int,
) -> StyleVerdict:
    """Run the judge prompt via the coding agent's CLI, parsing JSON with retries.

    The CLIs (opencode/grok/claude) can't enforce a JSON schema server-side, so
    schema validation happens here, with the same call retried on parse failure.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 2):
        try:
            raw = await agent.run_structured(system_prompt, prompt, label=label)
            return StyleVerdict.model_validate_json(_extract_json(raw))
        except Exception as exc:  # noqa: BLE001 - broad by design, retried below
            last_exc = exc
            if attempt <= max_retries:
                logger.warning(
                    "[%s] attempt %d/%d failed (%s) — retrying",
                    label, attempt, max_retries + 1, exc,
                )
    raise RuntimeError(
        f"[{label}] structured verdict failed after {max_retries + 1} attempts"
    ) from last_exc


async def judge_file(
    file: Path,
    diff: str,
    rules: list[RuleEntry],
    agent: CodingAgent,
    repo_root: Path,
    max_parse_retries: int = 2,
) -> StyleVerdict:
    """Judge the diff against the concatenated rules via the active coding agent."""
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

    schema_str = json.dumps(StyleVerdict.model_json_schema(), indent=2)
    system_prompt = (
        load_prompt("llm_judge_system")
        + "\n\n## Output Format\n\n"
        "Respond with ONLY a valid JSON object matching this schema — "
        "no markdown, no explanation, no code fences:\n\n"
        f"{schema_str}"
    )
    prompt = (
        f"## File: {rel.as_posix()}\n\n"
        "## Diff (uncommitted changes)\n"
        f"```diff\n{diff}\n```\n\n"
        "## Full file content (context only, truncated to "
        f"{_FILE_CONTENT_LINE_CAP} lines)\n"
        f"```\n{truncated_content}\n```\n\n"
        f"## Applicable rules\n{rule_text}\n"
    )

    return await _judge_with_retries(
        agent,
        system_prompt,
        prompt,
        label=f"llm-judge:{file.name}",
        max_retries=max_parse_retries,
    )
