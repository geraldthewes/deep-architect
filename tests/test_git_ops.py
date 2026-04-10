from pathlib import Path

import git
import pytest

from deep_researcher.git_ops import get_modified_files, git_commit, validate_git_repo


def test_validate_git_repo_success(tmp_path: Path) -> None:
    git.Repo.init(tmp_path)
    result = validate_git_repo(tmp_path)
    assert result is not None


def test_validate_git_repo_fails_outside_git(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not inside a git repository"):
        validate_git_repo(tmp_path)


def test_git_commit_creates_commit(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    # Initial empty commit required so HEAD exists
    repo.index.commit("init")

    test_file = tmp_path / "test.md"
    test_file.write_text("# Test")
    git_commit(repo, "test commit", [test_file])

    assert repo.head.commit.message == "test commit"


def test_git_commit_noop_when_no_files(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")
    initial_sha = repo.head.commit.hexsha

    # Commit with non-existent file — should be a no-op
    git_commit(repo, "empty commit", [tmp_path / "nonexistent.md"])

    assert repo.head.commit.hexsha == initial_sha


def test_git_commit_initial_no_head(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    test_file = tmp_path / "first.md"
    test_file.write_text("# First")

    # Should handle BadName (no HEAD) gracefully
    git_commit(repo, "initial commit", [test_file])

    assert repo.head.commit.message == "initial commit"


def test_get_modified_files_detects_new(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    new_file = tmp_path / "new.md"
    new_file.write_text("# New")

    files = get_modified_files(repo)
    assert any("new.md" in str(f) for f in files)


def test_get_modified_files_detects_modified(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    existing = tmp_path / "existing.md"
    existing.write_text("# Original")
    repo.index.add([str(existing)])
    repo.index.commit("init")

    existing.write_text("# Modified")
    files = get_modified_files(repo)
    assert any("existing.md" in str(f) for f in files)


def test_get_modified_files_empty_on_clean(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    existing = tmp_path / "clean.md"
    existing.write_text("# Clean")
    repo.index.add([str(existing)])
    repo.index.commit("init")

    files = get_modified_files(repo)
    assert files == []
