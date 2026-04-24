"""CLI-level tests for the --yolo flag wiring."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from deep_architect.cli import app

runner = CliRunner()


def test_help_text_mentions_yolo() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--yolo" in result.output


def test_yolo_flag_threaded_to_harness(tmp_path):  # type: ignore[no-untyped-def]
    prd = tmp_path / "prd.md"
    prd.write_text("# Stub PRD\n")
    out = tmp_path / "arch"
    out.mkdir()

    with patch("deep_architect.cli.validate_git_repo"), \
         patch("deep_architect.cli.asyncio.run") as mock_run:
        result = runner.invoke(
            app,
            ["--prd", str(prd), "--output", str(out), "--yolo"],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.called
    coro = mock_run.call_args.args[0]
    assert coro.cr_frame.f_locals.get("yolo") is True  # type: ignore[union-attr]
    coro.close()


def test_yolo_defaults_false(tmp_path):  # type: ignore[no-untyped-def]
    prd = tmp_path / "prd.md"
    prd.write_text("# Stub PRD\n")
    out = tmp_path / "arch"
    out.mkdir()

    with patch("deep_architect.cli.validate_git_repo"), \
         patch("deep_architect.cli.asyncio.run") as mock_run:
        result = runner.invoke(app, ["--prd", str(prd), "--output", str(out)])

    assert result.exit_code == 0, result.output
    coro = mock_run.call_args.args[0]
    assert coro.cr_frame.f_locals.get("yolo") is False  # type: ignore[union-attr]
    coro.close()
