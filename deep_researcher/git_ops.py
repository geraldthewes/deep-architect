from __future__ import annotations

from pathlib import Path

import git

from deep_researcher.logger import get_logger

_log = get_logger(__name__)


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


def get_modified_files(repo: git.Repo) -> list[Path]:
    """Return new and modified files from the working tree (not yet committed)."""
    working_dir = Path(repo.working_dir)
    paths: list[Path] = []
    for item in repo.untracked_files:
        paths.append(working_dir / item)
    for diff in repo.index.diff(None):
        if diff.a_path:
            paths.append(working_dir / diff.a_path)
    _log.debug("get_modified_files: %d files", len(paths))
    return paths


def git_commit(repo: git.Repo, message: str, paths: list[Path]) -> None:
    """Stage the given paths and create a commit. No-op if nothing changed."""
    str_paths = [str(p) for p in paths if p.exists()]
    if not str_paths:
        _log.debug("git_commit: no paths to stage, skipping")
        return
    repo.index.add(str_paths)
    try:
        diff = repo.index.diff("HEAD")
        if diff or repo.untracked_files:
            repo.index.commit(message)
            _log.info("Git commit: %s (%d files)", message, len(str_paths))
        else:
            _log.debug("git_commit: nothing changed after staging, skipping")
    except git.BadName:
        # Initial commit (no HEAD yet)
        repo.index.commit(message)
        _log.info("Git initial commit: %s (%d files)", message, len(str_paths))
