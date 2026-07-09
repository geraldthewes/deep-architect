import logging
from pathlib import Path

import pytest

from deep_architect.config import HarnessConfig, load_config


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


def test_agent_config_max_agent_retries_default() -> None:
    from deep_architect.config import AgentConfig

    cfg = AgentConfig()
    assert cfg.max_agent_retries == 2


def test_threshold_config_max_round_retries_default() -> None:
    from deep_architect.config import ThresholdConfig

    cfg = ThresholdConfig()
    assert cfg.max_round_retries == 2


def test_load_config_with_max_agent_retries(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
max_agent_retries = 3
[critic]
model = "sonnet"
max_agent_retries = 1
""")
    cfg = load_config(cfg_file)
    assert cfg.generator.max_agent_retries == 3
    assert cfg.critic.max_agent_retries == 1


def test_role_timeout_defaults_when_omitted_from_toml(tmp_path: Path) -> None:
    """Generator gets 3600s and critic gets 1800s even when agent_timeout_seconds
    is absent from the TOML — the class-level default (None) must not win."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "opus"
max_turns = 50

[critic]
model = "sonnet"
max_turns = 30
""")
    cfg = load_config(cfg_file)
    assert cfg.generator.agent_timeout_seconds == 3600.0
    assert cfg.critic.agent_timeout_seconds == 1800.0


def test_explicit_timeout_in_toml_is_preserved(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
agent_timeout_seconds = 7200

[critic]
model = "sonnet"
agent_timeout_seconds = 900
""")
    cfg = load_config(cfg_file)
    assert cfg.generator.agent_timeout_seconds == 7200.0
    assert cfg.critic.agent_timeout_seconds == 900.0


def test_load_config_with_max_round_retries(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
[critic]
model = "sonnet"
[thresholds]
max_round_retries = 0
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.max_round_retries == 0


def test_threshold_early_accept_defaults() -> None:
    from deep_architect.config import ThresholdConfig

    cfg = ThresholdConfig()
    assert cfg.early_accept_score == 9.5
    assert cfg.early_accept_stalls == 3


def test_threshold_coding_agent_timeout_default() -> None:
    from deep_architect.config import ThresholdConfig

    cfg = ThresholdConfig()
    assert cfg.coding_agent_timeout is None


def test_load_config_with_coding_agent_timeout(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
[critic]
model = "sonnet"
[thresholds]
coding_agent_timeout = 240.0
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.coding_agent_timeout == 240.0


def test_threshold_coding_agent_max_turns_default() -> None:
    from deep_architect.config import ThresholdConfig

    cfg = ThresholdConfig()
    assert cfg.coding_agent_max_turns is None


def test_load_config_with_coding_agent_max_turns(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
[critic]
model = "sonnet"
[thresholds]
coding_agent_max_turns = 50
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.coding_agent_max_turns == 50


def test_threshold_judge_parse_retries_default() -> None:
    from deep_architect.config import ThresholdConfig

    cfg = ThresholdConfig()
    assert cfg.judge_parse_retries == 2


def test_load_config_with_judge_parse_retries(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
[critic]
model = "sonnet"
[thresholds]
judge_parse_retries = 4
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.judge_parse_retries == 4


def test_load_config_with_early_accept(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
model = "sonnet"
[critic]
model = "sonnet"
[thresholds]
early_accept_score  = 9.8
early_accept_stalls = 2
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.early_accept_score == 9.8
    assert cfg.thresholds.early_accept_stalls == 2


def test_resolve_default_config_path_prefers_xdg_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deep_architect.config import _resolve_default_config_path

    xdg_home = tmp_path / "xdgcfg"
    cfg_dir = xdg_home / "deep-architect"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))

    resolved = _resolve_default_config_path()
    assert resolved == cfg_dir / "config.toml"


def test_resolve_default_config_path_falls_back_to_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deep_architect.config import _resolve_default_config_path

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg_dir = tmp_path / ".config" / "deep-architect"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("")

    resolved = _resolve_default_config_path()
    assert resolved == cfg_dir / "config.toml"


def test_resolve_default_config_path_falls_back_to_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from deep_architect.config import _resolve_default_config_path

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    legacy_file = tmp_path / ".deep-architect.toml"
    legacy_file.write_text("")

    with caplog.at_level(logging.WARNING):
        resolved = _resolve_default_config_path()

    assert resolved == legacy_file
    assert "legacy path" in caplog.text


def test_resolve_default_config_path_defaults_to_xdg_when_nothing_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deep_architect.config import _resolve_default_config_path

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    resolved = _resolve_default_config_path()
    assert resolved == tmp_path / ".config" / "deep-architect" / "config.toml"


def test_load_config_default_path_missing_mentions_xdg_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    expected_path = tmp_path / ".config" / "deep-architect" / "config.toml"
    with pytest.raises(FileNotFoundError, match=r"\.config/deep-architect/config\.toml"):
        load_config(None)
    assert not expected_path.exists()
