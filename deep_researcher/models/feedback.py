from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CriterionScore(BaseModel):
    criterion: str
    score: float = Field(ge=1.0, le=10.0)
    severity: Literal["Critical", "High", "Medium", "Low"]
    details: str = Field(description="Specific feedback with file:line references where applicable")


class CriticResult(BaseModel):
    scores: dict[str, float]
    feedback: list[CriterionScore] = Field(min_length=1)
    overall_summary: str
    average_score: float = Field(default=0.0)
    passed: bool = Field(default=False)

    @model_validator(mode="after")
    def compute_pass(self) -> CriticResult:
        if self.feedback:
            self.average_score = sum(f.score for f in self.feedback) / len(self.feedback)
        has_critical_high = any(f.severity in ("Critical", "High") for f in self.feedback)
        self.passed = self.average_score >= 9.0 and not has_critical_high
        return self


class PingPongResult(BaseModel):
    similarity_score: float = Field(ge=0.0, le=1.0)
    reasoning: str
