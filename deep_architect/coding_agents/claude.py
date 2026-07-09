from __future__ import annotations

import asyncio
from pathlib import Path

from deep_architect.coding_agents.base import _file_reflects_fix
from deep_architect.logger import get_logger

logger = get_logger(__name__)

CLAUDE_DEFAULT_TIMEOUT: float = 300.0


class ClaudeSDKAgent:
    """Claude SDK implementation of CodingAgent.

    Delegates to deep_architect.agents.client (the harness's canonical SDK
    layer) instead of driving claude_agent_sdk directly, so this agent gets
    the same inactivity timeout, retry, and cancel-scope handling as the
    generator/critic agents for free.
    """

    # Narrow, edit-only turn budget — this agent applies one small change to
    # one file, not a full generation task like the harness's generator.
    MAX_TURNS = 10

    def __init__(
        self,
        model: str = "sonnet",
        permission_mode: str = "bypassPermissions",
        disallowed_tools: list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model
        self.permission_mode = permission_mode
        # No longer used: deep_architect.agents.client.make_agent_options
        # applies its own canonical DISALLOWED_TOOLS list. Kept only so
        # existing callers passing disallowed_tools don't break.
        self.disallowed_tools = disallowed_tools
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else CLAUDE_DEFAULT_TIMEOUT
        )

    async def apply_fix(
        self,
        file_path: Path,
        existing_code: str,
        suggested_code: str,
        context: str = "",
        original_content: str | None = None,
    ) -> bool:
        """Apply fix using the Claude Agent SDK, via the shared client harness."""
        from deep_architect.agents.client import (  # noqa: PLC0415
            make_agent_options,
            run_agent,
        )
        from deep_architect.config import (  # noqa: PLC0415
            AgentConfig as ClientAgentConfig,
        )

        absolute_file_path = file_path.resolve()

        system_prompt = (
            "You are a precise code editing assistant. Your task is to make "
            "exact code replacements as specified. Do not make any other "
            "changes unless explicitly instructed. Do not run git or commit "
            "the change — that is handled separately. Confirm when the "
            "change has been made."
        )
        prompt = (
            f"Please apply the following code change to {absolute_file_path}:\n\n"
            f"Existing code:\n```\n{existing_code}\n```\n\n"
            f"Replace with:\n```\n{suggested_code}\n```\n\n"
            f"Context: {context}\n\n"
            "Make the change and confirm it was applied correctly. "
            "Do not commit the change."
        )

        client_config = ClientAgentConfig(model=self.model, max_turns=self.MAX_TURNS)

        try:
            options = make_agent_options(
                client_config,
                system_prompt,
                allowed_tools=["Read", "Edit", "Write"],
                cwd=str(Path.cwd()),
            )

            result_message = await run_agent(
                options,
                prompt,
                label=f"review-action:{file_path.name}",
                timeout_seconds=self.timeout_seconds,
                max_retries=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Exception using Claude SDK to apply fix for %s: %s",
                file_path,
                e,
            )
            return False

        # run_agent() raises on error, so reaching here means the agent
        # reported success — but that's not proof it actually edited the
        # file, so verify against the file on disk.
        try:
            return _file_reflects_fix(
                absolute_file_path,
                suggested_code,
                original_content,
                agent_response_text=result_message.result,
            )
        except Exception as e:
            logger.error(
                "ClaudeSDKAgent: error verifying fix for %s: %s", file_path, e
            )
            return False

    async def fix_check_failures(
        self,
        files: list[Path],
        failure_report: str,
        context: str = "",
    ) -> bool:
        """Address quality-check failures using the Claude Agent SDK.

        The check rerun in the harness's fix loop is the real verification —
        no _file_reflects_fix here, unlike apply_fix.
        """
        from deep_architect.agents.client import (  # noqa: PLC0415
            make_agent_options,
            run_agent,
        )
        from deep_architect.config import (  # noqa: PLC0415
            AgentConfig as ClientAgentConfig,
        )

        absolute_files = [f.resolve() for f in files]
        file_list = "\n".join(f"- {f}" for f in absolute_files)

        system_prompt = (
            "You are a precise code editing assistant. A previous fix introduced "
            "quality-check failures (lint/type/security/test or style-rule "
            "violations). Address them without reverting the intent of the "
            "original change. Do not run git or commit — that is handled "
            "separately. Confirm when the failures have been addressed."
        )
        prompt = (
            f"The following files were modified by a previous fix:\n{file_list}\n\n"
            f"Original review context: {context}\n\n"
            f"{failure_report}\n\n"
            "Fix these quality-check failures, then confirm when done."
        )

        client_config = ClientAgentConfig(model=self.model, max_turns=self.MAX_TURNS)

        try:
            options = make_agent_options(
                client_config,
                system_prompt,
                allowed_tools=["Read", "Edit", "Write"],
                cwd=str(Path.cwd()),
            )

            await run_agent(
                options,
                prompt,
                label="review-action:fix-check-failures",
                timeout_seconds=self.timeout_seconds,
                max_retries=0,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Exception using Claude SDK to fix check failures: %s", e
            )
            return False

    async def run_structured(
        self,
        system_prompt: str,
        prompt: str,
        label: str = "structured",
    ) -> str:
        """Run a one-shot, tool-free prompt via the shared client harness; return raw text."""
        from deep_architect.agents.client import run_simple_text  # noqa: PLC0415
        from deep_architect.config import (  # noqa: PLC0415
            AgentConfig as ClientAgentConfig,
        )

        return await run_simple_text(
            ClientAgentConfig(model=self.model), system_prompt, prompt, label=label
        )
