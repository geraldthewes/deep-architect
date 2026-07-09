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
from deep_architect.coding_agents.base import _file_reflects_fix
from deep_architect.coding_agents.grok import _parse_grok_json

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
        import json as _json

        ndjson = _json.dumps({"type": "result", "is_error": False, "result": "ok"})
        mock_run.return_value = MagicMock(returncode=0, stdout=ndjson, stderr="")

        agent = OpencodeAgent(timeout_seconds=42.0)
        await agent.apply_fix(Path("test.py"), "old code", "new code")

        assert mock_run.call_args.kwargs["timeout"] == 42.0

    @patch("deep_architect.coding_agents.opencode.subprocess.run")
    async def test_apply_fix_success(self, mock_run: MagicMock) -> None:
        import json as _json

        ndjson = _json.dumps({"type": "result", "is_error": False, "result": "ok"})
        mock_run.return_value = MagicMock(
            returncode=0, stdout=ndjson, stderr=""
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
        import json as _json

        ndjson = _json.dumps({"type": "result", "is_error": False, "result": "ok"})
        mock_run.return_value = MagicMock(returncode=0, stdout=ndjson, stderr="")

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

    def test_missing_file_trusts_agent(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.py"
        assert _file_reflects_fix(target, "new code\n", "old code\n") is True

    def test_no_original_content_trusts_agent_on_mismatch(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "f.py"
        target.write_text("something unexpected\n", encoding="utf-8")
        assert _file_reflects_fix(target, "new code\n", None) is True


# ---------------------------------------------------------------------------
# ClaudeSDKAgent
# ---------------------------------------------------------------------------


class TestClaudeSDKAgent:

    def test_default_init(self) -> None:
        agent = ClaudeSDKAgent()
        assert agent.model == "sonnet"
        assert agent.timeout_seconds == 300.0

    def test_custom_timeout(self) -> None:
        agent = ClaudeSDKAgent(timeout_seconds=42.0)
        assert agent.timeout_seconds == 42.0

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
        # run_agent reports success, but the file on disk was never touched.
        target = tmp_path / "example.py"
        target.write_text("old code\n", encoding="utf-8")
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

        assert result is False

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


# ---------------------------------------------------------------------------
# _parse_grok_json
# ---------------------------------------------------------------------------


class TestParseGrokJson:

    def test_success_object(self) -> None:
        stdout = json.dumps(
            {"text": "done", "stopReason": "EndTurn", "sessionId": "s", "requestId": "r"}
        )
        assert _parse_grok_json(0, stdout, "") is True

    def test_error_object(self) -> None:
        stdout = json.dumps({"type": "error", "message": "boom"})
        assert _parse_grok_json(1, stdout, "") is False

    def test_exit_zero_non_json_trusts_exit_code(self) -> None:
        assert _parse_grok_json(0, "not json", "") is True

    def test_exit_one_empty_stdout_with_stderr(self) -> None:
        assert _parse_grok_json(1, "", "Error: something broke\n") is False


# ---------------------------------------------------------------------------
# GrokAgent
# ---------------------------------------------------------------------------


class TestGrokAgent:

    def test_default_init(self) -> None:
        agent = GrokAgent()
        assert agent.grok_bin == "grok"
        assert agent.model is None
        assert agent.timeout_seconds == 300.0

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


# ---------------------------------------------------------------------------
# CodingAgent Protocol
# ---------------------------------------------------------------------------


class TestCodingAgentProtocol:

    def test_opencode_agent_implements_protocol(self) -> None:
        """Verify OpencodeAgent satisfies the CodingAgent protocol."""
        agent: CodingAgent = OpencodeAgent()
        assert hasattr(agent, "apply_fix")
        assert hasattr(agent, "fix_check_failures")

    def test_claude_sdk_agent_implements_protocol(self) -> None:
        """Verify ClaudeSDKAgent satisfies the CodingAgent protocol."""
        agent: CodingAgent = ClaudeSDKAgent()
        assert hasattr(agent, "apply_fix")
        assert hasattr(agent, "fix_check_failures")

    def test_grok_agent_implements_protocol(self) -> None:
        """Verify GrokAgent satisfies the CodingAgent protocol."""
        agent: CodingAgent = GrokAgent()
        assert hasattr(agent, "apply_fix")
        assert hasattr(agent, "fix_check_failures")
