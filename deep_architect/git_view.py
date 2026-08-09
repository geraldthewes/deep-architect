"""Read-only git helpers for viewing commits from the action-results browser."""

from __future__ import annotations

import subprocess
from pathlib import Path

from deep_architect.logger import get_logger

logger = get_logger(__name__)

# Soft cap for TUI display (full patch remains available via git CLI).
DEFAULT_GIT_OUTPUT_MAX_CHARS = 200_000


def discover_repo_root(
    start: Path | None = None,
    *,
    feedback_dir: Path | None = None,
) -> Path | None:
    """Locate a git work tree root for viewing commits.

    Prefers walking parents of *feedback_dir* (the repo that was fixed), then
    falls back to ``git rev-parse --show-toplevel`` from *start* (default: cwd).
    Feedback-dir-first matters when the browser is launched from another
    checkout (e.g. deep-architect) against ``plant-tracking/feedback-...``.
    """
    if feedback_dir is not None:
        current = feedback_dir.resolve()
        if current.is_file():
            current = current.parent
        for candidate in [current, *current.parents]:
            if (candidate / ".git").exists():
                nested = _rev_parse_toplevel(candidate)
                return nested if nested is not None else candidate

    cwd = (start or Path.cwd()).resolve()
    return _rev_parse_toplevel(cwd)


def _rev_parse_toplevel(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git rev-parse failed in %s: %s", cwd, exc)
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    if not path:
        return None
    return Path(path)


def _run_git(
    repo_root: Path,
    args: list[str],
    *,
    max_chars: int = DEFAULT_GIT_OUTPUT_MAX_CHARS,
) -> str:
    """Run a read-only git command; return stdout or an error message."""
    cmd = ["git", "-C", str(repo_root), *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return f"Error: git timed out: {' '.join(cmd)}"
    except OSError as exc:
        logger.error("Failed to run git: %s", exc)
        return f"Error: failed to run git: {exc}"

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        return f"Error: git exited {result.returncode}: {err}"

    text = result.stdout
    if max_chars > 0 and len(text) > max_chars:
        return (
            text[:max_chars]
            + f"\n\n… truncated at {max_chars} characters "
            f"(run `git -C {repo_root} {' '.join(args)}` for full output)\n"
        )
    return text


def git_commit_log(repo_root: Path, sha: str) -> str:
    """Return ``git log -1 --format=fuller`` for *sha*."""
    if not sha.strip():
        return "Error: no commit SHA"
    return _run_git(repo_root, ["log", "-1", "--format=fuller", sha])


def git_commit_stat(repo_root: Path, sha: str) -> str:
    """Return ``git show --stat --format=medium`` for *sha*."""
    if not sha.strip():
        return "Error: no commit SHA"
    return _run_git(repo_root, ["show", "--stat", "--format=medium", sha])


def git_commit_diff(repo_root: Path, sha: str) -> str:
    """Return full ``git show --format=medium`` patch for *sha*."""
    if not sha.strip():
        return "Error: no commit SHA"
    return _run_git(repo_root, ["show", "--format=medium", sha])
