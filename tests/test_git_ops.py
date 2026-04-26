from pathlib import Path

import git
import pytest

from deep_architect.git_ops import (
    get_modified_files,
    git_commit,
    git_commit_staged,
    reject_unauthorized_files,
    restore_arch_files_from_commit,
    validate_git_repo,
)


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


def test_git_commit_sprint_boundary_message(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    feedback = tmp_path / "feedback.json"
    feedback.write_text('{"status": "passed"}')
    progress = tmp_path / "progress.json"
    progress.write_text('{"completed_sprints": 1}')

    message = "Sprint 1 complete: C4 Context View"
    git_commit(repo, message, [feedback, progress])

    assert repo.head.commit.message == message


def test_git_commit_final_completion_message(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    progress = tmp_path / "progress.json"
    progress.write_text('{"status": "complete"}')

    message = "Architecture complete — all 7 sprints passed"
    git_commit(repo, message, [progress])

    assert repo.head.commit.message == message


def test_git_commit_sprint_boundary_noop(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    # Simulate: generator already committed everything
    f = tmp_path / "file.md"
    f.write_text("content")
    git_commit(repo, "Generator pass 3 - sprint 1 (Context)", [f])
    sha_after_gen = repo.head.commit.hexsha

    # Sprint-boundary commit should be a no-op
    git_commit(repo, "Sprint 1 complete: C4 Context View", [])

    assert repo.head.commit.hexsha == sha_after_gen


def test_restore_arch_files_restores_modified(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    arch_file = tmp_path / "architecture.md"
    arch_file.write_text("# Best content")
    repo.index.add([str(arch_file)])
    best_commit = repo.index.commit("best")
    best_sha = best_commit.hexsha

    arch_file.write_text("# Regressed content")
    repo.index.add([str(arch_file)])
    repo.index.commit("regressed")

    restored = restore_arch_files_from_commit(repo, best_sha)

    assert "architecture.md" in restored
    assert arch_file.read_text() == "# Best content"


def test_restore_arch_files_skips_excluded(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    excluded_file = tmp_path / "generator-learnings.md"
    excluded_file.write_text("# Original learnings")
    repo.index.add([str(excluded_file)])
    best_commit = repo.index.commit("best")
    best_sha = best_commit.hexsha

    excluded_file.write_text("# Updated learnings")
    repo.index.add([str(excluded_file)])
    repo.index.commit("updated learnings")

    restored = restore_arch_files_from_commit(repo, best_sha)

    assert restored == []
    assert excluded_file.read_text() == "# Updated learnings"


def test_restore_arch_files_deletes_added_files(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    base_file = tmp_path / "base.md"
    base_file.write_text("# base")
    repo.index.add([str(base_file)])
    best_commit = repo.index.commit("best")
    best_sha = best_commit.hexsha

    added_file = tmp_path / "added-after-best.md"
    added_file.write_text("# Added later")
    repo.index.add([str(added_file)])
    repo.index.commit("added file")

    restored = restore_arch_files_from_commit(repo, best_sha)

    assert "added-after-best.md" in restored
    assert not added_file.exists()


def test_restore_arch_files_noop_when_at_best(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    f = tmp_path / "arch.md"
    f.write_text("# content")
    repo.index.add([str(f)])
    best_commit = repo.index.commit("best")
    best_sha = best_commit.hexsha

    restored = restore_arch_files_from_commit(repo, best_sha)

    assert restored == []


def test_git_commit_staged_commits_staged(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")

    f = tmp_path / "staged.md"
    f.write_text("# content")
    repo.index.add([str(f)])

    result = git_commit_staged(repo, "staged commit")

    assert result is True
    assert repo.head.commit.message == "staged commit"


def test_git_commit_staged_noop_when_nothing_staged(tmp_path: Path) -> None:
    repo = git.Repo.init(tmp_path)
    repo.index.commit("init")
    initial_sha = repo.head.commit.hexsha

    result = git_commit_staged(repo, "nothing to commit")

    assert result is False
    assert repo.head.commit.hexsha == initial_sha


# --- reject_unauthorized_files tests ---


def _make_repo_with_file(tmp_path: Path, filename: str, content: str = "# content") -> git.Repo:
    repo = git.Repo.init(tmp_path)
    f = tmp_path / filename
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    repo.index.add([str(f)])
    repo.index.commit("init")
    return repo


def test_reject_unauthorized_untracked_file_deleted(tmp_path: Path) -> None:
    repo = _make_repo_with_file(tmp_path, "c1-context.md")
    scratch = tmp_path / "frontend" / "debug.md"
    scratch.parent.mkdir(exist_ok=True)
    scratch.write_text("# scratch")
    paths = get_modified_files(repo)

    rejected = reject_unauthorized_files(
        repo, tmp_path, paths,
        allowed_relpaths={"c1-context.md"},
        allowed_dir_prefixes=set(),
    )

    assert scratch not in rejected or not scratch.exists()
    assert any("debug.md" in str(r) for r in rejected)
    assert not scratch.exists()


def test_reject_unauthorized_tracked_modified_file_reverted(tmp_path: Path) -> None:
    repo = _make_repo_with_file(tmp_path, "c1-context.md")
    extra = tmp_path / "extra.md"
    extra.write_text("# original")
    repo.index.add([str(extra)])
    repo.index.commit("add extra")

    extra.write_text("# modified")
    paths = get_modified_files(repo)

    reject_unauthorized_files(
        repo, tmp_path, paths,
        allowed_relpaths={"c1-context.md"},
        allowed_dir_prefixes=set(),
    )

    assert extra.read_text() == "# original"


def test_reject_unauthorized_allowed_dir_prefix_kept(tmp_path: Path) -> None:
    repo = _make_repo_with_file(tmp_path, "c1-context.md")
    frontend = tmp_path / "frontend" / "c2-container.md"
    frontend.parent.mkdir(exist_ok=True)
    frontend.write_text("# frontend")
    paths = get_modified_files(repo)

    rejected = reject_unauthorized_files(
        repo, tmp_path, paths,
        allowed_relpaths={"c1-context.md"},
        allowed_dir_prefixes={"frontend/"},
    )

    assert not any("c2-container.md" in str(r) for r in rejected)
    assert frontend.exists()


def test_reject_unauthorized_harness_state_files_kept(tmp_path: Path) -> None:
    repo = _make_repo_with_file(tmp_path, "c1-context.md")
    for name in ("generator-history.md", "generator-learnings.md"):
        (tmp_path / name).write_text("# state")
    paths = get_modified_files(repo)

    rejected = reject_unauthorized_files(
        repo, tmp_path, paths,
        allowed_relpaths={"c1-context.md", "generator-history.md", "generator-learnings.md"},
        allowed_dir_prefixes=set(),
    )

    assert not any("generator-history" in str(r) for r in rejected)
    assert not any("generator-learnings" in str(r) for r in rejected)


def test_reject_unauthorized_contracts_dir_kept(tmp_path: Path) -> None:
    repo = _make_repo_with_file(tmp_path, "c1-context.md")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "sprint-1.json").write_text("{}")
    paths = get_modified_files(repo)

    rejected = reject_unauthorized_files(
        repo, tmp_path, paths,
        allowed_relpaths={"c1-context.md"},
        allowed_dir_prefixes={"contracts/"},
    )

    assert not any("sprint-1.json" in str(r) for r in rejected)
    assert (contracts_dir / "sprint-1.json").exists()


def test_reject_unauthorized_noop_when_all_allowed(tmp_path: Path) -> None:
    repo = _make_repo_with_file(tmp_path, "c1-context.md")
    new_file = tmp_path / "c2-container.md"
    new_file.write_text("# c2")
    paths = get_modified_files(repo)

    rejected = reject_unauthorized_files(
        repo, tmp_path, paths,
        allowed_relpaths={"c1-context.md", "c2-container.md"},
        allowed_dir_prefixes=set(),
    )

    assert rejected == []
    assert new_file.exists()
