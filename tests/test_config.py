from pathlib import Path

import pytest

from deep_researcher.config import HarnessConfig, load_config


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config(tmp_path / "nonexistent.toml")


def test_load_config_valid(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model     = "sonnet"
max_turns = 50

[critic]
model     = "opus"
max_turns = 30
""")
    cfg = load_config(cfg_file)
    assert cfg.generator.model == "sonnet"
    assert cfg.generator.max_turns == 50
    assert cfg.critic.model == "opus"
    assert cfg.critic.max_turns == 30
    assert cfg.thresholds.min_score == 9.0


def test_load_config_defaults(tmp_path: Path) -> None:
    """Config file with no generator/critic sections uses defaults."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("")
    cfg = load_config(cfg_file)
    assert cfg.generator.model == "sonnet"
    assert cfg.critic.model == "sonnet"
    assert cfg.thresholds.min_score == 9.0


def test_load_config_custom_thresholds(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
[critic]
model = "sonnet"
[thresholds]
min_score = 8.5
max_rounds_per_sprint = 4
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.min_score == 8.5
    assert cfg.thresholds.max_rounds_per_sprint == 4


def test_load_config_cli_path(tmp_path: Path) -> None:
    # cli_path is a top-level HarnessConfig field, not nested under [generator]/[critic]
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'cli_path = "/usr/local/bin/claude"\n'
        "[generator]\nmodel = \"sonnet\"\n"
        "[critic]\nmodel = \"sonnet\"\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.cli_path == "/usr/local/bin/claude"


def test_harness_config_type(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
[critic]
model = "opus"
""")
    cfg = load_config(cfg_file)
    assert isinstance(cfg, HarnessConfig)
