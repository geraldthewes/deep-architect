#!/usr/bin/env python3
"""
Diagnostic: test three structured-output approaches against the live LiteLLM proxy.

Tests whether the critic's original output_format enforcement path can be restored,
or whether pydantic-ai via the OpenAI-compatible endpoint is a viable alternative.

Usage:
    uv run python scripts/test_output_format.py [--approach A|B|C|all]

Exits 0 if all selected approaches succeed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Ensure deep_architect is importable from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from deep_architect.agents.client import (
    json_schema_format,
    make_agent_options,
    resolve_model_id,
    run_agent_structured,
    run_simple_structured,
)
from deep_architect.config import load_config
from deep_architect.models.contract import SprintContract, SprintCriterion
from deep_architect.models.feedback import CriticResult

# ---------------------------------------------------------------------------
# Synthetic test fixture
# ---------------------------------------------------------------------------

_ARCH_FILE = "c1-context.md"
_ARCH_CONTENT = """\
# C1 Context Diagram

## System Context

```mermaid
C4Context
    title System Context for Test System
    Person(user, "User", "A person using the system")
    System(sys, "Test System", "The system under review")
    Rel(user, sys, "Uses")
```

See also: ADR-001
"""

_CONTRACT = SprintContract(
    sprint_number=1,
    sprint_name="C1 Context",
    files_to_produce=[_ARCH_FILE],
    criteria=[
        SprintCriterion(
            name="C1 diagram present",
            description="A C4 context diagram exists and is syntactically valid Mermaid.",
            threshold=7.0,
        ),
        SprintCriterion(
            name="System relationships",
            description="At least one relationship between actors and the system is defined.",
            threshold=7.0,
        ),
        SprintCriterion(
            name="File completeness",
            description="The file contains a title and a meaningful description.",
            threshold=7.0,
        ),
    ],
)

_CRITIC_SYSTEM = """\
You are Boris, a hostile senior architect evaluating architecture files.

## Scoring Guidelines
- 9-10: Exceptional.
- 7-8: Good. Minor issues only.
- 5-6: Partial. Significant gaps.
- 3-4: Poor. Fundamental issues.
- 1-2: Failed. Not implemented or broken.

## Response Format
Return ONLY a CriticResult JSON object — no preamble, no explanation, no code fences:
{
  "scores": {"criterion_name": score},
  "feedback": [{"criterion": "name", "score": 7.5, "severity": "Low", "details": "..."}],
  "overall_summary": "One-paragraph summary"
}

MANDATORY FINAL STEP: After any tool calls, output the CriticResult JSON as your final response.
"""


def _make_prompt_with_contents(tmp_dir: Path) -> str:
    """Build an evaluation prompt with file contents embedded (no tool use needed)."""
    file_path = tmp_dir / _ARCH_FILE
    contents = file_path.read_text()
    return (
        f"Evaluate this architecture file against the sprint contract.\n\n"
        f"## Sprint Contract\n{_CONTRACT.model_dump_json(indent=2)}\n\n"
        f"## File: {_ARCH_FILE}\n```\n{contents}\n```\n\n"
        "Score every criterion. Return ONLY a CriticResult JSON object."
    )


def _make_agentic_prompt(tmp_dir: Path) -> str:
    """Build an evaluation prompt for the agentic critic (uses Read tools)."""
    files_list = "\n".join(f"- {f}" for f in _CONTRACT.files_to_produce)
    return (
        f"Evaluate the architecture files in {tmp_dir} against the sprint contract.\n\n"
        f"## Sprint Contract\n{_CONTRACT.model_dump_json(indent=2)}\n\n"
        f"## Files to Evaluate\n{files_list}\n\n"
        "This is Round 1. Use Read to inspect each file. "
        "Score every criterion. Return a CriticResult JSON object."
    )


# ---------------------------------------------------------------------------
# Approach A: Claude Code SDK + output_format (original enforcement path)
# ---------------------------------------------------------------------------

async def test_approach_a(config: object, tmp_dir: Path) -> tuple[bool, str, float]:
    """Original approach: output_format injects StructuredOutput tool the model must call."""
    cfg = getattr(config, "critic")  # AgentConfig
    prompt = _make_agentic_prompt(tmp_dir)
    options = make_agent_options(
        cfg,
        _CRITIC_SYSTEM,
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
        cwd=str(tmp_dir),
        output_format=json_schema_format(CriticResult),
    )

    t0 = time.monotonic()
    try:
        raw = await run_agent_structured(
            options, prompt,
            label="A output_format",
            max_retries=0,
            timeout_seconds=120.0,
        )
        result = CriticResult.model_validate(raw)
        elapsed = time.monotonic() - t0
        detail = f"avg={result.average_score:.1f} passed={result.passed}"
        if result.feedback:
            detail += f"\n    feedback[0]: {result.feedback[0].details[:120]}"
        return True, detail, elapsed
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:400]}", time.monotonic() - t0


# ---------------------------------------------------------------------------
# Approach B: pydantic-ai via OpenAI-compatible endpoint
# ---------------------------------------------------------------------------

async def test_approach_b(config: object, tmp_dir: Path) -> tuple[bool, str, float]:
    """pydantic-ai Agent with output_type, backed by the litellm OpenAI endpoint."""
    cfg = getattr(config, "critic")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
    model_name = resolve_model_id(cfg.model)

    provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    model = OpenAIChatModel(model_name, provider=provider)  # type: ignore[arg-type]
    agent: Agent[None, CriticResult] = Agent(
        model, output_type=CriticResult, system_prompt=_CRITIC_SYSTEM
    )

    prompt = _make_prompt_with_contents(tmp_dir)
    t0 = time.monotonic()
    try:
        result_obj = await agent.run(prompt)
        result: CriticResult = result_obj.output
        elapsed = time.monotonic() - t0
        detail = f"avg={result.average_score:.1f} passed={result.passed}"
        if result.feedback:
            detail += f"\n    feedback[0]: {result.feedback[0].details[:120]}"
        return True, detail, elapsed
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:400]}", time.monotonic() - t0


# ---------------------------------------------------------------------------
# Approach C: Baseline — system prompt instruction only (current production path)
# ---------------------------------------------------------------------------

async def test_approach_c(config: object, tmp_dir: Path) -> tuple[bool, str, float]:
    """Current approach: no output_format enforcement, rely on prompt instruction alone."""
    cfg = getattr(config, "critic")
    prompt = _make_prompt_with_contents(tmp_dir)
    t0 = time.monotonic()
    try:
        result = await run_simple_structured(
            cfg, _CRITIC_SYSTEM, prompt, CriticResult, label="C baseline"
        )
        elapsed = time.monotonic() - t0
        detail = f"avg={result.average_score:.1f} passed={result.passed}"
        if result.feedback:
            detail += f"\n    feedback[0]: {result.feedback[0].details[:120]}"
        return True, detail, elapsed
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:400]}", time.monotonic() - t0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

APPROACHES = {
    "A": ("Claude Code SDK + output_format (original enforcement)", test_approach_a),
    "B": ("pydantic-ai via OpenAI-compatible endpoint", test_approach_b),
    "C": ("Baseline: system prompt instruction only (current)", test_approach_c),
}


async def main(selected: list[str]) -> int:
    config = load_config()

    with tempfile.TemporaryDirectory(prefix="deep-architect-test-") as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / _ARCH_FILE).write_text(_ARCH_CONTENT)

        results: dict[str, tuple[bool, str, float]] = {}
        for key in selected:
            label, fn = APPROACHES[key]
            print(f"\n[{key}] {label}")
            print("    running...", flush=True)
            ok, detail, elapsed = await fn(config, tmp_dir)
            results[key] = (ok, detail, elapsed)
            status = "PASS" if ok else "FAIL"
            print(f"    {status}  {elapsed:.1f}s")
            for line in detail.splitlines():
                print(f"    {line}")

    print("\n" + "=" * 68)
    print("Results summary")
    print("=" * 68)
    for key, (ok, _, elapsed) in results.items():
        label, _ = APPROACHES[key]
        status = "PASS" if ok else "FAIL"
        print(f"  [{key}] {status:4s}  {elapsed:5.1f}s  {label}")

    print()
    print("Interpretation:")
    a_ok = results.get("A", (False,))[0]
    b_ok = results.get("B", (False,))[0]
    c_ok = results.get("C", (False,))[0]

    if "A" in results:
        if a_ok:
            print("  A PASS → output_format works with current CLI.")
            print("           Restore as primary path in critic.py.")
        else:
            print("  A FAIL → output_format / StructuredOutput tool still broken with litellm.")
    if "B" in results:
        if b_ok:
            print("  B PASS → pydantic-ai via OpenAI endpoint is viable for enforcement.")
        else:
            print("  B FAIL → pydantic-ai tool_choice still fails against this litellm endpoint.")
    if "A" in results and "B" in results and not a_ok and not b_ok:
        print("  A+B FAIL → litellm does not support tool-enforced structured output.")
        print("             Keep the rescue-call fallback approach.")
    if "C" in results:
        if c_ok:
            print("  C PASS → baseline (prompt-only) works when the model cooperates.")
        else:
            print("  C FAIL → baseline is broken — separate issue.")

    all_ok = all(ok for ok, _, _ in results.values())
    return 0 if all_ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--approach",
        default="all",
        choices=["A", "B", "C", "all"],
        help="Which approach to test (default: all)",
    )
    args = parser.parse_args()
    selected = list(APPROACHES.keys()) if args.approach == "all" else [args.approach]
    sys.exit(asyncio.run(main(selected)))
