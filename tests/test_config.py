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
base_url = "http://gen.example.com/v1"
api_key  = "gen-key"
model    = "nemotron"

[critic]
base_url = "http://crit.example.com/v1"
api_key  = "crit-key"
model    = "qwen"
""")
    cfg = load_config(cfg_file)
    assert cfg.generator.model == "nemotron"
    assert cfg.thresholds.min_score == 9.0


def test_load_config_custom_thresholds(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
base_url = "http://gen"
api_key = "k"
model = "m"
[critic]
base_url = "http://crit"
api_key = "k"
model = "m"
[thresholds]
min_score = 8.5
max_rounds_per_sprint = 4
""")
    cfg = load_config(cfg_file)
    assert cfg.thresholds.min_score == 8.5
    assert cfg.thresholds.max_rounds_per_sprint == 4


def test_harness_config_type(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[generator]
base_url = "http://gen"
api_key = "k"
model = "m"
[critic]
base_url = "http://crit"
api_key = "k"
model = "m"
""")
    cfg = load_config(cfg_file)
    assert isinstance(cfg, HarnessConfig)
