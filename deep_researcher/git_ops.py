from __future__ import annotations

from pathlib import Path

import git


def validate_git_repo(path: Path) -> git.Repo:
    """Fail fast with clear error if path is not inside a git repo."""
    try:
        repo = git.Repo(path, search_parent_directories=True)
        return repo
    except git.InvalidGitRepositoryError:
        raise SystemExit(
            f"Error: {path} is not inside a git repository.\n"
            "adversarial-architect requires a git repo for auto-commits."
        )


def git_commit(repo: git.Repo, message: str, paths: list[Path]) -> None:
    """Stage the given paths and create a commit. No-op if nothing changed."""
    str_paths = [str(p) for p in paths if p.exists()]
    if not str_paths:
        return
    repo.index.add(str_paths)
    try:
        diff = repo.index.diff("HEAD")
        if diff or repo.untracked_files:
            repo.index.commit(message)
    except git.BadName:
        # Initial commit (no HEAD yet)
        repo.index.commit(message)
