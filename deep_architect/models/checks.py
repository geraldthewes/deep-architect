from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CheckProfile(BaseModel):
    """One glob-scoped group of programmatic check commands."""

    name: str
    paths: list[str]                      # gitwildmatch globs, repo-root-relative
    commands: list[str]                   # shell-less command strings; may contain {files}
    timeout: int = 120                    # per-command timeout in seconds


class LLMRulesConfig(BaseModel):
    """Where LLM-judged rules live."""

    source: str = ".opencodereview/rule.json"


class QualityChecksConfig(BaseModel):
    profiles: list[CheckProfile] = Field(default_factory=list)
    llm_rules: LLMRulesConfig | None = None
    auto_detected: bool = False           # True when built by fallback detection


class StyleViolation(BaseModel):
    rule_id: str                                    # e.g. "PY-STY-017"; "GENERAL" if uncited
    severity: Literal["MUST", "SHOULD", "MAY", "NIT"]
    description: str
    line: int | None = None


class StyleVerdict(BaseModel):
    violations: list[StyleViolation] = Field(default_factory=list)

    @property
    def blocking(self) -> list[StyleViolation]:
        return [v for v in self.violations if v.severity in ("MUST", "SHOULD")]
