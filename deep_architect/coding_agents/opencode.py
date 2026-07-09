from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from deep_architect.coding_agents.base import _file_reflects_fix
from deep_architect.logger import get_logger

logger = get_logger(__name__)

OPENCODE_DEFAULT_TIMEOUT: float = 120.0


class OpencodeAgent:
    """Opencode implementation of CodingAgent using subprocess."""

    def __init__(
        self,
        model: str = "standard/coder",
        opencode_bin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model
        self.opencode_bin = opencode_bin or os.environ.get(
            "OPENCODE_BIN", "/home/gerald/.opencode/bin/opencode"
        )
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else OPENCODE_DEFAULT_TIMEOUT
        )

    def _load_prompt_template(self) -> str:
        """Load the prompt template from package resources."""
        try:
            import importlib.resources
            resource = importlib.resources.files('deep_architect.resources').joinpath(
                'prompt_template.md'
            )
            with resource.open('r') as f:
                return f.read()
        except (ImportError, AttributeError, FileNotFoundError):
            prompt_path = Path(__file__).parent.parent / 'resources' / 'prompt_template.md'
            if prompt_path.exists():
                return prompt_path.read_text(encoding='utf-8')
            return (
                "You are a precise coding assistant. Your task is to:\n"
                "1. Read the feedback file to understand what needs to be fixed\n"
                "2. Confirm the issue is valid and needs fixing  \n"
                "3. Apply the exact fix suggested in the feedback\n"
                "4. Commit the changes with a conventional commit message\n"
                "5. Briefly summarize what was done\n\n"
                "The feedback file contains:\n"
                "- File to modify\n"
                "- Existing code (what's currently there)\n"
                "- Suggested code (what it should be changed to)\n"
                "- Context/explanation of why the change is needed\n\n"
                "When committing, use the format: `fix: {brief_description} [{file_path}]`\n"
                "If no changes are needed (already fixed), that's also acceptable."
            )

    async def apply_fix(
        self,
        file_path: Path,
        existing_code: str,
        suggested_code: str,
        context: str = "",
        original_content: str | None = None,
    ) -> bool:
        """Apply fix using opencode subprocess with file-based input."""
        # Use absolute path to avoid any path resolution issues
        absolute_file_path = file_path.resolve()

        # Create temporary files for prompt and feedback
        prompt_file = None
        feedback_file = None

        try:
            # Create prompt file
            prompt_content = self._load_prompt_template()
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', delete=False, encoding='utf-8'
            ) as f:
                f.write(prompt_content)
                prompt_file = f.name

            # Create feedback file with the specific finding details
            feedback_content = (
                f"**File**: {absolute_file_path}\n\n"
                f"**Existing Code**:\n```\n{existing_code}\n```\n\n"
                f"**Suggested Code**:\n```\n{suggested_code}\n```\n\n"
                f"**Review Comment**: {context}\n"
            )
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', delete=False, encoding='utf-8'
            ) as f:
                f.write(feedback_content)
                feedback_file = f.name

            # Run opencode with file-based input
            result = subprocess.run(
                [
                    self.opencode_bin,
                    "run",
                    "--format",
                    "json",
                    "--dangerously-skip-permissions",
                    "Apply the fix based on the review feedback",
                    "--file",
                    prompt_file,
                    "--file",
                    feedback_file,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )

            opencode_success = _parse_opencode_ndjson(result.stdout)
            if opencode_success:
                try:
                    return _file_reflects_fix(
                        absolute_file_path, suggested_code, original_content
                    )
                except Exception as e:
                    logger.error(
                        "OpencodeAgent: error verifying fix for %s: %s",
                        file_path,
                        e,
                    )
                    return False
            else:
                # opencode failed - extract error information from output
                last_error = "unknown error"
                stdout_preview = ""
                stderr_preview = ""
                error_details = []
                full_stdout_for_debug = result.stdout[:1000] if result.stdout else ""
                full_stderr_for_debug = result.stderr[:1000] if result.stderr else ""

                if result.stderr.strip():
                    stderr_lines = [
                        line.strip() for line in result.stderr.splitlines() if line.strip()
                    ]
                    if stderr_lines:
                        stderr_preview = " | ".join(stderr_lines[-3:])
                        raw_stderr = result.stderr.strip()
                        last_error = raw_stderr[:200]
                        error_details.append(f"stderr: {raw_stderr[:100]}")

                if result.stdout.strip():
                    stdout_lines = [
                        line.strip() for line in result.stdout.splitlines() if line.strip()
                    ]
                    if stdout_lines:
                        stdout_preview = " | ".join(stdout_lines[-3:])
                        # Try to parse NDJSON for error details
                        for line in result.stdout.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                # Check for error events
                                if event.get("type") == "error":
                                    error_data = event.get("error", {})
                                    if isinstance(error_data, dict):
                                        message = error_data.get("message", "Unknown error")
                                        last_error = str(message)[:200]
                                        error_details.append(f"error event: {message}")
                                    else:
                                        last_error = str(error_data)[:200]
                                        error_details.append(f"error event: {error_data}")
                                # Check for tool_use events with errors
                                elif event.get("type") == "tool_use":
                                    part = event.get("part", {})
                                    if part.get("type") == "tool_use":
                                        state = part.get("state", {})
                                        if state.get("status") == "error":
                                            error_msg = state.get("error", "unknown error")
                                            last_error = error_msg[:200]
                                            error_details.append(f"tool_use error: {error_msg}")
                                # Check for text events
                                elif event.get("type") == "text":
                                    part = event.get("part", {})
                                    if part.get("type") == "text":
                                        text_content = part.get("text", "")
                                        has_error_indicator = text_content and (
                                            "error" in text_content.lower()
                                            or "fail" in text_content.lower()
                                        )
                                        if has_error_indicator:
                                            last_error = text_content[:200]
                                            error_details.append(
                                                f"text error indicator: {text_content[:100]}"
                                            )
                            except json.JSONDecodeError:
                                # Not JSON, might be raw text - use first 200 chars
                                if not last_error or last_error == "no output":
                                    last_error = line[:200]
                                    error_details.append(f"raw line: {line[:100]}")

                # If still no specific error, check text events for anything useful.
                if last_error == "unknown error" and result.stdout.strip():
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                            if event.get("type") == "text":
                                part = event.get("part", {})
                                if part.get("type") == "text":
                                    text_content = part.get("text", "")
                                    if text_content and len(text_content) > 10:  # skip tiny bits
                                        last_error = text_content[:200]
                                        error_details.append(f"text content: {text_content[:100]}")
                                        break
                        except json.JSONDecodeError:
                            pass

                logger.error(
                    "OpencodeAgent: failed to apply fix for %s: returncode=%d, error=%s, "
                    "stdout_preview=%s, stderr_preview=%s, error_details=%s, full_stdout=%s, "
                    "full_stderr=%s, model=%s",
                    file_path,
                    result.returncode,
                    last_error,
                    stdout_preview or "(empty)",
                    stderr_preview or "(empty)",
                    " | ".join(error_details[:3]) if error_details else "(none)",
                    full_stdout_for_debug,
                    full_stderr_for_debug,
                    self.model,
                )
                return False
        except FileNotFoundError:
            logger.error(
                "OpencodeAgent: opencode binary not found at %s",
                self.opencode_bin,
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error(
                "OpencodeAgent: timeout applying fix for %s", file_path
            )
            return False
        except Exception as e:
            logger.error(
                "OpencodeAgent: exception applying fix for %s: %s",
                file_path,
                e,
            )
            return False
        finally:
            # Clean up temporary files
            for temp_file in [prompt_file, feedback_file]:
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.unlink(temp_file)
                    except OSError:
                        pass  # Ignore cleanup errors

    async def fix_check_failures(
        self,
        files: list[Path],
        failure_report: str,
        context: str = "",
    ) -> bool:
        """Address quality-check failures using opencode subprocess with file-based input.

        Success = agent completed (_parse_opencode_ndjson) — the check rerun in the
        harness's fix loop is the real verification, so there's no _file_reflects_fix here.
        """
        file_list = "\n".join(f"- {f.resolve()}" for f in files)

        prompt_file = None
        feedback_file = None
        try:
            prompt_content = self._load_prompt_template()
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', delete=False, encoding='utf-8'
            ) as f:
                f.write(prompt_content)
                prompt_file = f.name

            feedback_content = (
                "Your previous fix introduced these quality-check failures — fix them "
                "without reverting the intent of the original change:\n\n"
                f"**Modified files**:\n{file_list}\n\n"
                f"**Original review context**: {context}\n\n"
                f"{failure_report}\n"
            )
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', delete=False, encoding='utf-8'
            ) as f:
                f.write(feedback_content)
                feedback_file = f.name

            result = subprocess.run(
                [
                    self.opencode_bin,
                    "run",
                    "--format",
                    "json",
                    "--dangerously-skip-permissions",
                    "Fix the quality-check failures described in the feedback",
                    "--file",
                    prompt_file,
                    "--file",
                    feedback_file,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            return _parse_opencode_ndjson(result.stdout)
        except FileNotFoundError:
            logger.error(
                "OpencodeAgent: opencode binary not found at %s",
                self.opencode_bin,
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error("OpencodeAgent: timeout fixing check failures")
            return False
        except Exception as e:
            logger.error(
                "OpencodeAgent: exception fixing check failures: %s", e
            )
            return False
        finally:
            for temp_file in [prompt_file, feedback_file]:
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.unlink(temp_file)
                    except OSError:
                        pass  # Ignore cleanup errors


def _parse_opencode_ndjson(raw_stdout: str) -> bool:
    """Parse opencode NDJSON output and return True if the agent completed.

    opencode streams NDJSON events; we check for a ResultMessage-like event
    that indicates completion without error.
    """
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # ResultMessage in NDJSON: {"type": "result", ...}
        if event.get("type") == "result":
            is_error = event.get("is_error", False)
            if is_error:
                error_detail = event.get("errors", ["Unknown error"])
                logger.error(
                    "OpencodeAgent: result error: %s", error_detail
                )
                return False
            return True

    # No result event found — fallback: check stderr for clues
    if raw_stdout.strip():
        logger.warning(
            "OpencodeAgent: no result event in output, assuming partial"
        )
    return False
