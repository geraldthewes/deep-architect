from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class AgentConfig(BaseModel):
    model: str = "sonnet"  # alias: "sonnet", "opus", "haiku", or full model ID
    max_turns: int = 50
    max_agent_retries: int = 2
    context_window: int | None = None  # model context window size; logged when set
    agent_timeout_seconds: float | None = None  # per-attempt wall-clock limit; None → role default


class ThresholdConfig(BaseModel):
    min_score: float = 9.0
    consecutive_passing_rounds: int = 2
    max_rounds_per_sprint: int = 6
    max_total_rounds: int = 40
    timeout_hours: float = 0.0  # 0 = disabled; positive value enforces a wall-clock limit
    ping_pong_similarity_threshold: float = 0.85
    max_round_retries: int = 2
    rollback_regression_threshold: float = 0.05


def _default_generator() -> AgentConfig:
    return AgentConfig(model="sonnet", max_turns=50, agent_timeout_seconds=3600.0)


def _default_critic() -> AgentConfig:
    return AgentConfig(model="sonnet", max_turns=30, agent_timeout_seconds=1800.0)


class HarnessConfig(BaseModel):
    generator: AgentConfig = Field(default_factory=_default_generator)
    critic: AgentConfig = Field(default_factory=_default_critic)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    cli_path: str | None = None  # Override claude CLI path; defaults to shutil.which("claude")

    @model_validator(mode="after")
    def _apply_role_defaults(self) -> HarnessConfig:
        """Apply per-role timeout defaults when not set in TOML.

        Pydantic uses the AgentConfig field-level default (None) when a
        [generator] or [critic] section exists in TOML but omits
        agent_timeout_seconds.  The default_factory values on the fields
        above are only used when the entire section is absent.  This
        validator closes that gap.
        """
        if self.generator.agent_timeout_seconds is None:
            self.generator.agent_timeout_seconds = 3600.0
        if self.critic.agent_timeout_seconds is None:
            self.critic.agent_timeout_seconds = 1800.0
        return self


def load_config(config_path: Path | None = None) -> HarnessConfig:
    """Load config from ~/.deep-architect.toml, with optional override path."""
    if config_path is None:
        config_path = Path.home() / ".deep-architect.toml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Create ~/.deep-architect.toml with [generator] and [critic] sections.\n"
            "Endpoint configuration is via environment variables:\n"
            "  ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_DEFAULT_SONNET_MODEL, etc."
        )

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    return HarnessConfig.model_validate(raw)
