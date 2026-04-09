from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    base_url: str
    api_key: str
    model: str


class ThresholdConfig(BaseModel):
    min_score: float = 9.0
    consecutive_passing_rounds: int = 2
    max_rounds_per_sprint: int = 6
    max_total_rounds: int = 40
    timeout_hours: float = 3.0
    ping_pong_similarity_threshold: float = 0.85


class HarnessConfig(BaseModel):
    generator: AgentConfig
    critic: AgentConfig
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)


def load_config(config_path: Path | None = None) -> HarnessConfig:
    """Load config from ~/.deep-researcher.toml, with optional override path."""
    if config_path is None:
        config_path = Path.home() / ".deep-researcher.toml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Create ~/.deep-researcher.toml with [generator] and [critic] sections."
        )

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    return HarnessConfig.model_validate(raw)
