"""Unit tests for deep_architect.llm_judge."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from deep_architect.llm_judge import (
    RuleEntry,
    judge_file,
    load_llm_rules,
    rules_for_file,
)
from deep_architect.models.checks import (
    LLMRulesConfig,
    QualityChecksConfig,
    StyleVerdict,
    StyleViolation,
)


class FakeAgent:
    """Minimal CodingAgent stand-in exposing only run_structured."""

    def __init__(self) -> None:
        self.run_structured: AsyncMock = AsyncMock()

# ---------------------------------------------------------------------------
# load_llm_rules
# ---------------------------------------------------------------------------


class TestLoadLlmRules:

    def test_rule_json_happy_path(self, tmp_path: Path) -> None:
        ocr_dir = tmp_path / ".opencodereview"
        ocr_dir.mkdir()
        (ocr_dir / "rule.json").write_text(
            json.dumps(
                {
                    "rules": [
                        {"path": "**/*.py", "rule": "# Python rules\nUse type hints."},
                        {"path": "**/main.py", "rule": "# Entrypoint rules"},
                    ]
                }
            )
        )

        config = QualityChecksConfig(llm_rules=LLMRulesConfig())
        rules = load_llm_rules(tmp_path, config)

        assert len(rules) == 2
        assert rules[0].path_glob == "**/*.py"
        assert "type hints" in rules[0].rule_text

    def test_rule_json_bare_list_format(self, tmp_path: Path) -> None:
        ocr_dir = tmp_path / ".opencodereview"
        ocr_dir.mkdir()
        (ocr_dir / "rule.json").write_text(
            json.dumps([{"path": "**/*.py", "rule": "Some rule"}])
        )

        config = QualityChecksConfig(llm_rules=LLMRulesConfig())
        rules = load_llm_rules(tmp_path, config)

        assert len(rules) == 1
        assert rules[0].rule_text == "Some rule"

    def test_rules_markdown_fallback_nested_subdirs(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".opencodereview" / "rules"
        nested = rules_dir / "python-secure-coding"
        nested.mkdir(parents=True)
        (rules_dir / "python-style.md").write_text("# Style rules")
        (nested / "secure.md").write_text("# Secure coding rules")

        config = QualityChecksConfig()
        rules = load_llm_rules(tmp_path, config)

        assert len(rules) == 2
        assert all(r.path_glob == "**/*.py" for r in rules)
        texts = {r.rule_text for r in rules}
        assert "# Style rules" in texts
        assert "# Secure coding rules" in texts

    def test_neither_present_returns_empty(self, tmp_path: Path) -> None:
        config = QualityChecksConfig()
        assert load_llm_rules(tmp_path, config) == []

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        ocr_dir = tmp_path / ".opencodereview"
        ocr_dir.mkdir()
        (ocr_dir / "rule.json").write_text("not valid json {{{")

        config = QualityChecksConfig(llm_rules=LLMRulesConfig())
        with pytest.raises(ValueError, match="Malformed"):
            load_llm_rules(tmp_path, config)

    def test_custom_source_path(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom-rules.json"
        custom.write_text(json.dumps({"rules": [{"path": "**/*.py", "rule": "R"}]}))

        config = QualityChecksConfig(llm_rules=LLMRulesConfig(source="custom-rules.json"))
        rules = load_llm_rules(tmp_path, config)
        assert len(rules) == 1


# ---------------------------------------------------------------------------
# rules_for_file
# ---------------------------------------------------------------------------


class TestRulesForFile:

    def _rules(self) -> list[RuleEntry]:
        return [
            RuleEntry(path_glob="**/*.py", rule_text="general python rules"),
            RuleEntry(path_glob="**/main.py", rule_text="entrypoint rules"),
            RuleEntry(path_glob="**/tests/**/*.py", rule_text="test rules"),
        ]

    def test_generic_python_file_matches_general_only(self, tmp_path: Path) -> None:
        f = tmp_path / "src" / "app.py"
        matched = rules_for_file(self._rules(), f, tmp_path)
        assert [r.rule_text for r in matched] == ["general python rules"]

    def test_main_py_matches_general_and_entrypoint(self, tmp_path: Path) -> None:
        f = tmp_path / "src" / "main.py"
        matched = rules_for_file(self._rules(), f, tmp_path)
        texts = {r.rule_text for r in matched}
        assert texts == {"general python rules", "entrypoint rules"}

    def test_test_file_matches_general_and_test_rules(self, tmp_path: Path) -> None:
        f = tmp_path / "tests" / "test_app.py"
        matched = rules_for_file(self._rules(), f, tmp_path)
        texts = {r.rule_text for r in matched}
        assert texts == {"general python rules", "test rules"}

    def test_non_python_file_matches_nothing(self, tmp_path: Path) -> None:
        f = tmp_path / "src" / "app.ts"
        matched = rules_for_file(self._rules(), f, tmp_path)
        assert matched == []


# ---------------------------------------------------------------------------
# judge_file
# ---------------------------------------------------------------------------


class TestJudgeFile:

    async def test_no_rules_returns_empty_verdict_without_calling_llm(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        agent = FakeAgent()
        verdict = await judge_file(f, "diff content", [], agent, tmp_path)

        assert verdict.violations == []
        agent.run_structured.assert_not_awaited()

    async def test_prompt_contains_diff_and_rules_and_tool_config_wins_instruction(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("import os\ndef f():\n    return 1\n")
        rules = [RuleEntry(path_glob="**/*.py", rule_text="PY-STY-042: no bare except")]

        agent = FakeAgent()
        agent.run_structured.return_value = StyleVerdict().model_dump_json()
        await judge_file(f, "+import os\n-pass", rules, agent, tmp_path)

        assert agent.run_structured.await_count == 1
        args = agent.run_structured.call_args.args
        system_prompt, prompt = args[0], args[1]

        assert "+import os" in prompt
        assert "PY-STY-042" in prompt
        assert "tool" in system_prompt.lower() and "authoritative" in system_prompt.lower()
        assert "Output Format" in system_prompt

    async def test_verdict_passthrough(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        rules = [RuleEntry(path_glob="**/*.py", rule_text="rule text")]
        expected = StyleVerdict(
            violations=[
                StyleViolation(
                    rule_id="PY-STY-001", severity="MUST", description="bad thing", line=3
                )
            ]
        )

        agent = FakeAgent()
        agent.run_structured.return_value = expected.model_dump_json()
        verdict = await judge_file(f, "diff", rules, agent, tmp_path)

        assert verdict == expected

    async def test_code_fenced_json_is_extracted(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        rules = [RuleEntry(path_glob="**/*.py", rule_text="rule text")]
        expected = StyleVerdict(
            violations=[StyleViolation(rule_id="A", severity="MAY", description="d")]
        )

        agent = FakeAgent()
        agent.run_structured.return_value = f"```json\n{expected.model_dump_json()}\n```"
        verdict = await judge_file(f, "diff", rules, agent, tmp_path)

        assert verdict == expected

    async def test_malformed_then_valid_output_retries_and_succeeds(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        rules = [RuleEntry(path_glob="**/*.py", rule_text="rule text")]
        expected = StyleVerdict()

        agent = FakeAgent()
        agent.run_structured.side_effect = ["not json", expected.model_dump_json()]
        verdict = await judge_file(f, "diff", rules, agent, tmp_path)

        assert verdict == expected
        assert agent.run_structured.await_count == 2

    async def test_always_malformed_raises_after_max_attempts(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        rules = [RuleEntry(path_glob="**/*.py", rule_text="rule text")]

        agent = FakeAgent()
        agent.run_structured.return_value = "not json"
        with pytest.raises(RuntimeError, match="after 3 attempts"):
            await judge_file(f, "diff", rules, agent, tmp_path, max_parse_retries=2)

        assert agent.run_structured.await_count == 3

    async def test_run_structured_exception_then_success_is_retried(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        rules = [RuleEntry(path_glob="**/*.py", rule_text="rule text")]
        expected = StyleVerdict()

        agent = FakeAgent()
        agent.run_structured.side_effect = [
            RuntimeError("opencode timed out"),
            expected.model_dump_json(),
        ]
        verdict = await judge_file(f, "diff", rules, agent, tmp_path)

        assert verdict == expected
        assert agent.run_structured.await_count == 2

    def test_blocking_severity_gating(self) -> None:
        verdict = StyleVerdict(
            violations=[
                StyleViolation(rule_id="A", severity="MUST", description="d1"),
                StyleViolation(rule_id="B", severity="SHOULD", description="d2"),
                StyleViolation(rule_id="C", severity="MAY", description="d3"),
                StyleViolation(rule_id="D", severity="NIT", description="d4"),
            ]
        )
        blocking_ids = {v.rule_id for v in verdict.blocking}
        assert blocking_ids == {"A", "B"}

    def test_no_blocking_violations(self) -> None:
        verdict = StyleVerdict(
            violations=[StyleViolation(rule_id="C", severity="MAY", description="d3")]
        )
        assert verdict.blocking == []
