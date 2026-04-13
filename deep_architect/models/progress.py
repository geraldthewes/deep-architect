from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class SprintStatus(BaseModel):
    sprint_number: int
    sprint_name: str
    status: Literal[
        "pending", "negotiating", "building", "evaluating", "passed", "failed", "accepted"
    ] = "pending"
    rounds_completed: int = 0
    consecutive_passes: int = 0
    final_score: float | None = None
    best_round: int | None = None
    best_scores: dict[str, float] | None = None


class HarnessProgress(BaseModel):
    status: Literal["running", "complete", "failed"] = "running"
    current_sprint: int = 1
    total_sprints: int
    completed_sprints: int = 0
    total_rounds: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    seed: int = Field(default_factory=lambda: int(time.time()))
    sprint_statuses: list[SprintStatus]
