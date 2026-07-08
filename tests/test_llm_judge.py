"""Unit tests for deep_architect.llm_judge."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from deep_architect.config import AgentConfig
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

        with patch(
            "deep_architect.llm_judge.run_simple_structured", new_callable=AsyncMock
        ) as mock_call:
            verdict = await judge_file(f, "diff content", [], AgentConfig(), tmp_path)

        assert verdict.violations == []
        mock_call.assert_not_awaited()

    async def test_prompt_contains_diff_and_rules_and_tool_config_wins_instruction(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.py"
        f.write_text("import os\ndef f():\n    return 1\n")
        rules = [RuleEntry(path_glob="**/*.py", rule_text="PY-STY-042: no bare except")]

        with patch(
            "deep_architect.llm_judge.run_simple_structured", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = StyleVerdict()
            await judge_file(f, "+import os\n-pass", rules, AgentConfig(), tmp_path)

        assert mock_call.await_count == 1
        _, kwargs = mock_call.call_args
        args = mock_call.call_args.args
        system_prompt, prompt = args[1], args[2]

        assert "+import os" in prompt
        assert "PY-STY-042" in prompt
        assert "tool" in system_prompt.lower() and "authoritative" in system_prompt.lower()

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

        with patch(
            "deep_architect.llm_judge.run_simple_structured", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = expected
            verdict = await judge_file(f, "diff", rules, AgentConfig(), tmp_path)

        assert verdict == expected

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
