from __future__ import annotations

from pathlib import Path

import git

from deep_researcher.logger import get_logger

_log = get_logger(__name__)

_EXCLUDED_FROM_ROLLBACK = frozenset({
    "generator-learnings.md",
    "generator-history.md",
    "critic-history.md",
})


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


def restore_arch_files_from_commit(repo: git.Repo, best_commit_sha: str) -> list[str]:
    """Restore architecture files to the state at best_commit_sha.

    Diffs best_commit_sha → HEAD and reverts all changes, skipping files in
    _EXCLUDED_FROM_ROLLBACK. Restores modified/deleted files via checkout,
    deletes files added after best, and handles renames. Returns the list of
    relative paths affected. Returns [] when HEAD already equals best_commit_sha.
    """
    if repo.head.commit.hexsha == best_commit_sha:
        return []

    best_commit = repo.commit(best_commit_sha)
    diffs = best_commit.diff(repo.head.commit)
    restored: list[str] = []

    for diff in diffs:
        change_type = diff.change_type

        if change_type in ("M", "D"):
            path = diff.a_path
            if path is None or Path(path).name in _EXCLUDED_FROM_ROLLBACK:
                continue
            repo.git.checkout(best_commit_sha, "--", path)
            restored.append(path)

        elif change_type == "A":
            path = diff.b_path
            if path is None or Path(path).name in _EXCLUDED_FROM_ROLLBACK:
                continue
            disk_path = Path(repo.working_dir) / path
            if disk_path.exists():
                disk_path.unlink()
            repo.index.remove([path])
            restored.append(path)

        elif change_type in ("R", "C"):
            b_path = diff.b_path
            a_path = diff.a_path
            if b_path is not None and Path(b_path).name not in _EXCLUDED_FROM_ROLLBACK:
                disk_b = Path(repo.working_dir) / b_path
                if disk_b.exists():
                    disk_b.unlink()
                repo.index.remove([b_path])
                restored.append(b_path)
            if a_path is not None and Path(a_path).name not in _EXCLUDED_FROM_ROLLBACK:
                repo.git.checkout(best_commit_sha, "--", a_path)
                if a_path not in restored:
                    restored.append(a_path)

    _log.info(
        "restore_arch_files_from_commit: restored %d file(s) to %s",
        len(restored), best_commit_sha[:8],
    )
    return restored


def git_commit_staged(repo: git.Repo, message: str) -> bool:
    """Commit whatever is currently staged in the index. Returns True if a commit was made."""
    try:
        has_changes = bool(repo.index.diff("HEAD"))
    except git.BadName:
        has_changes = True  # no HEAD yet — initial commit path
    if has_changes:
        repo.index.commit(message)
        _log.info("Git commit (staged): %s", message)
        return True
    return False
