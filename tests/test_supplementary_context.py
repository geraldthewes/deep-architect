"""Tests for the supplementary context injection feature.

Covers:
- _build_supplementary_context() helper in harness.py
- run_generator() includes / omits the context section
- propose_contract() includes / omits the context section
- CLI validation of --context file paths
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from deep_architect.agents.generator import propose_contract, run_generator
from deep_architect.cli import app
from deep_architect.config import AgentConfig, HarnessConfig
from deep_architect.harness import _build_supplementary_context
from deep_architect.models.contract import SprintContract, SprintCriterion
from deep_architect.sprints import SPRINTS

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_agent_config() -> AgentConfig:
    return AgentConfig(model="test-model", max_turns=1, max_agent_retries=0)


def _make_contract() -> SprintContract:
    return SprintContract(
        sprint_number=1,
        sprint_name="C1 System Context",
        files_to_produce=["c1-context.md"],
        criteria=[
            SprintCriterion(name="a", description="A criterion"),
            SprintCriterion(name="b", description="B criterion"),
            SprintCriterion(name="c", description="C criterion"),
        ],
    )


# ---------------------------------------------------------------------------
# _build_supplementary_context
# ---------------------------------------------------------------------------


def test_build_supplementary_context_none_returns_empty() -> None:
    assert _build_supplementary_context(None) == ""


def test_build_supplementary_context_empty_list_returns_empty() -> None:
    assert _build_supplementary_context([]) == ""


def test_build_supplementary_context_single_file(tmp_path: Path) -> None:
    ctx = tmp_path / "tech-decisions.md"
    ctx.write_text("Use Go and Bifrost.")

    result = _build_supplementary_context([ctx])

    assert "binding constraints" in result
    assert "tech-decisions.md" in result
    assert "Use Go and Bifrost." in result


def test_build_supplementary_context_multiple_files(tmp_path: Path) -> None:
    f1 = tmp_path / "brainstorm.md"
    f1.write_text("Content A")
    f2 = tmp_path / "decisions.md"
    f2.write_text("Content B")

    result = _build_supplementary_context([f1, f2])

    assert "brainstorm.md" in result
    assert "decisions.md" in result
    assert "Content A" in result
    assert "Content B" in result


# ---------------------------------------------------------------------------
# run_generator — prompt assembly
# ---------------------------------------------------------------------------


async def test_run_generator_includes_supplementary_context(tmp_path: Path) -> None:
    captured_prompt: list[str] = []

    async def _mock_run_agent(options: object, prompt: str, **kwargs: object) -> object:
        captured_prompt.append(prompt)
        result = MagicMock()
        result.session_id = None
        return result

    with patch("deep_architect.agents.generator.run_agent", side_effect=_mock_run_agent):
        await run_generator(
            _make_agent_config(),
            SPRINTS[0],
            _make_contract(),
            "# PRD content",
            None,
            tmp_path,
            1,
            supplementary_context="Use Go and Bifrost for the gateway.",
        )

    assert captured_prompt, "run_agent was not called"
    prompt = captured_prompt[0]
    assert "## Supplementary Context" in prompt
    assert "Use Go and Bifrost for the gateway." in prompt


async def test_run_generator_omits_context_section_when_empty(tmp_path: Path) -> None:
    captured_prompt: list[str] = []

    async def _mock_run_agent(options: object, prompt: str, **kwargs: object) -> object:
        captured_prompt.append(prompt)
        result = MagicMock()
        result.session_id = None
        return result

    with patch("deep_architect.agents.generator.run_agent", side_effect=_mock_run_agent):
        await run_generator(
            _make_agent_config(),
            SPRINTS[0],
            _make_contract(),
            "# PRD content",
            None,
            tmp_path,
            1,
            supplementary_context="",
        )

    assert captured_prompt, "run_agent was not called"
    assert "## Supplementary Context" not in captured_prompt[0]


# ---------------------------------------------------------------------------
# propose_contract — prompt assembly
# ---------------------------------------------------------------------------


async def test_propose_contract_includes_supplementary_context() -> None:
    captured_prompt: list[str] = []

    async def _mock_structured(
        config: object,
        system: object,
        prompt: str,
        schema: object,
        **kwargs: object,
    ) -> SprintContract:
        captured_prompt.append(prompt)
        return _make_contract()

    with patch(
        "deep_architect.agents.generator.run_simple_structured",
        side_effect=_mock_structured,
    ):
        await propose_contract(
            _make_agent_config(),
            SPRINTS[0],
            "# PRD content",
            supplementary_context="Use Go and Bifrost.",
        )

    assert captured_prompt, "run_simple_structured was not called"
    assert "## Supplementary Context" in captured_prompt[0]
    assert "Use Go and Bifrost." in captured_prompt[0]


async def test_propose_contract_omits_context_section_when_empty() -> None:
    captured_prompt: list[str] = []

    async def _mock_structured(
        config: object,
        system: object,
        prompt: str,
        schema: object,
        **kwargs: object,
    ) -> SprintContract:
        captured_prompt.append(prompt)
        return _make_contract()

    with patch(
        "deep_architect.agents.generator.run_simple_structured",
        side_effect=_mock_structured,
    ):
        await propose_contract(
            _make_agent_config(),
            SPRINTS[0],
            "# PRD content",
            supplementary_context="",
        )

    assert captured_prompt, "run_simple_structured was not called"
    assert "## Supplementary Context" not in captured_prompt[0]


# ---------------------------------------------------------------------------
# CLI — --context validation
# ---------------------------------------------------------------------------


def test_cli_context_validation_missing_file(tmp_path: Path) -> None:
    runner = CliRunner()
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD")

    result = runner.invoke(
        app,
        [
            "--prd", str(prd),
            "--output", str(tmp_path),
            "--context", str(tmp_path / "nonexistent.md"),
        ],
    )

    assert result.exit_code != 0
    assert "nonexistent.md" in result.output


def test_cli_context_validation_passes_with_valid_file(tmp_path: Path) -> None:
    """CLI should not error on the context validation step when the file exists.
    We patch run_harness so the test doesn't actually run the harness.
    """
    runner = CliRunner()
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD")
    ctx = tmp_path / "context.md"
    ctx.write_text("Use Bifrost.")

    mock_run = AsyncMock()

    with patch("deep_architect.cli.run_harness", mock_run), \
         patch("deep_architect.cli.load_config", return_value=HarnessConfig()):
        result = runner.invoke(
            app,
            [
                "--prd", str(prd),
                "--output", str(tmp_path),
                "--context", str(ctx),
            ],
            catch_exceptions=False,
        )

    # Should not exit with a "file not found" error; harness call is mocked
    assert "Context file not found" not in result.output
    assert mock_run.called
    call_kwargs = mock_run.call_args.kwargs
    assert "context_files" in call_kwargs
    assert ctx in call_kwargs["context_files"]
