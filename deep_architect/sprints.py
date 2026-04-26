from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SprintDefinition:
    number: int
    name: str
    description: str
    primary_files: list[str]
    allow_extra_files: bool = False  # Generator may create area-specific docs
    prompt_name: str | None = None  # Sprint-specific guidance prompt loaded into generator


SPRINTS: list[SprintDefinition] = [
    SprintDefinition(
        number=1,
        name="C1 System Context",
        description=(
            "Generate the C4 Level 1 System Context diagram and narrative. "
            "Show the system, its users, and external dependencies."
        ),
        primary_files=["c1-context.md"],
        prompt_name="sprint_1_c1_context",
    ),
    SprintDefinition(
        number=2,
        name="C2 Container Overview",
        description=(
            "Generate the overall C4 Level 2 Container diagram and tech stack decisions. "
            "Show all major containers and their relationships."
        ),
        primary_files=["c2-container.md"],
        prompt_name="sprint_2_c2_container",
    ),
    SprintDefinition(
        number=3,
        name="Frontend Container",
        description=(
            "Generate the frontend container details. "
            "Create frontend/c2-container.md and any area-specific documents "
            "(e.g. auth.md, schemas.md) as warranted by the PRD."
        ),
        primary_files=["frontend/c2-container.md"],
        allow_extra_files=True,
        prompt_name="sprint_3_frontend",
    ),
    SprintDefinition(
        number=4,
        name="Backend / Orchestration Container",
        description=(
            "Generate the backend and orchestration container details. "
            "Create backend/c2-container.md and area-specific documents as needed."
        ),
        primary_files=["backend/c2-container.md"],
        allow_extra_files=True,
        prompt_name="sprint_4_backend",
    ),
    SprintDefinition(
        number=5,
        name="Database + Knowledge Base",
        description=(
            "Generate the database and knowledge base container details. "
            "Create database/c2-container.md and any supporting documents."
        ),
        primary_files=["database/c2-container.md"],
        allow_extra_files=True,
        prompt_name="sprint_5_database",
    ),
    SprintDefinition(
        number=6,
        name="Edge Functions / Deployment / Auth / Scaling / Observability",
        description=(
            "Generate edge-functions/c2-container.md and deployment.md covering "
            "auth, scaling, and observability cross-cutting concerns."
        ),
        primary_files=["edge-functions/c2-container.md", "deployment.md"],
        prompt_name="sprint_6_edge",
    ),
    SprintDefinition(
        number=7,
        name="ADRs + Cross-Cutting Concerns",
        description=(
            "Generate Architecture Decision Records in the decisions/ folder "
            "and document non-functional requirements and cross-cutting concerns."
        ),
        primary_files=["decisions/"],
        allow_extra_files=True,
        prompt_name="sprint_7_adrs",
    ),
]
