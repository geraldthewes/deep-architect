from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from deep_architect.coding_agents.base import _file_reflects_fix
from deep_architect.logger import get_logger

logger = get_logger(__name__)

GROK_DEFAULT_TIMEOUT: float = 300.0
GROK_DEFAULT_MAX_TURNS: int = 10  # mirrors ClaudeSDKAgent.MAX_TURNS


class GrokAgent:
    """Grok Build (xAI CLI) implementation of CodingAgent using subprocess.

    Shells out to the grok binary in headless single-turn mode
    (--prompt-file + --output-format json) and parses the single JSON
    result object. Auth is inherited from the environment: a cached
    `grok login` session or the XAI_API_KEY env var.
    """

    def __init__(
        self,
        model: str | None = None,
        grok_bin: str | None = None,
        timeout_seconds: float | None = None,
        max_turns: int = GROK_DEFAULT_MAX_TURNS,
    ) -> None:
        self.model = model  # None → grok's own configured default model
        self.grok_bin = grok_bin or os.environ.get("GROK_BIN", "grok")
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else GROK_DEFAULT_TIMEOUT
        )
        self.max_turns = max_turns

    def _build_command(self, prompt_file: str) -> list[str]:
        cmd = [
            self.grok_bin,
            "--prompt-file", prompt_file,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--max-turns", str(self.max_turns),
        ]
        if self.model:
            cmd += ["-m", self.model]
        return cmd

    async def apply_fix(
        self,
        file_path: Path,
        existing_code: str,
        suggested_code: str,
        context: str = "",
        original_content: str | None = None,
    ) -> bool:
        """Apply fix using the grok CLI in headless single-turn mode."""
        absolute_file_path = file_path.resolve()

        prompt_content = (
            "You are a precise code editing assistant. Apply the following code "
            "change exactly as specified. Do not make any other changes. Do not "
            "run git or commit the change — that is handled separately.\n\n"
            f"**File**: {absolute_file_path}\n\n"
            f"**Existing Code**:\n```\n{existing_code}\n```\n\n"
            f"**Suggested Code**:\n```\n{suggested_code}\n```\n\n"
            f"**Context**: {context}\n\n"
            "Make the change and confirm it was applied correctly."
        )

        prompt_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', delete=False, encoding='utf-8'
            ) as f:
                f.write(prompt_content)
                prompt_file = f.name

            result = subprocess.run(
                self._build_command(prompt_file),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )

            grok_success = _parse_grok_json(result.returncode, result.stdout, result.stderr)
            if not grok_success:
                return False

            try:
                return _file_reflects_fix(
                    absolute_file_path, suggested_code, original_content
                )
            except Exception as e:
                logger.error(
                    "GrokAgent: error verifying fix for %s: %s", file_path, e
                )
                return False
        except FileNotFoundError:
            logger.error(
                "GrokAgent: grok binary not found at %s", self.grok_bin
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error("GrokAgent: timeout applying fix for %s", file_path)
            return False
        except Exception as e:
            logger.error(
                "GrokAgent: exception applying fix for %s: %s", file_path, e
            )
            return False
        finally:
            if prompt_file and os.path.exists(prompt_file):
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass  # Ignore cleanup errors

    async def fix_check_failures(
        self,
        files: list[Path],
        failure_report: str,
        context: str = "",
    ) -> bool:
        """Address quality-check failures using the grok CLI.

        Success = agent completed (_parse_grok_json) — the check rerun in the
        harness's fix loop is the real verification, so there's no
        _file_reflects_fix here, unlike apply_fix.
        """
        absolute_files = [f.resolve() for f in files]
        file_list = "\n".join(f"- {f}" for f in absolute_files)

        prompt_content = (
            "You are a precise code editing assistant. A previous fix introduced "
            "quality-check failures (lint/type/security/test or style-rule "
            "violations). Address them without reverting the intent of the "
            "original change. Do not run git or commit — that is handled "
            "separately.\n\n"
            f"**Modified files**:\n{file_list}\n\n"
            f"**Original review context**: {context}\n\n"
            f"{failure_report}\n\n"
            "Fix these quality-check failures, then confirm when done."
        )

        prompt_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', delete=False, encoding='utf-8'
            ) as f:
                f.write(prompt_content)
                prompt_file = f.name

            result = subprocess.run(
                self._build_command(prompt_file),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            return _parse_grok_json(result.returncode, result.stdout, result.stderr)
        except FileNotFoundError:
            logger.error(
                "GrokAgent: grok binary not found at %s", self.grok_bin
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error("GrokAgent: timeout fixing check failures")
            return False
        except Exception as e:
            logger.error("GrokAgent: exception fixing check failures: %s", e)
            return False
        finally:
            if prompt_file and os.path.exists(prompt_file):
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass  # Ignore cleanup errors


def _parse_grok_json(returncode: int, raw_stdout: str, raw_stderr: str) -> bool:
    """Parse grok --output-format json output; True if the agent completed.

    Verified contract (grok 0.2.93):
      success → exit 0, stdout is one JSON object with text/stopReason/sessionId
      failure → exit 1, stdout is {"type": "error", "message": "..."}
    """
    if returncode != 0:
        message = "unknown error"
        try:
            event = json.loads(raw_stdout.strip() or "{}")
            if event.get("type") == "error":
                message = str(event.get("message", message))
        except json.JSONDecodeError:
            pass
        if message == "unknown error" and raw_stderr.strip():
            message = raw_stderr.strip().splitlines()[-1][:200]
        logger.error("GrokAgent: failed (returncode=%d): %s", returncode, message)
        return False
    try:
        result = json.loads(raw_stdout.strip())
        logger.debug(
            "GrokAgent: completed (stopReason=%s, sessionId=%s)",
            result.get("stopReason"), result.get("sessionId"),
        )
    except json.JSONDecodeError:
        logger.warning("GrokAgent: exit 0 but non-JSON stdout — trusting exit code")
    return True
