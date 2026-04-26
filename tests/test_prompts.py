import pytest

from deep_architect.prompts import load_prompt
from deep_architect.sprints import SPRINTS

EXPECTED_PROMPTS = [
    "generator_system",
    "critic_system",
    "contract_system",
    "contract_proposal",
    "contract_review",
    "ping_pong_check",
    "final_agreement",
    "sprint_1_c1_context",
    "sprint_2_c2_container",
    "sprint_3_frontend",
    "sprint_4_backend",
    "sprint_5_database",
    "sprint_6_edge",
    "sprint_7_adrs",
    "mermaid_c4_guide",
    "c4_skill",
    "critic_rescue",
    "contract_proposal_re",
    "final_agreement_re",
]


@pytest.mark.parametrize("name", EXPECTED_PROMPTS)
def test_prompt_loads(name: str) -> None:
    content = load_prompt(name)
    assert len(content) > 0


def test_prompt_not_found() -> None:
    with pytest.raises(FileNotFoundError, match="Prompt not found"):
        load_prompt("nonexistent_prompt")


def test_contract_proposal_variable_substitution() -> None:
    content = load_prompt(
        "contract_proposal",
        prd="PRD content here",
        sprint_number="1",
        sprint_name="C1 Context",
        sprint_description="Generate C1 diagram",
        primary_files="['c1-context.md']",
    )
    assert "PRD content here" in content
    assert "C1 Context" in content


def test_contract_review_variable_substitution() -> None:
    content = load_prompt("contract_review", contract_json='{"sprint_number": 1}')
    assert '{"sprint_number": 1}' in content


def test_ping_pong_variable_substitution() -> None:
    content = load_prompt(
        "ping_pong_check",
        previous_summary="previous feedback",
        current_summary="current feedback",
    )
    assert "previous feedback" in content
    assert "current feedback" in content


def test_final_agreement_no_path_placeholder() -> None:
    content = load_prompt("final_agreement")
    assert "cwd" in content
    assert "{output_dir}" not in content


def test_contract_proposal_re_variable_substitution() -> None:
    content = load_prompt(
        "contract_proposal_re",
        codebase_path="/path/to/repo",
        sprint_number="1",
        sprint_name="C1 Context",
        sprint_description="Generate C1 diagram",
        primary_files="['c1-context.md']",
    )
    assert "/path/to/repo" in content
    assert "C1 Context" in content


def test_final_agreement_re_no_path_placeholder() -> None:
    content = load_prompt("final_agreement_re")
    assert "cwd" in content
    assert "{output_dir}" not in content
    assert "accurate" in content


def test_all_sprint_prompt_names_resolve() -> None:
    for sprint in SPRINTS:
        assert sprint.prompt_name is not None, f"Sprint {sprint.number} missing prompt_name"
        content = load_prompt(sprint.prompt_name)
        assert len(content) > 0, f"Sprint {sprint.number} prompt is empty"


def test_generator_system_mentions_available_tools() -> None:
    content = load_prompt("generator_system")
    assert "ONLY use these tools" in content
    assert "TodoWrite" in content


def test_critic_system_mentions_available_tools() -> None:
    content = load_prompt("critic_system")
    assert "ONLY use these tools" in content
    assert "TodoWrite" in content
