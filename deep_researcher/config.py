from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    model: str = "sonnet"  # alias: "sonnet", "opus", "haiku", or full model ID
    max_turns: int = 50
    max_agent_retries: int = 2


class ThresholdConfig(BaseModel):
    min_score: float = 9.0
    consecutive_passing_rounds: int = 2
    max_rounds_per_sprint: int = 6
    max_total_rounds: int = 40
    timeout_hours: float = 3.0
    ping_pong_similarity_threshold: float = 0.85
    max_round_retries: int = 2


def _default_generator() -> AgentConfig:
    return AgentConfig(model="sonnet", max_turns=50)


def _default_critic() -> AgentConfig:
    return AgentConfig(model="sonnet", max_turns=30)


class HarnessConfig(BaseModel):
    generator: AgentConfig = Field(default_factory=_default_generator)
    critic: AgentConfig = Field(default_factory=_default_critic)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    cli_path: str | None = None  # Override claude CLI path; defaults to shutil.which("claude")


def load_config(config_path: Path | None = None) -> HarnessConfig:
    """Load config from ~/.deep-researcher.toml, with optional override path."""
    if config_path is None:
        config_path = Path.home() / ".deep-researcher.toml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Create ~/.deep-researcher.toml with [generator] and [critic] sections.\n"
            "Endpoint configuration is via environment variables:\n"
            "  ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_DEFAULT_SONNET_MODEL, etc."
        )

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    return HarnessConfig.model_validate(raw)
