"""Unit tests for deep_architect.coding_agents."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deep_architect.coding_agents import (
    ClaudeSDKAgent,
    CodingAgent,
    CodingAgentConfig,
    GrokAgent,
    OpencodeAgent,
    create_agent,
)
from deep_architect.coding_agents.base import (
    _agent_reports_already_done,
    _file_reflects_fix,
    finding_already_satisfied,
)
from deep_architect.coding_agents.grok import _parse_grok_json
from deep_architect.coding_agents.opencode import _parse_opencode_ndjson

# ---------------------------------------------------------------------------
# opencode NDJSON fixtures / parser
# ---------------------------------------------------------------------------


def _opencode_success_ndjson(text: str = "ok") -> str:
    """Modern opencode 1.17+ completion stream (step_finish reason=stop)."""
    return "\n".join(
        [
            json.dumps({"type": "step_start", "timestamp": 1}),
            json.dumps(
                {"type": "text", "part": {"type": "text", "text": text}}
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "type": "step-finish",
                        "reason": "stop",
                        "messageID": "msg_test",
                    },
                }
            ),
        ]
    )


def _opencode_tool_then_stop_ndjson(text: str = "done") -> str:
    """Multi-step run: intermediate tool-calls then final stop."""
    return "\n".join(
        [
            json.dumps({"type": "step_start", "timestamp": 1}),
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool_use",
                        "state": {"status": "completed"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "type": "step-finish",
                        "reason": "tool-calls",
                    },
                }
            ),
            json.dumps({"type": "step_start", "timestamp": 2}),
            json.dumps(
                {"type": "text", "part": {"type": "text", "text": text}}
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "type": "step-finish",
                        "reason": "stop",
                    },
                }
            ),
        ]
    )


class TestParseOpencodeNdjson:

    def test_modern_step_finish_stop_is_success(self) -> None:
        ok, text = _parse_opencode_ndjson(_opencode_success_ndjson("hello"))
        assert ok is True
        assert text == "hello"

    def test_tool_calls_then_stop_is_success(self) -> None:
        ok, text = _parse_opencode_ndjson(_opencode_tool_then_stop_ndjson("fixed"))
        assert ok is True
        assert text == "fixed"

    def test_tool_calls_only_without_stop_is_failure(self) -> None:
        raw = "\n".join(
            [
                json.dumps({"type": "step_start"}),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"type": "step-finish", "reason": "tool-calls"},
                    }
                ),
            ]
        )
        ok, _ = _parse_opencode_ndjson(raw)
        assert ok is False

    def test_legacy_result_event_still_accepted(self) -> None:
        raw = json.dumps({"type": "result", "is_error": False, "result": "ok"})
        ok, _ = _parse_opencode_ndjson(raw)
        assert ok is True

    def test_legacy_result_error_is_failure(self) -> None:
        raw = json.dumps(
            {"type": "result", "is_error": True, "errors": ["boom"]}
        )
        ok, _ = _parse_opencode_ndjson(raw)
        assert ok is False

    def test_error_event_is_failure(self) -> None:
        raw = "\n".join(
            [
                json.dumps(
                    {"type": "error", "error": {"message": "provider down"}}
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"type": "step-finish", "reason": "stop"},
                    }
                ),
            ]
        )
        ok, _ = _parse_opencode_ndjson(raw)
        assert ok is False

    def test_empty_stdout_is_failure(self) -> None:
        ok, text = _parse_opencode_ndjson("")
        assert ok is False
        assert text is None


# ---------------------------------------------------------------------------
# OpencodeAgent
# ---------------------------------------------------------------------------


class TestOpencodeAgent:

    def test_default_init(self) -> None:
        agent = OpencodeAgent()
        assert agent.model == "standard/coder"
        assert "opencode" in agent.opencode_bin
        assert agent.timeout_seconds == 120.0

    def test_custom_model(self) -> None:
        agent = OpencodeAgent(model="custom/model")
        assert agent.model == "custom/model"

    def test_custom_bin(self) -> None:
        agent = OpencodeAgent(opencode_bin="/custom/path")
        assert agent.opencode_bin == "/custom/path"

    def test_custom_timeout(self) -> None:
        agent = OpencodeAgent(timeout_seconds=42.0)
        assert agent.timeout_seconds == 42.0

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_timeout_passed_to_subprocess(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=_opencode_success_ndjson(), stderr=""
        )

        agent = OpencodeAgent(timeout_seconds=42.0)
        await agent.apply_fix(Path("test.py"), "old code", "new code")

        assert mock_run.call_args.kwargs["timeout"] == 42.0

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_apply_fix_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=_opencode_success_ndjson(), stderr=""
        )

        agent = OpencodeAgent()
        result = await agent.apply_fix(
            Path("test.py"), "old code", "new code", "context"
        )

        assert result is True
        mock_run.assert_called_once()

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_apply_fix_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error occurred"
        )

        agent = OpencodeAgent()
        result = await agent.apply_fix(
            Path("test.py"), "old code", "new code"
        )

        assert result is False

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_apply_fix_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="opencode", timeout=120
        )

        agent = OpencodeAgent()
        result = await agent.apply_fix(
            Path("test.py"), "old code", "new code"
        )

        assert result is False

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_apply_fix_binary_not_found(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.side_effect = FileNotFoundError()

        agent = OpencodeAgent(opencode_bin="/nonexistent/bin")
        result = await agent.apply_fix(
            Path("test.py"), "old code", "new code"
        )

        assert result is False

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_fix_check_failures_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=_opencode_success_ndjson(), stderr=""
        )

        agent = OpencodeAgent()
        result = await agent.fix_check_failures(
            [Path("test.py")], "## Programmatic check failures\n\nruff: E501", "context"
        )

        assert result is True
        mock_run.assert_called_once()

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_fix_check_failures_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

        agent = OpencodeAgent()
        result = await agent.fix_check_failures([Path("test.py")], "failure report")

        assert result is False

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_fix_check_failures_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="opencode", timeout=120)

        agent = OpencodeAgent()
        result = await agent.fix_check_failures([Path("test.py")], "failure report")

        assert result is False

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_fix_check_failures_binary_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError()

        agent = OpencodeAgent(opencode_bin="/nonexistent/bin")
        result = await agent.fix_check_failures([Path("test.py")], "failure report")

        assert result is False

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_run_structured_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=_opencode_success_ndjson('{"ok": true}'),
            stderr="",
        )

        agent = OpencodeAgent(timeout_seconds=42.0)
        result = await agent.run_structured("system", "prompt", label="test")

        assert result == '{"ok": true}'
        assert mock_run.call_args.kwargs["timeout"] == 42.0

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_run_structured_cli_error_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

        agent = OpencodeAgent()
        with pytest.raises(RuntimeError, match="opencode structured run failed"):
            await agent.run_structured("system", "prompt", label="test")

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_run_structured_empty_text_raises(self, mock_run: MagicMock) -> None:
        # Completed with stop but no text payload
        ndjson = json.dumps(
            {
                "type": "step_finish",
                "part": {"type": "step-finish", "reason": "stop"},
            }
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=ndjson, stderr="")

        agent = OpencodeAgent()
        with pytest.raises(RuntimeError, match="no text output"):
            await agent.run_structured("system", "prompt", label="test")

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_run_structured_timeout_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="opencode", timeout=120)

        agent = OpencodeAgent()
        with pytest.raises(RuntimeError, match="timed out"):
            await agent.run_structured("system", "prompt", label="test")

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_run_structured_binary_not_found_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError()

        agent = OpencodeAgent(opencode_bin="/nonexistent/bin")
        with pytest.raises(RuntimeError, match="opencode binary not found"):
            await agent.run_structured("system", "prompt", label="test")


# ---------------------------------------------------------------------------
# _file_reflects_fix
# ---------------------------------------------------------------------------


class TestFileReflectsFix:

    def test_matches_suggested_code(self, tmp_path: Path) -> None:
        target = tmp_path / "f.py"
        target.write_text("new code\n", encoding="utf-8")
        assert _file_reflects_fix(target, "new code\n", "old code\n") is True

    def test_differs_from_original(self, tmp_path: Path) -> None:
        target = tmp_path / "f.py"
        target.write_text("something else\n", encoding="utf-8")
        assert _file_reflects_fix(target, "new code\n", "old code\n") is True

    def test_unchanged_from_original_returns_false(self, tmp_path: Path) -> None:
        target = tmp_path / "f.py"
        target.write_text("old code\n", encoding="utf-8")
        assert _file_reflects_fix(target, "new code\n", "old code\n") is False

    def test_unchanged_but_agent_reports_already_done(self, tmp_path: Path) -> None:
        target = tmp_path / "f.py"
        target.write_text("old code\n", encoding="utf-8")
        assert (
            _file_reflects_fix(
                target,
                "new code\n",
                "old code\n",
                agent_response_text=(
                    "Already fixed. The tests were renamed. No changes needed."
                ),
            )
            is True
        )

    def test_unchanged_but_finding_structurally_satisfied(
        self, tmp_path: Path
    ) -> None:
        # Suggested introduces a new test already present; existing anchor still
        # there — no disk delta required.
        body = (
            "def test_old():\n    pass\n\n"
            "def test_new_direct_ctor():\n    pass\n"
        )
        target = tmp_path / "f.py"
        target.write_text(body, encoding="utf-8")
        assert (
            _file_reflects_fix(
                target,
                "def test_old():\n    pass\n\ndef test_new_direct_ctor():\n    pass\n",
                body,
                existing_code="def test_old():\n    pass\n",
            )
            is True
        )

    def test_missing_file_trusts_agent(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.py"
        assert _file_reflects_fix(target, "new code\n", "old code\n") is True

    def test_no_original_content_trusts_agent_on_mismatch(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "f.py"
        target.write_text("something unexpected\n", encoding="utf-8")
        assert _file_reflects_fix(target, "new code\n", None) is True

    def test_empty_suggested_code_unchanged_file_returns_false(
        self, tmp_path: Path
    ) -> None:
        """suggested_code='' (derive-it-yourself findings) must not be treated
        as trivially satisfied by an empty/whitespace-only file."""
        target = tmp_path / "f.py"
        target.write_text("   \n", encoding="utf-8")
        assert _file_reflects_fix(target, "", "   \n") is False


class TestAgentReportsAlreadyDone:

    def test_common_phrases(self) -> None:
        assert _agent_reports_already_done("Already fixed. All tests pass.")
        assert _agent_reports_already_done("No changes needed — already applied.")
        assert _agent_reports_already_done(
            "The tests are already renamed to the recommended names."
        )
        assert _agent_reports_already_done("This feedback has already been addressed.")

    def test_negative_and_empty(self) -> None:
        assert _agent_reports_already_done(None) is False
        assert _agent_reports_already_done("") is False
        assert _agent_reports_already_done("I will apply the fix now.") is False
        assert _agent_reports_already_done("Done.") is False


# ---------------------------------------------------------------------------
# finding_already_satisfied
# ---------------------------------------------------------------------------


class TestFindingAlreadySatisfied:

    def test_normal_case_returns_none(self) -> None:
        file_content = "def foo():\n    pass\n"
        assert finding_already_satisfied(file_content, "def foo():", "def foo() -> None:") is None

    def test_already_applied_returns_reason(self) -> None:
        file_content = "def foo() -> None:\n    pass\n"
        reason = finding_already_satisfied(
            file_content, "def foo():", "def foo() -> None:"
        )
        assert reason is not None
        assert "already" in reason.lower()

    def test_stale_anchor_returns_reason(self) -> None:
        # Mirrors the real eedcebe3-159 case: __init__ was already rewritten
        # with parameters by a sibling finding, so the plain anchor is gone.
        file_content = (
            "class S3Service:\n"
            "    def __init__(\n"
            "        self,\n"
            "        endpoint_url: str | None,\n"
            "        bucket: str,\n"
            "    ) -> None:\n"
            "        pass\n"
        )
        reason = finding_already_satisfied(
            file_content, "def __init__(self):", "def __init__(self) -> None:"
        )
        assert reason is not None
        assert "stale" in reason.lower()

    def test_empty_existing_code_addition_never_stale(self) -> None:
        file_content = "def foo():\n    pass\n"
        assert finding_already_satisfied(file_content, "", "def bar():\n    pass\n") is None

    def test_new_def_from_suggested_already_in_file(self) -> None:
        # Suggested is a sketch (not an exact file match) but introduces a new
        # def that is already on disk.
        file_content = (
            "def test_invalid_death_reason(self):\n"
            "    assert True\n\n"
            "def test_invalid_death_reason_direct_ctor(self):\n"
            "    with pytest.raises(ValueError):\n"
            "        Plant(death_reason='nope')\n"
        )
        reason = finding_already_satisfied(
            file_content,
            "def test_invalid_death_reason(self):\n    assert True\n",
            (
                "def test_invalid_death_reason(self):\n    pass\n\n"
                "def test_invalid_death_reason_direct_ctor(self):\n"
                "    Plant(..., death_reason='x')  # or document if unsupported\n"
            ),
        )
        assert reason is not None
        assert "already" in reason.lower()
        assert "direct_ctor" in reason

    def test_placeholder_suggested_does_not_false_positive(self) -> None:
        # Sketch with `{ ... }` / ellipsis must not count as applied.
        file_content = "def test_old(self):\n    pass\n"
        reason = finding_already_satisfied(
            file_content,
            "def test_old(self):\n    pass\n",
            "def test_old(self):\n    Plant.create_from_dict({ ... })\n",
        )
        assert reason is None

    def test_indentation_only_difference_still_matches(self) -> None:
        file_content = "class C:\n    def foo():\n        pass\n"
        # Anchor written with different indentation than the file - should
        # still be found via the stripped-line comparison.
        anchor = "def foo():\n  pass"
        assert finding_already_satisfied(file_content, anchor, "def foo() -> None:") is None


# ---------------------------------------------------------------------------
# ClaudeSDKAgent
# ---------------------------------------------------------------------------


class TestClaudeSDKAgent:

    def test_default_init(self) -> None:
        agent = ClaudeSDKAgent()
        assert agent.model == "sonnet"
        assert agent.timeout_seconds == 300.0
        assert agent.max_turns == 30

    def test_custom_timeout(self) -> None:
        agent = ClaudeSDKAgent(timeout_seconds=42.0)
        assert agent.timeout_seconds == 42.0

    def test_custom_max_turns(self) -> None:
        agent = ClaudeSDKAgent(max_turns=50)
        assert agent.max_turns == 50

    @patch("deep_architect.agents.client.run_agent", new_callable=AsyncMock)
    @patch("deep_architect.agents.client.make_agent_options")
    async def test_apply_fix_success(
        self,
        mock_make_options: MagicMock,
        mock_run_agent: AsyncMock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "example.py"
        target.write_text("new code\n", encoding="utf-8")
        mock_make_options.return_value = MagicMock()
        mock_run_agent.return_value = MagicMock(is_error=False)

        agent = ClaudeSDKAgent()
        result = await agent.apply_fix(
            target,
            "old code",
            "new code",
            "context",
            original_content="old code\n",
        )

        assert result is True
        mock_run_agent.assert_awaited_once()

    @patch("deep_architect.agents.client.run_agent", new_callable=AsyncMock)
    @patch("deep_architect.agents.client.make_agent_options")
    async def test_apply_fix_agent_error(
        self,
        mock_make_options: MagicMock,
        mock_run_agent: AsyncMock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "example.py"
        target.write_text("old code\n", encoding="utf-8")
        mock_make_options.return_value = MagicMock()
        mock_run_agent.side_effect = RuntimeError("Agent query failed: boom")

        agent = ClaudeSDKAgent()
        result = await agent.apply_fix(
            target, "old code", "new code", "context"
        )

        assert result is False

    @patch("deep_architect.agents.client.run_agent", new_callable=AsyncMock)
    @patch("deep_architect.agents.client.make_agent_options")
    async def test_apply_fix_no_op_returns_false(
        self,
        mock_make_options: MagicMock,
        mock_run_agent: AsyncMock,
        tmp_path: Path,
    ) -> None:
        # run_agent reports success, but the file on disk was never touched and
        # the agent did not claim the fix was already present.
        target = tmp_path / "example.py"
        target.write_text("old code\n", encoding="utf-8")
        mock_make_options.return_value = MagicMock()
        mock_run_agent.return_value = MagicMock(
            is_error=False, result="I looked at the file."
        )

        agent = ClaudeSDKAgent()
        result = await agent.apply_fix(
            target,
            "old code",
            "new code",
            "context",
            original_content="old code\n",
        )

        assert result is False

    @patch("deep_architect.agents.client.run_agent", new_callable=AsyncMock)
    @patch("deep_architect.agents.client.make_agent_options")
    async def test_apply_fix_no_op_already_done_returns_true(
        self,
        mock_make_options: MagicMock,
        mock_run_agent: AsyncMock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "example.py"
        target.write_text("old code\n", encoding="utf-8")
        mock_make_options.return_value = MagicMock()
        mock_run_agent.return_value = MagicMock(
            is_error=False,
            result="Already fixed. No changes needed.",
        )

        agent = ClaudeSDKAgent()
        result = await agent.apply_fix(
            target,
            "old code",
            "new code",
            "context",
            original_content="old code\n",
        )

        assert result is True

    @patch("deep_architect.agents.client.run_agent", new_callable=AsyncMock)
    @patch("deep_architect.agents.client.make_agent_options")
    async def test_fix_check_failures_success(
        self,
        mock_make_options: MagicMock,
        mock_run_agent: AsyncMock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "example.py"
        target.write_text("code\n", encoding="utf-8")
        mock_make_options.return_value = MagicMock()
        mock_run_agent.return_value = MagicMock(is_error=False)

        agent = ClaudeSDKAgent()
        result = await agent.fix_check_failures(
            [target], "## Programmatic check failures\n\nruff: E501", "context"
        )

        assert result is True
        mock_run_agent.assert_awaited_once()

    @patch("deep_architect.agents.client.run_agent", new_callable=AsyncMock)
    @patch("deep_architect.agents.client.make_agent_options")
    async def test_fix_check_failures_agent_error(
        self,
        mock_make_options: MagicMock,
        mock_run_agent: AsyncMock,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "example.py"
        target.write_text("code\n", encoding="utf-8")
        mock_make_options.return_value = MagicMock()
        mock_run_agent.side_effect = RuntimeError("Agent query failed: boom")

        agent = ClaudeSDKAgent()
        result = await agent.fix_check_failures([target], "failure report")

        assert result is False

    @patch("deep_architect.agents.client.run_simple_text", new_callable=AsyncMock)
    async def test_run_structured_delegates_to_run_simple_text(
        self, mock_run_simple_text: AsyncMock
    ) -> None:
        mock_run_simple_text.return_value = "raw text result"

        agent = ClaudeSDKAgent(model="opus")
        result = await agent.run_structured("system", "prompt", label="test")

        assert result == "raw text result"
        mock_run_simple_text.assert_awaited_once()
        args, kwargs = mock_run_simple_text.call_args
        assert args[0].model == "opus"
        assert args[1] == "system"
        assert args[2] == "prompt"
        assert kwargs["label"] == "test"


# ---------------------------------------------------------------------------
# _parse_grok_json
# ---------------------------------------------------------------------------


class TestParseGrokJson:

    def test_success_object(self) -> None:
        stdout = json.dumps(
            {"text": "done", "stopReason": "EndTurn", "sessionId": "s", "requestId": "r"}
        )
        assert _parse_grok_json(0, stdout, "") == (True, "done")

    def test_error_object(self) -> None:
        stdout = json.dumps({"type": "error", "message": "boom"})
        assert _parse_grok_json(1, stdout, "") == (False, None)

    def test_exit_zero_non_json_trusts_exit_code(self) -> None:
        assert _parse_grok_json(0, "not json", "") == (True, None)

    def test_exit_one_empty_stdout_with_stderr(self) -> None:
        assert _parse_grok_json(1, "", "Error: something broke\n") == (False, None)


# ---------------------------------------------------------------------------
# GrokAgent
# ---------------------------------------------------------------------------


class TestGrokAgent:

    def test_default_init(self) -> None:
        agent = GrokAgent()
        assert agent.grok_bin == "grok"
        assert agent.model is None
        assert agent.timeout_seconds == 300.0
        assert agent.max_turns == 30

    def test_custom_max_turns(self) -> None:
        agent = GrokAgent(max_turns=50)
        assert agent.max_turns == 50

    def test_grok_bin_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROK_BIN", "/custom/grok")
        agent = GrokAgent()
        assert agent.grok_bin == "/custom/grok"

    def test_custom_model_in_argv(self) -> None:
        agent = GrokAgent(model="grok-build")
        cmd = agent._build_command("prompt.md")
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "grok-build"

    def test_no_model_omits_flag(self) -> None:
        agent = GrokAgent()
        cmd = agent._build_command("prompt.md")
        assert "-m" not in cmd

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_apply_fix_success(self, mock_run: MagicMock) -> None:
        stdout = json.dumps(
            {"text": "done", "stopReason": "EndTurn", "sessionId": "s", "requestId": "r"}
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")

        agent = GrokAgent()
        result = await agent.apply_fix(
            Path("test.py"), "old code", "new code", "context"
        )

        assert result is True
        mock_run.assert_called_once()

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_apply_fix_error_json(self, mock_run: MagicMock) -> None:
        stdout = json.dumps({"type": "error", "message": "boom"})
        mock_run.return_value = MagicMock(returncode=1, stdout=stdout, stderr="")

        agent = GrokAgent()
        result = await agent.apply_fix(Path("test.py"), "old code", "new code")

        assert result is False

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_apply_fix_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="grok", timeout=300)

        agent = GrokAgent()
        result = await agent.apply_fix(Path("test.py"), "old code", "new code")

        assert result is False

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_apply_fix_binary_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError()

        agent = GrokAgent(grok_bin="/nonexistent/grok")
        result = await agent.apply_fix(Path("test.py"), "old code", "new code")

        assert result is False

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_fix_check_failures_success(self, mock_run: MagicMock) -> None:
        stdout = json.dumps(
            {"text": "done", "stopReason": "EndTurn", "sessionId": "s", "requestId": "r"}
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")

        agent = GrokAgent()
        result = await agent.fix_check_failures(
            [Path("test.py")], "## Programmatic check failures\n\nruff: E501", "context"
        )

        assert result is True
        mock_run.assert_called_once()

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_fix_check_failures_failure(self, mock_run: MagicMock) -> None:
        stdout = json.dumps({"type": "error", "message": "boom"})
        mock_run.return_value = MagicMock(returncode=1, stdout=stdout, stderr="")

        agent = GrokAgent()
        result = await agent.fix_check_failures([Path("test.py")], "failure report")

        assert result is False

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_timeout_passed_to_subprocess(self, mock_run: MagicMock) -> None:
        stdout = json.dumps(
            {"text": "done", "stopReason": "EndTurn", "sessionId": "s", "requestId": "r"}
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")

        agent = GrokAgent(timeout_seconds=42.0)
        await agent.apply_fix(Path("test.py"), "old code", "new code")

        assert mock_run.call_args.kwargs["timeout"] == 42.0

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_run_structured_success(self, mock_run: MagicMock) -> None:
        stdout = json.dumps(
            {"text": '{"ok": true}', "stopReason": "EndTurn", "sessionId": "s"}
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")

        agent = GrokAgent()
        result = await agent.run_structured("system", "prompt", label="test")

        assert result == '{"ok": true}'

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_run_structured_cli_error_raises(self, mock_run: MagicMock) -> None:
        stdout = json.dumps({"type": "error", "message": "boom"})
        mock_run.return_value = MagicMock(returncode=1, stdout=stdout, stderr="")

        agent = GrokAgent()
        with pytest.raises(RuntimeError, match="grok structured run failed"):
            await agent.run_structured("system", "prompt", label="test")

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_run_structured_no_text_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")

        agent = GrokAgent()
        with pytest.raises(RuntimeError, match="no text output"):
            await agent.run_structured("system", "prompt", label="test")

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_run_structured_timeout_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="grok", timeout=300)

        agent = GrokAgent()
        with pytest.raises(RuntimeError, match="timed out"):
            await agent.run_structured("system", "prompt", label="test")

    @patch("deep_architect.coding_agents.grok.subprocess.run")
    async def test_run_structured_binary_not_found_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError()

        agent = GrokAgent(grok_bin="/nonexistent/grok")
        with pytest.raises(RuntimeError, match="grok binary not found"):
            await agent.run_structured("system", "prompt", label="test")


# ---------------------------------------------------------------------------
# create_agent
# ---------------------------------------------------------------------------


class TestCreateAgent:

    def test_create_opencode_agent(self) -> None:
        config = CodingAgentConfig(provider="opencode", model="test/model")
        agent = create_agent(config)
        assert isinstance(agent, OpencodeAgent)
        assert agent.model == "test/model"

    def test_create_opencode_agent_with_timeout(self) -> None:
        config = CodingAgentConfig(provider="opencode", timeout_seconds=42.0)
        agent = create_agent(config)
        assert isinstance(agent, OpencodeAgent)
        assert agent.timeout_seconds == 42.0

    def test_create_unsupported_agent_raises(self) -> None:
        config = CodingAgentConfig(provider="unsupported", model="test/model")
        with pytest.raises(ValueError, match="Unsupported agent provider"):
            create_agent(config)

    def test_create_claude_agent_unavailable_raises(self) -> None:
        """When claude-agent-sdk is not importable (simulated), raising is expected."""
        config = CodingAgentConfig(provider="claude", model="sonnet")
        # This will try to import claude_agent_sdk - since it's installed in
        # the project, we patch the import to simulate absence
        with patch.dict("sys.modules", {"claude_agent_sdk": None}):
            with pytest.raises(ImportError, match="claude-agent-sdk"):
                create_agent(config)

    def test_create_grok_agent(self) -> None:
        config = CodingAgentConfig(provider="grok", model="grok-build")
        agent = create_agent(config)
        assert isinstance(agent, GrokAgent)
        assert agent.model == "grok-build"

    def test_create_grok_agent_default_max_turns(self) -> None:
        config = CodingAgentConfig(provider="grok", model="grok-build")
        agent = create_agent(config)
        assert isinstance(agent, GrokAgent)
        assert agent.max_turns == 30

    def test_create_grok_agent_custom_max_turns(self) -> None:
        config = CodingAgentConfig(provider="grok", model="grok-build", max_turns=50)
        agent = create_agent(config)
        assert isinstance(agent, GrokAgent)
        assert agent.max_turns == 50

    def test_create_claude_agent_custom_max_turns(self) -> None:
        config = CodingAgentConfig(provider="claude", model="sonnet", max_turns=50)
        agent = create_agent(config)
        assert isinstance(agent, ClaudeSDKAgent)
        assert agent.max_turns == 50


# ---------------------------------------------------------------------------
# CodingAgent Protocol
# ---------------------------------------------------------------------------


class TestCodingAgentProtocol:

    def test_opencode_agent_implements_protocol(self) -> None:
        """Verify OpencodeAgent satisfies the CodingAgent protocol."""
        agent: CodingAgent = OpencodeAgent()
        assert hasattr(agent, "apply_fix")
        assert hasattr(agent, "fix_check_failures")
        assert hasattr(agent, "run_structured")

    def test_claude_sdk_agent_implements_protocol(self) -> None:
        """Verify ClaudeSDKAgent satisfies the CodingAgent protocol."""
        agent: CodingAgent = ClaudeSDKAgent()
        assert hasattr(agent, "apply_fix")
        assert hasattr(agent, "fix_check_failures")
        assert hasattr(agent, "run_structured")

    def test_grok_agent_implements_protocol(self) -> None:
        """Verify GrokAgent satisfies the CodingAgent protocol."""
        agent: CodingAgent = GrokAgent()
        assert hasattr(agent, "apply_fix")
        assert hasattr(agent, "fix_check_failures")
        assert hasattr(agent, "run_structured")
