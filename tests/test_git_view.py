"""Unit tests for deep_architect.git_view."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from deep_architect.git_view import (
    discover_repo_root,
    git_commit_diff,
    git_commit_log,
    git_commit_stat,
)


def _init_repo(path: Path) -> str:
    """Create a tiny git repo with one commit; return the short SHA."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "hello.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_discover_repo_root_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = _init_repo(tmp_path)
    assert sha
    monkeypatch.chdir(tmp_path)
    root = discover_repo_root()
    assert root is not None
    assert root.resolve() == tmp_path.resolve()


def test_discover_repo_root_from_feedback_dir(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    feedback = tmp_path / "feedback-proj"
    feedback.mkdir()
    # Even if cwd is not the target repo, feedback_dir wins.
    root = discover_repo_root(start=Path("/tmp"), feedback_dir=feedback)
    assert root is not None
    assert root.resolve() == tmp_path.resolve()


def test_discover_prefers_feedback_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    _init_repo(target)
    _init_repo(other)
    feedback = target / "feedback"
    feedback.mkdir()
    monkeypatch.chdir(other)
    root = discover_repo_root(feedback_dir=feedback)
    assert root is not None
    assert root.resolve() == target.resolve()


def test_discover_repo_root_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert discover_repo_root() is None


def test_git_commit_log_stat_diff(tmp_path: Path) -> None:
    sha = _init_repo(tmp_path)
    log = git_commit_log(tmp_path, sha)
    assert "initial commit" in log
    assert not log.startswith("Error:")

    stat = git_commit_stat(tmp_path, sha)
    assert "hello.txt" in stat
    assert not stat.startswith("Error:")

    diff = git_commit_diff(tmp_path, sha)
    assert "+hello" in diff or "hello" in diff
    assert not diff.startswith("Error:")


def test_git_bad_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    out = git_commit_log(tmp_path, "0000000dead")
    assert out.startswith("Error:")


def test_git_empty_sha(tmp_path: Path) -> None:
    assert git_commit_diff(tmp_path, "").startswith("Error:")
