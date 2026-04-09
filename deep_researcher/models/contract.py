from __future__ import annotations

from pydantic import BaseModel, Field


class SprintCriterion(BaseModel):
    name: str
    description: str
    threshold: float = Field(default=9.0, ge=1.0, le=10.0)


class SprintContract(BaseModel):
    sprint_number: int
    sprint_name: str
    files_to_produce: list[str]
    criteria: list[SprintCriterion] = Field(min_length=3)
